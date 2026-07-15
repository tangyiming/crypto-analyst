# Crypto Analyst

本地跑的 **AI 行情分析 + U 本位永续盯盘**。AI 给出观点/计划并落库复盘；规则引擎常驻盯盘，命中后推 Telegram——**只提醒，不下单**。

<p align="center">
  <img src="docs/images/web-dashboard.png" alt="Web 盯盘与 AI 分析" width="900" />
</p>
<p align="center"><em>Web：K 线与计划线 · AI 侧栏 · 历史会话</em></p>

<p align="center">
  <img src="docs/images/telegram-alert.png" alt="Telegram 告警" width="420" />
</p>
<p align="center"><em>Telegram：双线反转等规则告警（入场 / SL / TP / Kelly）</em></p>

---

## 两条能力

| | AI 分析 | 实时盯盘 |
|---|---|---|
| **做什么** | 拉多周期数据 → LLM 出观点与计划 → 到期对照 K 线验证 | 规则扫形态（如双线反转）→ Web / Telegram 提醒 |
| **入口** | Web 右侧「AI 行情分析」，或 `analyst practice` | 打开 Web；开常驻后关页面也推 TG |
| **数据** | 会话写入 `analyst.db` | 观察列表 + 告警；K 线本身不长期落库 |

```
盯盘推送（Web / TG）  ←→  选币做 AI 分析落库  →  到期验证  →  历史复盘
```

---

## 5 分钟上手

**1. 安装**（项目根目录）

```bash
uv sync --extra web
# 或：python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[web]"
```

**2. 配置**

```bash
cp .env.example .env
```

编辑 `.env`，至少填一个 LLM（示例默认 DeepSeek）：

```bash
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-你的key
```

要推 Telegram 再加，并打开常驻：

```bash
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
MONITOR_ALWAYS_ON=true
```

初始化库并自检：

```bash
analyst db init
analyst config test-llm
```

**3. 启动 Web**

```bash
./scripts/run-web.sh
# 等价：analyst web
```

打开 **http://127.0.0.1:8000**。改代码后重新跑脚本即可（会先释放端口再启动）。

---

## 配置速查

详细注释见 [`.env.example`](.env.example)。常用项：

| 变量 | 作用 |
|------|------|
| `DEEPSEEK_API_KEY` / `LLM_*` | 主分析线路（还可切 b.ai / Anthropic / Groq 等） |
| `DEFAULT_SYMBOLS` | 默认观察列表；常驻品种未单独配置时也用这份 |
| `MONITOR_ALWAYS_ON` | `true`：Web 进程在跑时关页面也继续盯盘并推 TG |
| `MONITOR_DAEMON_TIMEFRAMES` | 常驻多周期，如 `15m,1h,4h` |
| `MONITOR_DAEMON_SYMBOLS` | 常驻品种；空则跟 `DEFAULT_SYMBOLS` / 页面观察列表 |
| `TELEGRAM_BOT_TOKEN` / `CHAT_ID` | 告警推送 |

Web **固定 U 本位永续**，无需再配「现货 / 合约」切换。

---

## CLI（可选）

```bash
analyst practice BTC          # 创建分析会话
analyst verify                # 验证已到期会话
analyst history               # 历史列表
analyst backtest BTC -t 15m   # 策略/规则历史回测
analyst monitor once BTC -t 15m
analyst monitor start BTC -t 15m
```

| 命令 | 作用 |
|------|------|
| `analyst web` | Web + 常驻盯盘 |
| `analyst practice <symbol>` | AI 分析并落库 |
| `analyst verify` | 验证到期会话 |
| `analyst backtest <symbol>` | 历史回放回测（策略胜率 + 规则命中率） |
| `analyst history` / `review <id>` | 历史 / 单条复盘 |
| `analyst progress` / `weakness` / `ai-benchmark` | 统计 |
| `analyst config test-llm` | LLM 连通 |
| `analyst db init` | 初始化 SQLite |

---

## 回测

用**和实时盯盘同一套评估代码**在历史 K 线上向前回放，量化告警质量：

```bash
analyst backtest BTC -t 15m --bars 1000        # 最近 1000 根 15m
analyst backtest SOL -t 1h --bars 1500 --json r.json   # 结果另存 JSON
analyst backtest ETH -t 4h --no-rules          # 只测策略，跳过规则统计
```

两条评估线：

| | 双线反转策略 | 规则告警 |
|---|---|---|
| **怎么测** | 逐根收盘回放，出信号即按计划模拟下单，逐根判定止损 / 止盈 / 超时（同根双触保守按止损） | 每条带方向的告警做 ATR 屏障前瞻：`--horizon` 根内先顺向走 1×ATR 算命中，先逆向算打脸 |
| **输出** | 胜率、累计 R、平均 R/笔、盈亏比 PF、最大回撤、逐笔明细 | 每条规则的样本数 / 命中率 / 平均前瞻收益 |

读数参考：规则命中率 ≈50% 说明单独使用无优势（只适合当上下文）；样本 < 10 结论不可靠；突破型策略在震荡段表现差属正常，换周期和趋势段多测几组再下结论。

---

## 本地会生成什么

| 路径 | 内容 |
|------|------|
| `analyst.db` | AI 会话、计划、验证、聊天 |
| `.cache/data/monitor_daemon.json` | 常驻盯盘品种（页面观察列表可同步过来） |
| `.cache/data/` | REST 短缓存（可删） |
| `.env` / `.venv/` | 本地密钥与虚拟环境（已 gitignore） |

实时 WS K 线只在内存滚动，**不**当历史库存。

---

## 开发

```bash
uv sync --extra web --extra dev
pytest tests/ -q
```

```
crypto-analyst/
├── docs/images/       # README 截图
├── prompts/           # LLM 提示词
├── scripts/run-web.sh
├── src/analyst/
└── tests/
```

---

## 说明

- **不自动下单**；盈亏与决策自负。
- 需能访问 Binance 行情；Python **3.11+**。
