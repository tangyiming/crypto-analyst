# Crypto Analyst

> 📊 AI 行情分析、预测与结果验证 + U 本位永续实时盯盘（规则告警 / Telegram）

拉取多周期数据 → **AI 给出观点与计划** → 到期后对照真实 K 线做验证；同时可用 Web 常驻规则引擎盯盘推送。

入口文档就是本 README；配置见 `.env.example`。

---

## 它解决什么问题

- 结构化地做 **技术面 + 周期语境** 下的观点输出  
- **同一套提示词与数据快照**，便于对照「当时怎么说、后来怎么走」  
- 验证环节把 **预测与现实** 对齐；实时规则提醒只告警、不下单  

**当前主路径：**

```
Web 盯盘（常驻推 TG） ↔ 选币分析落库 →（可选）到期验证 → 历史会话复盘
```

---

## 环境要求

- **Python 3.11+**（推荐 3.11 或 3.12）
- 能访问 **Binance** 行情；LLM 需自备 API Key

---

## 安装

在**本仓库根目录**（含 `pyproject.toml`）执行：

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -e .                   # CLI + 核心
pip install -e ".[dev]"            # 开发（pytest 等）
pip install -e ".[web]"            # Web（FastAPI + uvicorn）
```

安装后可用命令 **`analyst`**。

---

## 配置（.env）

```bash
cp .env.example .env
# 至少配置一种 LLM；盯盘推送再配 TELEGRAM_* 与 MONITOR_ALWAYS_ON
```

- **DeepSeek**：`DEEPSEEK_API_KEY`、`LLM_PROVIDER=deepseek` 等，详见 `.env.example`  
- 连通性：`analyst config test-llm`  
- 首次：`analyst db init`

---

## 运行方法

请在**项目根目录**启动（与 `analyst.db`、`.env` 同级）。

### Web（推荐）

```bash
analyst web
# 或 ./scripts/run-web.sh
```

默认 **`http://127.0.0.1:8000`**。

首页：U 本位图表 + AI 分析/对话 + 规则告警。  
关网页也要推 TG：`.env` 设 `MONITOR_ALWAYS_ON=true`，保持 web 进程在跑。  
多级别：`MONITOR_DAEMON_TIMEFRAMES=15m,1h,4h`；品种：`MONITOR_DAEMON_SYMBOLS=...` 或页面观察列表。

### CLI 监控 / 分析

```bash
analyst monitor once BTC -t 15m
analyst monitor start BTC -t 15m
analyst practice BTC
analyst verify
analyst history
```

---

## 本地持久化说明

| 数据 | 是否落盘 | 位置 / 说明 |
|------|----------|-------------|
| AI 会话、计划、验证、聊天 | **要** | `analyst.db`（SQLite）——复盘与验证依赖 |
| 常驻盯盘品种列表 | **要** | `.cache/data/monitor_daemon.json` |
| REST K 线 / 衍生品短缓存 | **可选** | `.cache/data/`（diskcache，TTL 几分钟，加速拉数） |
| **实时 WS K 线** | **不用持久化** | 内存滚动窗口即可；历史随时 REST 回补，落库只会膨胀且难维护 |
| 规则告警历史 | **目前内存** | Hub 里约 200 条；要长久复盘可再写入 SQLite（未做） |
| 浏览器观察列表 / UI 偏好 | localStorage | 前端本地；常驻开关打开时会 sync 到 daemon 文件 |

**结论：** 实时收到的 K 线**不必**本地永久存；该存的是「决策结果」（会话/告警/配置），不是 tick 流水。

---

## 开发与测试

```bash
pip install -e ".[dev]"
pytest tests/ -q
```

---

## 支持的 LLM Provider

通过 **`.env` 切换**：DeepSeek（默认示例）、b.ai、Anthropic、Groq、OpenRouter、Ollama、OpenAI。键名以 `.env.example` 为准。

---

## 核心命令

| 命令 | 作用 |
|---|---|
| `analyst web` | Web + 常驻盯盘 |
| `analyst practice <symbol>` | 创建会话并跑 AI |
| `analyst verify` | 验证已到期会话 |
| `analyst progress` / `weakness` / `ai-benchmark` | 统计 |
| `analyst history` / `review <id>` | 历史与复盘 |
| `analyst config test-llm` | LLM 连通 |
| `analyst db init` | 初始化 SQLite |

---

## 目录结构

```
crypto-analyst/
├── README.md
├── pyproject.toml
├── .env.example
├── prompts/               # LLM 提示词版本
├── scripts/run-web.sh
├── src/analyst/           # 包名 analyst
└── tests/
```

本地生成（已 gitignore）：`analyst.db`、`.cache/`、`.env`、`.venv/`。

---

## 说明

- **不自动下单**；交易决策由你本人负责。  
- 改源码后需重启 Web 进程。  
- 方法论补充材料（若有）见 monorepo 下 `methodology/`。
