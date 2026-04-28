# AI Coding 工具采用度跟踪

每日跟踪 Claude Code / Codex / Cursor / Copilot 等 AI Coding 工具的真实使用信号。

**Live**: https://robinlllll.github.io/ai-coding-adoption/

## 数据源

| 指标 | 频率 | 数据来源 |
|---|---|---|
| GitHub Commit 信号 | 每周 | `api.github.com/search/commits` + GH Archive |
| npm 包周下载量 | 每周 | `api.npmjs.org/downloads/range` |
| OpenRouter Token 用量 | 每天 | 抓 `openrouter.ai/{model_slug}` 模型页内嵌 Next.js Flight payload |

## 指标如何计算

### ① Commit 信号 — 谁推了 commit

每个 AI Coding 工具 push 时会在 commit message 里加一行 `Co-Authored-By: ...<email>`。
按工具特有邮箱精确匹配（不靠文本搜索，避免 "Codex" 误匹配同名项目）：

```
claude   = "noreply@anthropic.com"
codex    = "noreply@openai.com"
cursor   = "cursoragent@cursor.com"
aider    = Co-Authored-By: Aider
devin    = "devin-ai-integration[bot]"
copilot  = "198982749+Copilot"
```

每个工具调一次 `GET /search/commits?q=...&committer-date:start..end`，取响应里的 `total_count`（拿真实总数，不分页）。

### ② npm 周下载量

```
@anthropic-ai/claude-code   ← Claude Code CLI
@anthropic-ai/sdk           ← Claude API SDK
@openai/codex               ← OpenAI Codex CLI
openai                      ← OpenAI JS SDK (基线)
```

每个包调 `https://api.npmjs.org/downloads/range/{week_start}:{week_end}/{pkg}`，把返回的每日 downloads 数组求和。

### ③ OpenRouter Token 用量

OpenRouter 的模型页（如 `/anthropic/claude-opus-4.7`）服务端嵌入了完整 `rankingData` 数组，包含按日聚合的 token 字段。我们 GET 每个跟踪模型的页面 → 用正则提取 JSON → 解析每天的 6 类 token：

```
tokens_prompt        ← 输入 token
tokens_completion    ← 输出 token
tokens_reasoning     ← extended thinking token
tokens_cached        ← 命中 prompt cache
requests             ← 请求数
tool_calls           ← agentic 工作量
```

按 `author`（提供商）汇总 `tokens_prompt + tokens_completion`，得到每天每提供商的总 token 量。

### 局限性

- **GitHub commit 只统计 push 到公开 repo 的**：私有仓库、GitLab、Bitbucket 不在内。
- **npm download 含 CI/CD 缓存重新拉取**：不等于活跃用户数，但 WoW 趋势仍有意义。
- **OpenRouter ≠ 全市场**：Anthropic / OpenAI 自家 API 直连企业流量看不到，OpenRouter 偏开发者 / 长尾路由场景。
- **OpenRouter 数据有 7-14 天回填窗口**：BYOK / 日志聚合延迟会让前几天的数字小幅上调。我们的 scraper 在 14 天窗口内无条件刷新。

## 自动更新机制

| 时间 | 任务 |
|---|---|
| 每天 01:00 | scrape OpenRouter |
| 每天 02:30 | rebuild landing page + push to GitHub Pages |
| 每周六 09:00 | 更新 GitHub commit 数据 + 立即 rebuild |
| 每周六 09:30 | 更新 npm/PyPI 数据 + 立即 rebuild |

## 本地构建

```bash
python build.py
# 生成 index.html, data/latest.json, dashboards/*.html
```

需要本地有 `~/.claude/data/trackers/{openrouter_model_share,claude_code_adoption,npm_pypi_downloads}/*.jsonl`（这些通过对应 scraper 脚本自动生成）。

## 许可

MIT
