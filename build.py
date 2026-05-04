"""Build the public AI-coding adoption site (Chinese landing page).

Reads:
  ~/.claude/data/trackers/openrouter_model_share/daily_snapshots.jsonl
  ~/.claude/data/trackers/claude_code_adoption/weekly_counts.jsonl
  ~/.claude/data/trackers/npm_pypi_downloads/weekly_downloads.jsonl

Writes:
  <site>/index.html        — Chinese landing page with KPIs, methodology, charts
  <site>/data/latest.json  — small aggregate JSON consumed by index.html
  <site>/dashboards/*.html — copies of the 4 detail dashboards

Run after each tracker finishes. Idempotent.
"""

from __future__ import annotations

import json
import re
import shutil
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

SITE = Path(__file__).resolve().parent
DATA_OUT = SITE / "data"
DASH_OUT = SITE / "dashboards"
T = Path.home() / ".claude" / "data" / "trackers"
GH_STARS_FILE = Path.home() / ".claude" / "data" / "github_stars.jsonl"

# 2026-04-30: @openai/codex switched to per-platform binary distribution
# (79.4MB tarball, republished multiple times daily). npm/range counts before
# and after this date are NOT apples-to-apples; the chart line passes through
# the boundary but downstream UIs annotate it.
CODEX_REGIME_BREAK = "2026-04-30"


def _load_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [
        json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()
    ]


def _aggregate_commits() -> dict:
    rows = _load_jsonl(T / "claude_code_adoption" / "weekly_counts.jsonl")
    seen: dict[str, dict] = {}
    for r in rows:
        k = r.get("week_end")
        if not k:
            continue
        # Prefer rows with non-None per-agent data over rows that retried with stripped schema.
        # Within same completeness, prefer later fetched_at.
        cur = seen.get(k)
        r_complete = r.get("claude") is not None
        cur_complete = cur is not None and cur.get("claude") is not None
        if cur is None:
            seen[k] = r
        elif r_complete and not cur_complete:
            seen[k] = r
        elif r_complete == cur_complete and r.get("fetched_at", "") > cur.get(
            "fetched_at", ""
        ):
            seen[k] = r
    weeks = sorted(seen.values(), key=lambda r: r["week_end"])
    series: dict[str, list] = {
        agent: []
        for agent in ("claude", "codex", "copilot", "cursor", "aider", "devin")
    }
    labels: list[str] = []
    for r in weeks:
        if r.get("claude") is None:
            continue
        labels.append(r["week_end"])
        for k in series:
            series[k].append(r.get(k) or 0)
    latest = weeks[-1] if weeks else {}
    prev = weeks[-2] if len(weeks) >= 2 else {}
    return {
        "labels": labels,
        "series": series,
        "latest": {
            k: latest.get(k)
            for k in (
                "week_start",
                "week_end",
                "claude",
                "codex",
                "copilot",
                "cursor",
                "aider",
                "devin",
                "total_pushes",
                "distinct_devs",
            )
        },
        "prev": {k: prev.get(k) for k in ("claude", "codex", "copilot")},
    }


def _aggregate_npm() -> dict:
    rows = _load_jsonl(T / "npm_pypi_downloads" / "weekly_downloads.jsonl")
    rows.sort(key=lambda r: r["week_end"])
    labels = [r["week_end"] for r in rows]
    keys = [
        "npm_claude-code",
        "npm_anthropic-sdk",
        "npm_openai-codex",
        "npm_openai-sdk",
    ]
    series = {k: [r.get(k) or 0 for r in rows] for k in keys}
    latest = rows[-1] if rows else {}
    prev = rows[-2] if len(rows) >= 2 else {}
    # Mark which weeks are affected by the @openai/codex regime break (4/30).
    # Any week whose end date is >= the boundary is post-shift.
    codex_regime_post = [i for i, lab in enumerate(labels) if lab >= CODEX_REGIME_BREAK]
    return {
        "labels": labels,
        "series": series,
        "latest": {
            **{k: latest.get(k) for k in keys},
            "week_start": latest.get("week_start"),
            "week_end": latest.get("week_end"),
        },
        "prev": {k: prev.get(k) for k in keys},
        "codex_regime_break": CODEX_REGIME_BREAK,
        "codex_regime_post_indices": codex_regime_post,
    }


def _aggregate_openrouter() -> dict:
    """Provider-level token totals per day (last 90 days).

    JSONL is append-only and OpenRouter back-fills past days, so the same
    (date, model_permaslug, variant) key appears multiple times — keep the
    last occurrence (latest fetch wins) before summing.
    """
    p = T / "openrouter_model_share" / "daily_snapshots.jsonl"
    if not p.exists():
        return {"labels": [], "series": {}, "latest_date": None, "today_total_t": 0}
    # Dedup pass — last write wins (matches scraper's _dedupe_models).
    slot: dict[tuple, dict] = {}
    with p.open(encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            k = (r.get("date"), r.get("model_permaslug"), r.get("variant"))
            slot[k] = r
    daily: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in slot.values():
        d = r.get("date")
        if not d:
            continue
        author = r.get("author") or "other"
        daily[d][author] += (r.get("tokens_completion", 0) or 0) + (
            r.get("tokens_prompt", 0) or 0
        )
    from datetime import date as _date, timedelta as _td

    all_dates = sorted(daily.keys())
    today_iso = _date.today().isoformat()
    # Drop today AND yesterday — OpenRouter back-fills BYOK / log-aggregation
    # data over 7-14 days, so the 2 most recent days routinely undercount.
    # Showing them on a public chart would imply a fake decline. Cutoff
    # = today - 2 days. (Anything dropped here will appear in tomorrow's run.)
    cutoff_iso = (_date.today() - _td(days=2)).isoformat()
    full_dates = [d for d in all_dates if d <= cutoff_iso][-90:]

    # Anomaly detection: if any of last 4 days is < 75% of prior 7-day median,
    # mark it as suspect (likely scraper regression). Threshold tightened from
    # 40% on 2026-05-04 — the prior threshold missed a 38% drop caused by
    # top_n=300 truncation in the scraper, which silently dropped 113 long-tail
    # models. 75% catches that and similar future regressions while still
    # allowing real day-of-week swings (e.g., weekend dips ~80% of midweek).
    daily_totals = [(d, sum(daily[d].values())) for d in full_dates]
    suspect: list[dict] = []
    if len(daily_totals) >= 11:
        for i in range(len(daily_totals) - 4, len(daily_totals)):
            d, t = daily_totals[i]
            window = sorted(x[1] for x in daily_totals[max(0, i - 7) : i])
            if window:
                med = window[len(window) // 2]
                if med > 0 and t / med < 0.75:
                    suspect.append(
                        {"date": d, "total_b": t / 1e9, "median_b": med / 1e9}
                    )

    # For "today's KPI" use latest non-suspect day; if all 4 are suspect, use
    # latest anyway but flag in UI.
    suspect_dates = {s["date"] for s in suspect}
    clean_full = [d for d in full_dates if d not in suspect_dates]
    latest_clean = (
        clean_full[-1] if clean_full else (full_dates[-1] if full_dates else None)
    )

    # Use clean dates for the chart (don't render suspect cliffs)
    chart_dates = clean_full if clean_full else full_dates
    # Always pin the 3 hyperscalers so the Claude/GPT/Gemini story is continuous,
    # then add top-4 challengers by today's volume (typically Moonshot/DeepSeek/xAI/Qwen).
    PINNED = ["anthropic", "openai", "google"]
    if chart_dates:
        latest_d = chart_dates[-1]
        ranked = sorted(daily[latest_d].items(), key=lambda kv: -kv[1])
        challengers = [a for a, _ in ranked if a not in PINNED][:4]
        tracked = PINNED + challengers
        top_authors = [(a, daily[latest_d].get(a, 0)) for a in tracked]
    else:
        tracked = []
        top_authors = []
    series = {a: [daily[d].get(a, 0) for d in chart_dates] for a in tracked}
    series["其他"] = [
        sum(v for k, v in daily[d].items() if k not in tracked) for d in chart_dates
    ]
    latest_total = sum(daily[latest_clean].values()) if latest_clean else 0
    # Recently-dropped dates (today + yesterday): present in raw data but
    # excluded from chart because OpenRouter is still back-filling them.
    recently_dropped = [d for d in all_dates if d > cutoff_iso]
    return {
        "labels": chart_dates,
        "series": series,
        "latest_date": latest_clean,
        "today_total_t": latest_total / 1e12,
        "providers_today": [(a, v / 1e9) for a, v in top_authors],
        "suspect": suspect,
        "dropped_dates": sorted(suspect_dates) + recently_dropped,
        "recently_dropped": recently_dropped,
    }


_OR_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[\d+,\s*"(.*?)"\]\)', re.DOTALL)


def _or_extract_bracketed(text: str, start: int, op: str = "{", cl: str = "}") -> str:
    depth = 0
    in_str = False
    esc = False
    for j in range(start, len(text)):
        ch = text[j]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == op:
            depth += 1
        elif ch == cl:
            depth -= 1
            if depth == 0:
                return text[start : j + 1]
    return ""


def _aggregate_or_leaderboard(top_n: int = 12) -> dict:
    """Live-fetch openrouter.ai/rankings and parse the top-N models.

    The /rankings page embeds `rankingData` arrays in Next.js Flight chunks.
    OpenRouter publishes weekly-rolling cumulative tokens per (model, variant)
    stamped with one date. We use the latest stamped date as the "as_of" and
    return the top-N models by token volume.

    Self-contained — does not import from ~/scripts/.
    Returns {} on any failure (network, parse) so build.py never blocks on it.
    """
    try:
        import requests as _req

        r = _req.get(
            "https://openrouter.ai/rankings",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 11.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0 Safari/537.36"
                ),
                "Accept": "text/html",
            },
            timeout=20,
        )
        r.raise_for_status()
        html = r.text
    except Exception:
        return {}
    by_key: dict[tuple, dict] = {}
    for raw in _OR_PUSH_RE.findall(html):
        try:
            u = raw.encode().decode("unicode_escape")
        except UnicodeDecodeError:
            continue
        m = re.search(r'"rankingData":\[', u)
        if not m:
            continue
        arr = _or_extract_bracketed(u, m.end() - 1, "[", "]")
        if not arr:
            continue
        try:
            data = json.loads(arr)
        except json.JSONDecodeError:
            continue
        for row in data:
            slug = row.get("model_permaslug") or ""
            variant = row.get("variant") or ""
            d = (row.get("date") or "")[:10]
            tok = int(row.get("total_completion_tokens") or 0) + int(
                row.get("total_prompt_tokens") or 0
            )
            if not slug or not d:
                continue
            key = (slug, variant)
            existing = by_key.get(key)
            if existing is None or tok > existing["tokens"]:
                by_key[key] = {
                    "permaslug": slug,
                    "variant": variant,
                    "tokens": tok,
                    "date": d,
                    "author": slug.split("/", 1)[0],
                }
    if not by_key:
        return {}
    rows = sorted(by_key.values(), key=lambda r: -r["tokens"])
    as_of = max(r["date"] for r in rows)
    top = rows[:top_n]
    return {
        "as_of": as_of,
        "note": "weekly cumulative · OpenRouter /rankings",
        "models": [
            {
                "permaslug": r["permaslug"],
                "variant": r["variant"],
                "author": r["author"],
                "tokens_b": r["tokens"] / 1e9,
            }
            for r in top
        ],
    }


def _aggregate_github_stars() -> dict:
    """GitHub star history for anthropics/claude-code vs openai/codex.

    Source: ~/.claude/data/github_stars.jsonl (populated daily by
    vscode_tracker_dashboard.py). Returns latest counts and 7-day delta.
    Returns empty dict if file does not exist or has < 2 days of data.
    """
    if not GH_STARS_FILE.exists():
        return {}
    rows = _load_jsonl(GH_STARS_FILE)
    if not rows:
        return {}
    by_name: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("name") and r.get("date"):
            by_name[r["name"]].append(r)
    out: dict[str, dict] = {}
    for name, entries in by_name.items():
        entries.sort(key=lambda e: e["date"])
        latest = entries[-1]
        latest_dt = datetime.fromisoformat(latest["date"])
        target = (latest_dt - timedelta(days=7)).date().isoformat()
        prior = None
        for e in reversed(entries[:-1]):
            if e["date"] <= target:
                prior = e
                break
        delta = None
        if prior is not None:
            delta = latest.get("stars", 0) - prior.get("stars", 0)
        out[name] = {
            "stars": latest.get("stars", 0),
            "delta_7d": delta,
            "as_of": latest["date"],
        }
    return out


def build():
    DATA_OUT.mkdir(parents=True, exist_ok=True)
    DASH_OUT.mkdir(parents=True, exist_ok=True)

    commits = _aggregate_commits()
    npm = _aggregate_npm()
    orouter = _aggregate_openrouter()
    or_leaderboard = _aggregate_or_leaderboard(top_n=12)
    gh_stars = _aggregate_github_stars()

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "commits": commits,
        "npm": npm,
        "openrouter": orouter,
        "or_leaderboard": or_leaderboard,
        "gh_stars": gh_stars,
    }
    (DATA_OUT / "latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    for src_name, out_name in [
        ("openrouter_model_share", "openrouter.html"),
        ("claude_code_adoption", "commits.html"),
        ("npm_pypi_downloads", "npm.html"),
        ("ai_coding_unified", "unified.html"),
    ]:
        src = T / src_name / "dashboard.html"
        if src.exists():
            shutil.copy2(src, DASH_OUT / out_name)

    (SITE / "index.html").write_text(_render_index(payload), encoding="utf-8")
    print(f"[build_site] wrote {SITE / 'index.html'}")
    print(f"[build_site] wrote {DATA_OUT / 'latest.json'}")
    print(f"[build_site] copied 4 dashboards to {DASH_OUT}")


def _fmt(n, unit_threshold=1e6):
    if n is None:
        return "—"
    if n >= 1e9:
        return f"{n / 1e9:.2f}B"
    if n >= 1e6:
        return f"{n / 1e6:.2f}M"
    if n >= 1e3:
        return f"{n / 1e3:.1f}K"
    return f"{n:,}"


def _wow(curr, prev):
    if not curr or not prev:
        return ""
    pct = (curr - prev) / prev * 100
    arrow = "▲" if pct > 0 else "▼"
    return f"{arrow} {pct:+.1f}% WoW"


def _render_index(p: dict) -> str:
    c = p["commits"]["latest"]
    cprev = p["commits"]["prev"]
    n = p["npm"]["latest"]
    nprev = p["npm"]["prev"]
    o = p["openrouter"]
    gen = datetime.fromisoformat(p["generated_at"]).strftime("%Y-%m-%d %H:%M UTC")
    data_blob = json.dumps(
        {
            "commits": p["commits"],
            "npm": p["npm"],
            "openrouter": p["openrouter"],
        },
        ensure_ascii=False,
    )
    template = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>AI Coding 工具采用度跟踪</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="每日跟踪 Claude Code / Codex / Cursor / Copilot 等 AI Coding 工具的采用度信号：GitHub commits、npm/PyPI 下载、OpenRouter token 用量">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Ctext x='2' y='13' font-size='13'%3E📈%3C/text%3E%3C/svg%3E">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3"></script>
<style>
:root {
  --bg:#0d1117; --card:#161b22; --border:#30363d; --text:#c9d1d9;
  --muted:#8b949e; --accent:#58a6ff; --warn:#f0b400; --good:#3fb950;
  --claude:#f85149; --codex:#3fb950; --cursor:#58a6ff; --copilot:#a371f7;
  --aider:#d97706; --devin:#ec4899;
}
* { box-sizing: border-box; }
body { margin:0; padding:0; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif; background:var(--bg); color:var(--text); line-height:1.6; }
.wrap { max-width:1180px; margin:0 auto; padding:32px 24px 80px; }
header { padding-bottom:24px; border-bottom:1px solid var(--border); margin-bottom:32px; }
h1 { margin:0 0 10px; font-size:30px; letter-spacing:-0.02em; }
.subtitle { color:var(--muted); font-size:14px; margin:0; }
.subtitle .gen { color:var(--accent); font-variant-numeric:tabular-nums; }
.section { margin:48px 0 0; }
.section-title { font-size:13px; color:var(--muted); text-transform:uppercase; letter-spacing:0.12em; margin:0 0 16px; padding-left:10px; border-left:3px solid var(--accent); }
h2 { font-size:22px; margin:0 0 8px; letter-spacing:-0.01em; }
.lead { color:var(--muted); font-size:13px; margin:0 0 20px; max-width:780px; }

.banner-warn { background:rgba(240,180,0,0.08); border:1px solid var(--warn); border-left:4px solid var(--warn); border-radius:6px; padding:14px 18px; margin:0 0 24px; font-size:13px; line-height:1.55; }
.banner-warn h3 { margin:0 0 6px; font-size:14px; color:var(--warn); }
.banner-warn ul { margin:6px 0 0; padding-left:20px; color:var(--text); }
.banner-warn code { background:#0d1117; padding:2px 5px; border-radius:3px; font-size:12px; color:var(--accent); }
.kpi-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:14px; }
.kpi { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:18px 20px; }
.kpi-label { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:0.1em; }
.kpi-value { font-size:28px; font-weight:700; margin:8px 0 4px; font-variant-numeric:tabular-nums; letter-spacing:-0.02em; }
.kpi-sub { font-size:12px; color:var(--muted); }
.kpi-sub.up { color:var(--good); }
.kpi-sub.down { color:var(--claude); }

.chart-card { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:22px 26px; margin:20px 0; }
.chart-card h3 { margin:0 0 4px; font-size:16px; }
.chart-card .axis-note { font-size:11px; color:var(--muted); margin:0 0 14px; font-style:italic; }
.chart-card .axis-note b { color:var(--accent); font-style:normal; }
.chart-box { position:relative; height:380px; }

.method-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(360px,1fr)); gap:16px; }
.method-card { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:22px 24px; }
.method-card h3 { margin:0 0 6px; font-size:16px; display:flex; align-items:center; gap:8px; }
.method-card .freq { font-size:11px; color:var(--muted); background:#1f2a38; padding:2px 8px; border-radius:10px; font-weight:500; }
.method-card .source-line { font-size:12px; color:var(--muted); margin:0 0 14px; }
.method-card .source-line code { background:#0d1117; padding:2px 6px; border-radius:3px; font-size:11px; color:var(--accent); }
.method-card dl { margin:0; font-size:13px; }
.method-card dt { color:var(--text); font-weight:600; margin-top:14px; }
.method-card dt:first-child { margin-top:0; }
.method-card dd { margin:6px 0 0; color:var(--muted); padding-left:0; }
.method-card .formula { background:#0d1117; border:1px solid var(--border); border-radius:5px; padding:10px 14px; font-family:'SF Mono',Consolas,monospace; font-size:12px; color:var(--text); margin:8px 0 0; overflow-x:auto; }
.method-card .caveat { background:rgba(240,180,0,0.08); border-left:3px solid var(--warn); padding:10px 14px; border-radius:4px; font-size:12px; margin-top:14px; }
.method-card .caveat b { color:var(--warn); }

.dash-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:14px; }
.dash-card { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:18px 22px; transition:border-color 0.15s, transform 0.15s; }
.dash-card:hover { border-color:var(--accent); transform:translateY(-2px); }
.dash-card a { color:inherit; text-decoration:none; display:block; }
.dash-card .arrow { color:var(--accent); font-size:14px; }
.dash-card h4 { margin:0 0 6px; font-size:15px; }
.dash-card p { margin:0; font-size:12px; color:var(--muted); }

footer { margin-top:64px; padding-top:24px; border-top:1px solid var(--border); font-size:12px; color:var(--muted); display:flex; justify-content:space-between; flex-wrap:wrap; gap:12px; }
footer a { color:var(--accent); text-decoration:none; }
footer a:hover { text-decoration:underline; }

@media (max-width:640px) {
  h1 { font-size:24px; }
  .chart-box { height:300px; }
}
</style>
</head>
<body>
<div class="wrap">

<header>
  <h1>📈 AI Coding 工具采用度跟踪</h1>
  <p class="subtitle">每日跟踪 Claude Code / Codex / Cursor / Copilot 的真实使用信号 — 数据更新于 <span class="gen">__GEN__</span></p>
</header>

__BANNER__

<section class="section">
  <h3 class="section-title">最新数据</h3>
  <div class="kpi-grid">
    <div class="kpi">
      <div class="kpi-label">⭐ Anthropic / OpenAI SDK 比值</div>
      <div class="kpi-value" style="color:var(--good)">__SDK_RATIO__</div>
      <div class="kpi-sub">不受 Codex 分发口径变更影响 · 周比值</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Claude Code 周 commit</div>
      <div class="kpi-value" style="color:var(--claude)">__C_CLAUDE__</div>
      <div class="kpi-sub __C_CLAUDE_DIR__">__C_CLAUDE_WOW__ · 截至 __C_WEEK__</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Codex 周 commit</div>
      <div class="kpi-value" style="color:var(--codex)">__C_CODEX__</div>
      <div class="kpi-sub __C_CODEX_DIR__">__C_CODEX_WOW__ · 截至 __C_WEEK__</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">GitHub Stars 周新增（CC vs Codex）</div>
      <div class="kpi-value" style="color:var(--accent)">__GH_DELTA__</div>
      <div class="kpi-sub">__GH_TOTALS__</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">OpenRouter 当日 token 总量</div>
      <div class="kpi-value" style="color:var(--accent)">__OR_TOTAL__</div>
      <div class="kpi-sub">截至 __OR_DATE__ · 跨所有模型</div>
    </div>
    <div class="kpi" title="演示了 Codex 4/30 之后的 npm 跳变；保留作上下文，但不再作头条">
      <div class="kpi-label">@anthropic-ai/claude-code 周下载 <span style="color:var(--warn)">⚠</span></div>
      <div class="kpi-value" style="color:var(--claude)">__N_CCODE__</div>
      <div class="kpi-sub __N_CCODE_DIR__">__N_CCODE_WOW__ · 截至 __N_WEEK__ · npm 口径变更，仅作参考</div>
    </div>
  </div>
</section>

<section class="section">
  <h3 class="section-title">主图 · 周 commit 量对比</h3>
  <div class="chart-card">
    <h3>AI Coding 工具周 GitHub commit 量趋势</h3>
    <p class="axis-note">⚠ 量级差 ~200x，使用<b>双轴</b>：<b>左轴</b>（橙色刻度）= Claude Code（百万级）；<b>右轴</b>（蓝色刻度）= Codex / Copilot / Cursor / Aider / Devin（千级）。X 轴为周结束日。</p>
    <div class="chart-box"><canvas id="chartCommits"></canvas></div>
  </div>
</section>

<section class="section">
  <h3 class="section-title">主图 · npm 周下载量</h3>
  <div class="chart-card">
    <h3>4 个核心 npm 包周下载量趋势</h3>
    <p class="axis-note"><b>单轴</b>（百万下载/周）—— 4 个包量级相近所以单轴足够。X 轴为周结束日。</p>
    <div class="chart-box"><canvas id="chartNpm"></canvas></div>
  </div>
</section>

<section class="section">
  <h3 class="section-title">OpenRouter 周排行榜（实时镜像）</h3>
  <div class="chart-card">
    <h3>Top 模型 · 周累计 token（口径与 <a href="https://openrouter.ai/rankings" target="_blank" style="color:var(--accent)">openrouter.ai/rankings</a> 一致）</h3>
    <p class="axis-note"><b>每次构建从 OpenRouter 实时拉取</b>。OpenRouter 把"周累计 token"打上一个日期戳（通常是昨天 / 前天），代表过去 7 天的累积用量。下面表格直接镜像该口径，<b>避免与堆叠图日数据混淆</b>。</p>
    <div style="overflow-x:auto">__OR_LEADERBOARD__</div>
  </div>
</section>

<section class="section">
  <h3 class="section-title">主图 · OpenRouter 提供商 token 份额</h3>
  <div class="chart-card">
    <h3>各 AI 提供商每日 token 用量（堆叠面积图）</h3>
    <p class="axis-note"><b>单轴</b>（10 亿 token/天）—— 堆叠展示份额变化。X 轴为日期，最近 90 天。<b>注意</b>：本图基于 per-model 详情页抓取的 daily 数据，主要捕获 <code>standard</code> 变体。<code>free</code> 变体（如 Tencent Hy3 Preview 在 free tier 的高流量）在此图中不可见，请配合上方周排行榜查看完整对比。</p>
    <div class="chart-box"><canvas id="chartOR"></canvas></div>
  </div>
</section>

<section class="section">
  <h3 class="section-title">指标方法学 · 每个数字怎么算的</h3>
  <p class="lead">下面每张卡说明：数据从哪来、用什么 API、计算公式是什么、有什么局限。投资判断请配合多源验证。</p>
  <div class="method-grid">

    <div class="method-card">
      <h3>① GitHub Commit 信号 <span class="freq">每周</span></h3>
      <p class="source-line">数据源 · <code>api.github.com/search/commits</code> + <code>data.gharchive.org</code></p>
      <dl>
        <dt>测什么</dt>
        <dd>每周由各 AI Coding 工具 push 的 commit 数 — 反映"实际被开发者用来推代码"的强信号。</dd>
        <dt>怎么识别"是这个工具推的"</dt>
        <dd>每个工具 push 时会在 commit message 里加一行 <code>Co-Authored-By: ...&lt;email&gt;</code>。我们用工具特有的邮箱精确匹配，不靠文本搜索（避免 "Codex" 误匹配同名项目）：</dd>
        <dd><div class="formula">claude   = "noreply@anthropic.com"
codex    = "noreply@openai.com"
cursor   = "cursoragent@cursor.com"
aider    = Co-Authored-By: Aider
devin    = "devin-ai-integration[bot]"
copilot  = "198982749+Copilot"</div></dd>
        <dt>计算公式</dt>
        <dd>对每个工具的邮箱模式调一次 GitHub Search Commits API，加 <code>committer-date:start..end</code> 限制，取响应里的 <code>total_count</code>。<b>不分页</b>（直接拿真实总数，不受 1000 条结果上限影响）。</dd>
        <dt>另外两个字段</dt>
        <dd><b>total_pushes</b>: 该周全 GitHub PushEvent 总数（来自 GH Archive 公开数据集，每小时一份 JSON）。<br><b>distinct_devs</b>: 该周不重复的 commit author 数。两者用作分母计算"AI commit 占比"。</dd>
        <dt>更新频率</dt>
        <dd>每周一抓上一个完整周（周一~周日 UTC）。</dd>
        <div class="caveat"><b>局限</b>：① 只统计 push 到 GitHub 公开 repo 的；私有仓库、GitLab、Bitbucket 不在内。② Aider 用文本匹配（没固定邮箱），可能略偏高。③ Copilot 的邮箱模式仅匹配 Copilot Coding Agent 自动提交，IDE 内 inline 完成不在内。</div>
      </dl>
    </div>

    <div class="method-card">
      <h3>② npm 包周下载量 <span class="freq">每周</span></h3>
      <p class="source-line">数据源 · <code>api.npmjs.org/downloads/range</code></p>
      <dl>
        <dt>测什么</dt>
        <dd>4 个核心 AI Coding npm 包的全球周下载量 — 反映工具/SDK 的"安装频次"信号。</dd>
        <dt>跟踪哪些包</dt>
        <dd><div class="formula">@anthropic-ai/claude-code   ← Claude Code CLI
@anthropic-ai/sdk           ← Claude API SDK
@openai/codex               ← OpenAI Codex CLI
openai                      ← OpenAI JS SDK (基线)</div></dd>
        <dt>计算公式</dt>
        <dd>对每个包调 <code>https://api.npmjs.org/downloads/range/{week_start}:{week_end}/{pkg}</code>，把返回的每日 <code>downloads</code> 数组求和。npm 的接口返回每日数据，我们聚合成周。</dd>
        <dt>更新频率</dt>
        <dd>每周一抓上一个完整周。npm 数据本身延迟约 1 天。</dd>
        <div class="caveat"><b>局限</b>：① npm download 数包含 CI/CD 缓存重新拉取，<b>不等于活跃用户数</b>，但量级 WoW 趋势仍有意义。② 不区分 stable/beta tag。③ Cursor / GitHub Copilot 没发 npm 包所以不在表里 — 他们的信号在 commit 表里。</div>
      </dl>
    </div>

    <div class="method-card">
      <h3>③ OpenRouter Token 用量 <span class="freq">每日</span></h3>
      <p class="source-line">数据源 · 抓 <code>openrouter.ai/{model_slug}</code> 模型页内嵌 Next.js Flight payload</p>
      <dl>
        <dt>测什么</dt>
        <dd>每个模型每天通过 OpenRouter 路由消耗的 token 量 — 反映"真实推理消耗"的强信号，比下载量更接近"用了多少"。</dd>
        <dt>怎么抓</dt>
        <dd>OpenRouter 的模型页（如 <code>/anthropic/claude-opus-4.7</code>）服务端嵌入了完整 <code>rankingData</code> 数组，里面是按日聚合的 token 字段。我们 GET 每个跟踪模型的页面 → 用正则提取 JSON → 解析每天的：</dd>
        <dd><div class="formula">tokens_prompt        ← 输入 token
tokens_completion    ← 输出 token
tokens_reasoning     ← extended thinking token
tokens_cached        ← 命中 prompt cache
requests             ← 请求数
tool_calls           ← agentic 工作量</div></dd>
        <dt>landing 页主图怎么算</dt>
        <dd>按 <code>author</code>（提供商）汇总 <code>tokens_prompt + tokens_completion</code>，得到每天每提供商的总 token 量。堆叠面积图展示份额变化。</dd>
        <dt>更新频率</dt>
        <dd>每天 UTC 凌晨 1 点抓一次（约 200 个跟踪模型 × 每天 1 次请求）。</dd>
        <div class="caveat"><b>局限</b>：① <b>OpenRouter ≠ 全市场</b> — Anthropic / OpenAI 自家 API 直连企业流量看不到。OpenRouter 偏开发者 / 长尾路由场景。② 量级解读：OpenRouter 全平台日 token ~万亿级，约是 Anthropic API 全部量的 5-10%。③ 历史已迁移过一次格式（2026-04-24 之前抓的是 <code>/rankings</code> 周滚动榜，现在抓模型页的真实日数据）。</div>
      </dl>
    </div>

    <div class="method-card">
      <h3>④ 衍生指标 · 工具间相对增速 <span class="freq">每周</span></h3>
      <p class="source-line">从 ① 派生 · 见详细 dashboard</p>
      <dl>
        <dt>WoW (Week-over-Week) 增速</dt>
        <dd><div class="formula">WoW% = (本周值 - 上周值) / 上周值 × 100%</div></dd>
        <dt>分母选择</dt>
        <dd>用 <b>上一个完整周</b>（不用 7-day rolling），对应 GH Archive / npm 的发布节奏。</dd>
        <dt>占比</dt>
        <dd>"Claude commit / 全 GitHub commit" 不直接报告 — 因为 GH Archive 抓出来的 total_pushes 是 PushEvent 数（一个 push 可能含多个 commit），分母含义不同。所以仅在 unified dashboard 里给参考占比。</dd>
        <dt>背离信号</dt>
        <dd>当 npm 下载量 WoW &gt; commit 量 WoW 的 1.5x，或反过来时，标记"divergence"。可能含义：① npm 涨快 commit 慢 → 新装但没真正用；② commit 涨快 npm 慢 → 老用户更深度使用。详见合并 dashboard。</dd>
      </dl>
    </div>

  </div>
</section>

<section class="section">
  <h3 class="section-title">深度 dashboard</h3>
  <p class="lead">每个 tracker 的完整时间序列、模型 / 工具维度细分、release 事件叠加。</p>
  <div class="dash-grid">
    <div class="dash-card"><a href="dashboards/openrouter.html">
      <h4>OpenRouter Usage <span class="arrow">→</span></h4>
      <p>每日 token 用量、模型排名、release 吸收曲线</p>
    </a></div>
    <div class="dash-card"><a href="dashboards/commits.html">
      <h4>GitHub Commits <span class="arrow">→</span></h4>
      <p>6 个工具周 commit 趋势、AI commit 占比</p>
    </a></div>
    <div class="dash-card"><a href="dashboards/npm.html">
      <h4>npm + PyPI Downloads <span class="arrow">→</span></h4>
      <p>4 个 npm 包 + 3 个 PyPI 包的周下载量</p>
    </a></div>
    <div class="dash-card"><a href="dashboards/unified.html">
      <h4>Unified View <span class="arrow">→</span></h4>
      <p>commits × npm 合并视图 + 背离信号</p>
    </a></div>
  </div>
</section>

<footer>
  <div>数据来源公开 API · 更新于 <span class="gen">__GEN__</span></div>
  <div><a href="https://github.com/" target="_blank">源码 GitHub</a> · <a href="dashboards/unified.html">合并视图</a></div>
</footer>

</div>

<script id="payload" type="application/json">__DATA__</script>
<script>
(function(){
  const D = JSON.parse(document.getElementById('payload').textContent);
  const FONT = "-apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif";
  Chart.defaults.color = '#8b949e';
  Chart.defaults.font.family = FONT;
  Chart.defaults.borderColor = '#30363d';

  const COLORS = {
    claude:'#f85149', codex:'#3fb950', cursor:'#58a6ff',
    copilot:'#a371f7', aider:'#d97706', devin:'#ec4899',
  };

  // Chart 1: dual-axis commits
  if (D.commits.labels.length > 0) {
    new Chart(document.getElementById('chartCommits'), {
      type:'line',
      data:{
        labels:D.commits.labels,
        datasets:[
          {label:'Claude Code（左轴）',data:D.commits.series.claude,borderColor:COLORS.claude,backgroundColor:'rgba(248,81,73,0.1)',yAxisID:'y1',tension:0.25,fill:false,borderWidth:2.5},
          {label:'Codex（右轴）',data:D.commits.series.codex,borderColor:COLORS.codex,yAxisID:'y2',tension:0.25,borderWidth:1.8},
          {label:'GitHub Copilot（右轴）',data:D.commits.series.copilot,borderColor:COLORS.copilot,yAxisID:'y2',tension:0.25,borderWidth:1.8},
          {label:'Cursor（右轴）',data:D.commits.series.cursor,borderColor:COLORS.cursor,yAxisID:'y2',tension:0.25,borderWidth:1.8},
          {label:'Aider（右轴）',data:D.commits.series.aider,borderColor:COLORS.aider,yAxisID:'y2',tension:0.25,borderWidth:1.8},
          {label:'Devin（右轴）',data:D.commits.series.devin,borderColor:COLORS.devin,yAxisID:'y2',tension:0.25,borderWidth:1.8},
        ],
      },
      options:{
        responsive:true,maintainAspectRatio:false,
        interaction:{mode:'index',intersect:false},
        scales:{
          y1:{type:'linear',position:'left',title:{display:true,text:'左轴 · Claude Code commits（百万 / 周）',color:COLORS.claude,font:{size:12,weight:'600'}},
            ticks:{color:COLORS.claude,callback:v=>(v/1e6).toFixed(1)+'M'},grid:{color:'rgba(248,81,73,0.08)'}},
          y2:{type:'linear',position:'right',title:{display:true,text:'右轴 · Codex / Copilot / Cursor / Aider / Devin（千 / 周）',color:'#58a6ff',font:{size:12,weight:'600'}},
            ticks:{color:'#58a6ff',callback:v=>(v/1e3).toFixed(0)+'K'},grid:{drawOnChartArea:false}},
          x:{title:{display:true,text:'X 轴 · 周结束日（UTC）',color:'#8b949e',font:{size:11}}},
        },
        plugins:{
          legend:{position:'bottom',labels:{boxWidth:14,padding:14}},
          tooltip:{callbacks:{label:ctx=>{const v=ctx.parsed.y;const lab=ctx.dataset.label;return `${lab}: ${v>=1e6?(v/1e6).toFixed(2)+'M':(v/1e3).toFixed(1)+'K'}`;}}},
        },
      },
    });
  }

  // Chart 2: npm single axis
  if (D.npm.labels.length > 0) {
    new Chart(document.getElementById('chartNpm'), {
      type:'line',
      data:{
        labels:D.npm.labels,
        datasets:[
          {label:'@anthropic-ai/claude-code',data:D.npm.series['npm_claude-code'],borderColor:COLORS.claude,tension:0.25,borderWidth:2.2},
          {label:'@anthropic-ai/sdk',data:D.npm.series['npm_anthropic-sdk'],borderColor:COLORS.aider,tension:0.25,borderWidth:1.8},
          {label:'@openai/codex',data:D.npm.series['npm_openai-codex'],borderColor:COLORS.codex,tension:0.25,borderWidth:2.2},
          {label:'openai (JS SDK 基线)',data:D.npm.series['npm_openai-sdk'],borderColor:COLORS.cursor,tension:0.25,borderWidth:1.8},
        ],
      },
      options:{
        responsive:true,maintainAspectRatio:false,
        interaction:{mode:'index',intersect:false},
        scales:{
          y:{title:{display:true,text:'Y 轴 · 下载量（百万 / 周）',color:'#8b949e',font:{size:12,weight:'600'}},
            ticks:{callback:v=>(v/1e6).toFixed(1)+'M'}},
          x:{title:{display:true,text:'X 轴 · 周结束日（UTC）',color:'#8b949e',font:{size:11}}},
        },
        plugins:{
          legend:{position:'bottom',labels:{boxWidth:14,padding:14}},
          annotation:{annotations:{regime:{
            type:'line',
            xMin:D.npm.codex_regime_break,xMax:D.npm.codex_regime_break,
            borderColor:'#f0b400',borderWidth:2,borderDash:[6,4],
            label:{display:true,
              content:'Codex 分发口径变更（per-platform binary）',
              position:'start',color:'#f0b400',backgroundColor:'#0d1117',
              font:{size:10}}
          }}},
        },
      },
    });
  }

  // Chart 3: openrouter stacked area
  if (D.openrouter.labels.length > 0) {
    const SRS = D.openrouter.series;
    const PROV_COLORS = {google:'#4285f4',anthropic:'#f85149',openai:'#10a37f','x-ai':'#e5e7eb',deepseek:'#6e40c9','moonshotai':'#fbbf24',minimax:'#ec4899',qwen:'#f97316','z-ai':'#06b6d4',stepfun:'#84cc16','meta-llama':'#0668e1','其他':'#8b949e'};
    const datasets = Object.keys(SRS).map(prov => ({
      label:prov,data:SRS[prov],
      borderColor:PROV_COLORS[prov]||'#8b949e',
      backgroundColor:(PROV_COLORS[prov]||'#8b949e')+'80',
      fill:true,tension:0.3,borderWidth:1.2,pointRadius:0,
    }));
    new Chart(document.getElementById('chartOR'), {
      type:'line',
      data:{labels:D.openrouter.labels,datasets},
      options:{
        responsive:true,maintainAspectRatio:false,
        interaction:{mode:'index',intersect:false},
        scales:{
          y:{stacked:true,title:{display:true,text:'Y 轴 · token 用量（10 亿 / 天，堆叠）',color:'#8b949e',font:{size:12,weight:'600'}},
            ticks:{callback:v=>(v/1e9).toFixed(0)+'B'}},
          x:{title:{display:true,text:'X 轴 · 日期（UTC）',color:'#8b949e',font:{size:11}}, ticks:{maxTicksLimit:12}},
        },
        plugins:{legend:{position:'bottom',labels:{boxWidth:14,padding:12}}},
      },
    });
  }
})();
</script>
</body>
</html>
"""

    def wow_class(curr, prev):
        if not curr or not prev:
            return ""
        return "up" if curr >= prev else "down"

    # Build warning banner. Three levels (concatenated when present):
    # (R) Codex npm regime break — high priority, visible above OpenRouter notes.
    # (1) ALWAYS show "data freshness" line: the chart cuts off at today-2.
    # (2) If anomaly detector flagged a suspect day, show that prominently.
    suspect = o.get("suspect", [])
    recently_dropped = o.get("recently_dropped", [])
    banner_html = ""

    # Regime-break alert — shown whenever the latest npm week shows precursor
    # inflation OR is fully post-boundary. Codex npm counts started
    # inflating the week ending 2026-04-26 (precursor of the 2026-04-30 switch
    # to per-platform binary distribution; weekly Codex went 3.1M → 5.8M, +82.7% WoW).
    n_latest = n.get("week_end") or ""
    PRECURSOR_THRESHOLD = "2026-04-19"
    if n_latest and n_latest >= PRECURSOR_THRESHOLD:
        banner_html += (
            '<div class="banner-warn">'
            "<h3>⚠ npm 计数口径变更（@openai/codex）</h3>"
            "<div>2026-04-30 起，<code>@openai/codex</code> 切换为 "
            "<b>per-platform 二进制分发</b>（79.4MB tarball，每日多次发布）。"
            "<code>api.npmjs.org/downloads/range</code> 在变更前/后计的"
            "不是同一个东西：</div>"
            "<ul>"
            "<li>变更前：CLI JS 包安装次数（受 CI/CD 缓存倍增）</li>"
            "<li>变更后：CLI JS + 平台二进制 tarball 各自计入 + 频繁版本"
            "刷新触发的 CI 重拉，同一用户被 5-50× 放大</li>"
            "</ul>"
            "<div>因此 npm 图表在 4/30 处的"
            "<b>跳变非真实流量爆发</b>。已用 ⚠ 黄色虚线在 npm 主图标注。"
            "看竞争对比请优先参考 <b>SDK 比值</b>（Anthropic SDK ÷ OpenAI SDK）"
            "和 <b>GitHub commits</b> — 它们都不受该事件影响。</div>"
            "</div>"
        )

    if suspect or recently_dropped:
        items = []
        for s in suspect:
            items.append(
                f"<li><b>{s['date']}</b>: 实际 <code>{s['total_b']:.0f}B tokens</code> · "
                f"前 7 天中位数 <code>{s['median_b']:.0f}B</code> · "
                f"比值 {s['total_b'] / s['median_b'] * 100:.0f}% — 采集不全，已剔除</li>"
            )
        if recently_dropped and not suspect:
            cutoff_d = recently_dropped[0]
            last_d = recently_dropped[-1]
            items.append(
                f"<li><b>{cutoff_d} ~ {last_d}</b>："
                "OpenRouter 在前 7-14 天会持续回填 BYOK / 日志数据，"
                "最近 2 天数字尚未结算 — 默认从图表剔除以避免误显"
                "下行趋势</li>"
            )
        banner_html += (
            '<div class="banner-warn">'
            "<h3>⚠ 数据新鲜度声明</h3>"
            "<div>所有日级图表只显示<b>截至前天</b>的数据（today − 2）。"
            "今天 + 昨天因 OpenRouter 后台仍在回填，<b>不展示</b>以避免"
            "误读：</div>"
            f"<ul>{''.join(items)}</ul>"
            "</div>"
        )

    # SDK ratio: anthropic-sdk / openai-sdk (last week's npm downloads).
    sdk_anthropic = n.get("npm_anthropic-sdk") or 0
    sdk_openai = n.get("npm_openai-sdk") or 0
    if sdk_anthropic and sdk_openai:
        sdk_ratio_text = f"{sdk_anthropic / sdk_openai * 100:.0f}%"
    else:
        sdk_ratio_text = "—"

    # GitHub stars KPI: weekly delta + current totals.
    gh = p.get("gh_stars") or {}
    cc_gh = gh.get("Claude Code") or {}
    cx_gh = gh.get("Codex") or {}
    cc_d = cc_gh.get("delta_7d")
    cx_d = cx_gh.get("delta_7d")
    if cc_d is not None and cx_d is not None:
        gh_delta_text = f"+{cc_d:,} vs +{cx_d:,}"
        gh_totals_text = (
            f"截至 {cc_gh.get('as_of', '—')} · "
            f"now {cc_gh.get('stars', 0):,} / {cx_gh.get('stars', 0):,}"
        )
    elif cc_gh.get("stars") and cx_gh.get("stars"):
        gh_delta_text = f"{cc_gh['stars'] / 1000:.1f}K vs {cx_gh['stars'] / 1000:.1f}K"
        gh_totals_text = (
            f"截至 {cc_gh.get('as_of', '—')} · 总数 · "
            "需 7 天积累 JSONL 历史以显示周新增"
        )
    else:
        gh_delta_text = "首次运行"
        gh_totals_text = "需 ~/.claude/data/github_stars.jsonl 数据"

    # OpenRouter weekly leaderboard table (mirrors openrouter.ai/rankings).
    lb = p.get("or_leaderboard") or {}
    AUTHOR_COLORS = {
        "anthropic": "var(--claude)",
        "openai": "#10a37f",
        "google": "#4285f4",
        "deepseek": "#6e40c9",
        "moonshotai": "#fbbf24",
        "tencent": "#06b6d4",
        "x-ai": "#e5e7eb",
        "qwen": "#f97316",
        "minimax": "#ec4899",
        "z-ai": "#22d3ee",
        "stepfun": "#84cc16",
        "meta-llama": "#0668e1",
    }
    if lb.get("models"):
        rows_html = []
        max_t = max(m["tokens_b"] for m in lb["models"]) or 1
        for i, m in enumerate(lb["models"], 1):
            color = AUTHOR_COLORS.get(m["author"], "var(--muted)")
            bar_pct = m["tokens_b"] / max_t * 100
            t_label = (
                f"{m['tokens_b'] / 1000:.2f}T"
                if m["tokens_b"] >= 1000
                else f"{m['tokens_b']:.0f}B"
            )
            v_tag = (
                f' <span style="color:var(--muted);font-size:11px">[{m["variant"]}]</span>'
                if m["variant"] not in ("standard", "")
                else ""
            )
            rows_html.append(
                f'<tr><td style="color:var(--muted);width:30px">{i}</td>'
                f'<td><b style="color:{color}">{m["permaslug"]}</b>{v_tag}</td>'
                f'<td class="r mono" style="white-space:nowrap">{t_label}</td>'
                f'<td style="width:35%">'
                f'<div style="background:#334155;border-radius:3px;height:14px;overflow:hidden">'
                f'<div style="height:14px;background:{color};width:{bar_pct:.1f}%;border-radius:3px"></div>'
                f"</div></td></tr>"
            )
        or_lb_html = (
            f'<p style="font-size:12px;color:var(--muted);margin:0 0 10px">'
            f'快照日期 <b style="color:var(--text)">{lb.get("as_of", "—")}</b> · '
            f"{lb.get('note', '')}</p>"
            '<table style="width:100%;border-collapse:collapse;font-size:13px">'
            '<thead><tr><th style="text-align:left;padding:6px;color:var(--muted);'
            'font-size:11px;text-transform:uppercase">#</th>'
            '<th style="text-align:left;padding:6px;color:var(--muted);font-size:11px;'
            'text-transform:uppercase">Model</th>'
            '<th style="text-align:right;padding:6px;color:var(--muted);font-size:11px;'
            'text-transform:uppercase">Weekly Tokens</th>'
            '<th style="padding:6px;color:var(--muted);font-size:11px;'
            'text-transform:uppercase">Distribution</th></tr></thead><tbody>'
            + "".join(rows_html)
            + "</tbody></table>"
        )
    else:
        or_lb_html = (
            '<p style="color:var(--muted);font-size:13px;padding:20px 0">'
            "OpenRouter /rankings fetch failed at build time — leaderboard unavailable. "
            "Daily chart below still reflects per-model-page data.</p>"
        )

    replacements = {
        "__BANNER__": banner_html,
        "__OR_LEADERBOARD__": or_lb_html,
        "__SDK_RATIO__": sdk_ratio_text,
        "__GH_DELTA__": gh_delta_text,
        "__GH_TOTALS__": gh_totals_text,
        "__GEN__": gen,
        "__C_WEEK__": c.get("week_end") or "—",
        "__N_WEEK__": n.get("week_end") or "—",
        "__OR_DATE__": o.get("latest_date") or "—",
        "__C_CLAUDE__": _fmt(c.get("claude")),
        "__C_CLAUDE_WOW__": _wow(c.get("claude"), cprev.get("claude")),
        "__C_CLAUDE_DIR__": wow_class(c.get("claude"), cprev.get("claude")),
        "__C_CODEX__": _fmt(c.get("codex")),
        "__C_CODEX_WOW__": _wow(c.get("codex"), cprev.get("codex")),
        "__C_CODEX_DIR__": wow_class(c.get("codex"), cprev.get("codex")),
        "__N_CCODE__": _fmt(n.get("npm_claude-code")),
        "__N_CCODE_WOW__": _wow(n.get("npm_claude-code"), nprev.get("npm_claude-code")),
        "__N_CCODE_DIR__": wow_class(
            n.get("npm_claude-code"), nprev.get("npm_claude-code")
        ),
        "__OR_TOTAL__": f"{o.get('today_total_t', 0):.2f}T",
        "__DATA__": data_blob,
    }
    out = template
    for k, v in replacements.items():
        out = out.replace(k, str(v))
    return out


if __name__ == "__main__":
    build()
