# Crypto Analyst

本地跑的 **AI 行情分析 + U 本位永续盯盘 + 牛熊周期工具**。AI 给出观点/计划并落库复盘；规则引擎常驻盯盘，命中后推 Telegram——**只提醒，不下单**。

顶栏：**盯盘** → **周期** → **策略库** → **回测** → **开源框架** → **Web3** → **开发工具** → **自动交易**（纸面）。

---

## 纸面模拟炒币

顶栏 **「自动交易」** 现为纸面账本（**非真金**）：

- 跟单 **`ai_plan` / `double_line` / `cycle_switch`**（可用 `MONITOR_PAPER_SOURCES` 裁剪）
- 默认初始 **100 USDT**，单笔风险约权益 1%；开平各收手续费（默认 4 bps）
- `ai_plan` / 双线：标记价触止损 / 止盈 1 平仓；`cycle_switch`：按目标仓位同步，信号平仓
- 页面按策略汇总已实现 / 浮动盈亏，持仓约 5 秒刷新；可选 TG「📄 纸面…」
- 落盘：`.cache/data/paper_account.json`；API：`GET /api/paper/status` · `POST /api/paper/reset`

开启常驻盯盘（`MONITOR_ALWAYS_ON=true`）后，策略信号会自动记纸面仓（AI 确认仍走 `ai_plan`）。

---

## 能力一览

| | AI 分析 | 实时盯盘 | 周期与组合策略 |
|---|---|---|---|
| **做什么** | 多周期数据 + **波段锁点预计算** → LLM 出观点与计划 → 到期验证 | 双线反转 + 规则 + `cycle_switch`；候选信号可再调 AI 确认后推 TG | Wolfy 四年周期 + `cycle_switch`；经典策略长周期回测 |
| **入口** | Web 右侧「AI 行情分析」，或 `analyst practice` | 打开 Web；开常驻后关页面也推 TG | 顶栏「周期」；`analyst cycle-outlook` / `backtest-classic` |
| **数据** | 会话写入 `analyst.db`（含 `jack_levels`） | 观察列表 + 告警；K 线本身不长期落库 | BTC 日线定日历相位；组合回测分页拉 2–5 年历史 |

```
盯盘推送（Web / TG）  ←→  选币做 AI 分析（锁点注入）落库  →  到期验证  →  历史复盘
                              ↑
              周期图 / cycle_switch / 转折点倒计时 / Web3 头仓计算器
```

### 告警怎么推（简要）

| 类型 | 页面 | Telegram |
|------|------|----------|
| 双线 / 规则噪音（放量、触及等） | 有 | 默认不推（可改白名单） |
| 收盘有候选 → AI 确认 `long`/`short`（`ai_plan`） | 有 | 推 |
| 各币 `cycle_switch` 仓位变化 | 有（触发 AI 候选） | 不直推；等 AI 确认 |
| 周期位置日更（`cycle_outlook`，BTC） | 有 | UTC **每天最多 1 条** |

### Web 周期图

顶栏 **「周期」**（紧跟「盯盘」）进入四年周期专页（基于 BTC 日线）：

- **刻舟求剑日历**：牛 1064 天 / 熊 364 天，显示当前相位进度与下一转折点
- **转折点倒计时**：距预计牛顶 / 熊底还有多少天（≤30 天高亮）
- **时间轴色带**：历史牛熊分段 + 减半标记 + 价格背景折线
- **狼波动能**：RSI 分区（过热 / 超卖），与日历交叉确认

数据每 5 分钟自动刷新；与主图 WebSocket 独立，固定用 BTC 日线。

### Web 应用导航

| 页 | 内容 |
|----|------|
| 策略库 / 回测 | 本平台策略目录；经典组合回测与 CLI `backtest-classic` 同源 |
| 开源框架 | Freqtrade / Jesse / vectorbt 等调研与集成路线（侧车 dry-run，非整库内嵌） |
| Web3 | ETH 单位换算、地址/Hex 工具、**头仓/补仓计算器**、链上/DeFi 外链 |
| 开发工具 | JSON / Base64 / URL / 时间戳 / UUID + 常用在线工具外链 |

---

## 波段锁点与头仓（AI 分析增强）

参考公开交易笔记中的可复现部分：**公式在代码里算，提示词只加短纪律**，避免撑爆 Groq / LLM 上下文。

### 波段锁点（`jack_levels`）

创建分析会话时预计算并写入 `market_snapshot.jack_levels`，再注入 user 模板 `{jack_block}`：

| 字段 | 含义 |
|------|------|
| `rebound_382` / `rebound_618` | 下跌后反弹：`Low+(H−L)×0.382/0.618`（近压 / 主目标） |
| `daily_bias` | 日线定调：`up` / `down` / `range` |
| `defense_level` | 失效防守位 |
| `htf_ready` / `horizon` | 高周期是否成熟；未成熟时建议 `short` 反抽 |
| `confluence_*` | 斐波位是否贴近日线 BOLL 中轨 |
| `touch_count` | 关键阻力近期触及次数（二破参考） |
| `rs_note` | 相对 BTC 强弱摘要 |

规则基线计划（`generate_baseline_plan`）在有锁点时：下半区按反弹目标给 TP；上半区仍偏趋势回踩。system 提示词仅增加约百余字纪律（见 `prompts/system_v1.md` / `system_groq.md`）。

### 头仓 / 补仓

防踏空小头仓（默认权益 3–4%）+ 短线总仓上限（默认 18%）；回踩补仓与突破补仓**二选一**，不可叠加。

- Web：**Web3 → 头仓/补仓计算器**
- API：`GET /api/tools/seed-position?account=10000&leverage=25&seed_pct=0.04&max_total_pct=0.18&add_mode=pullback`
- 代码：`src/analyst/compute/position_sizing.py`

与 Kelly（`kelly.py`）互补：Kelly 管单笔风险比例，头仓模块管分层结构。

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
# 推荐白名单见 .env.example：ai_plan + cycle_switch；周期位置日更另走 cycle_outlook
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
| `LLM_PROMPT_VERSION` | 提示词版本，默认 `v1`（完整）；Groq 前置层固定用短版 `groq` |
| `DEFAULT_ACCOUNT_USD` / `MAX_*` | AI 建议用的账户规模、单笔风险%、杠杆上限（亦用于头仓计算器默认参考） |
| `DEFAULT_SYMBOLS` | 默认观察列表；常驻品种未单独配置时也用这份 |
| `MONITOR_ALWAYS_ON` | `true`：Web 进程在跑时关页面也继续盯盘并推 TG |
| `MONITOR_DAEMON_TIMEFRAMES` | 常驻多周期，如 `15m,1h,4h` |
| `MONITOR_DAEMON_SYMBOLS` | 常驻品种；空则跟 `DEFAULT_SYMBOLS` / 页面观察列表（常驻模式下加减币**无需重启**） |
| `MONITOR_CYCLE_SWITCH_ENABLED` | `true`：各盯盘币对跑 `cycle_switch`；相对上一根 K 仓位变化 → 页面 + AI 候选（**不直推 TG**） |
| `MONITOR_CYCLE_OUTLOOK_ENABLED` | `true`：每天提醒一次当前周期位置（BTC，**UTC 每天最多 1 条**） |
| `MONITOR_AI_ON_CANDIDATE` | `true`：收盘有双线/规则/`cycle_switch` 候选时才调 AI；结论 `long`/`short` 才推 `ai_plan` |
| `MONITOR_AI_FREE_ONLY` | `true`：盯盘自动确认**只用免费 Groq**，失败不回落付费线路（需 `GROQ_API_KEY`） |
| `MONITOR_AI_COOLDOWN_MINUTES` | 同品种+AI 周期冷却（默认 240），防候选刷屏 |
| `MONITOR_PAPER_ENABLED` | `true`：纸面模拟炒币，跟 `ai_plan`/`double_line`/`cycle_switch` |
| `MONITOR_PAPER_EQUITY` | 初始虚拟权益（默认 100 USDT） |
| `MONITOR_PAPER_SOURCES` | 纸面跟单来源，默认 `ai_plan,double_line,cycle_switch` |
| `MONITOR_PAPER_MAX_POSITIONS` | 最大同时持仓数（默认 12；去重键=品种×策略） |
| `MONITOR_TG_TRADE_RULES` | TG 白名单；默认仅 `ai_plan`。空=全部规则推 TG |
| `MONITOR_TOUCH_COOLDOWN_BARS` | 支撑/阻力触及告警冷却根数（页面仍提示；默认不进 TG） |
| `TELEGRAM_BOT_TOKEN` / `CHAT_ID` | 告警推送 |

Web **固定 U 本位永续**，无需再配「现货 / 合约」切换。

---

## 策略库

```bash
analyst strategies    # 列出全部策略及 CLI 示例
```

| 类型 | ID | 说明 |
|------|-----|------|
| **实时** | `double_line` | 双线反转：15m 形态突破 + EMA200 过滤，走 monitor 收盘评估 |
| **组合** | `cycle_switch` | 牛熊周期切换（D）：减半日历×200 日线双确认；牛市唐奇安只多，熊市反弹做空半仓 |
| **组合** | `donchian` | 唐奇安 40/20 通道只多，低频趋势基线 |
| **组合** | `ema_cross` | EMA 双均线 always-in |
| **组合** | `boll_mr` | 布林均值回归（对照组） |

实时策略与组合策略**互补**：前者盯短线形态，后者看长周期仓位与牛熊相位。

---

## CLI（可选）

```bash
analyst practice BTC          # 创建分析会话
analyst verify                # 验证已到期会话
analyst history               # 历史列表
analyst backtest BTC -t 15m   # 双线反转 + 规则告警历史回放
analyst backtest-classic BTC -s cycle_switch --days 1825   # 组合策略长周期回测
analyst cycle-outlook         # Wolfy 周期展望（终端）
analyst cycle-status          # 当前牛熊相位 + 各币 cycle_switch 目标仓位
analyst monitor once BTC -t 15m
```

| 命令 | 作用 |
|------|------|
| `analyst web` | Web + 常驻盯盘 + 周期图 API |
| `analyst practice <symbol>` | AI 分析并落库 |
| `analyst verify` | 验证到期会话 |
| `analyst backtest <symbol>` | 双线反转策略胜率 + 规则命中率回放 |
| `analyst backtest-classic <symbol>` | 经典组合策略回测（复利、手续费、牛熊分段、样本外） |
| `analyst cycle-outlook` | Wolfy 日历 + 狼波 RSI + 转折点倒计时 |
| `analyst cycle-status` | 实时 `cycle_switch` 各品种目标仓位 |
| `analyst strategies` | 策略库目录 |
| `analyst history` / `review <id>` | 历史 / 单条复盘 |
| `analyst progress` / `weakness` / `ai-benchmark` | 统计 |
| `analyst config test-llm` | LLM 连通 |
| `analyst db init` | 初始化 SQLite |

---

## 回测

### 实时策略回放（`analyst backtest`）

用**和实时盯盘同一套评估代码**在历史 K 线上向前回放，量化告警质量：

```bash
analyst backtest BTC -t 15m --bars 1000        # 最近 1000 根 15m
analyst backtest SOL -t 1h --bars 1500 --json r.json   # 结果另存 JSON
analyst backtest ETH -t 4h --no-rules          # 只测策略，跳过规则统计
```

| | 双线反转策略 | 规则告警 |
|---|---|---|
| **怎么测** | 逐根收盘回放，出信号即按计划模拟下单 | 每条带方向的告警做 ATR 屏障前瞻 |
| **输出** | 胜率、累计 R、PF、最大回撤 | 每条规则的样本数 / 命中率 |

### 经典组合策略（`analyst backtest-classic`）

长周期仓位回测，含单边手续费/滑点、复利收益、牛熊震荡分段贡献与样本外验证：

```bash
analyst backtest-classic BTC -s donchian --days 1825      # 唐奇安只多 5 年
analyst backtest-classic BTC -s cycle_switch --days 1825  # 牛熊周期切换
analyst backtest-classic ETH -s ema_cross -t 4h --oos-days 365
```

可选策略：`-s donchian | ema_cross | boll_mr | cycle_switch | buy_hold`

读数参考：规则命中率 ≈50% 说明单独使用无优势；组合策略在加密市场**做空腿普遍拖累收益**，`cycle_switch` 仅在熊市用反弹做空；样本 < 10 或日历边界过拟合需谨慎。

---

## 四年周期（Wolfy 刻舟求剑 + 狼波）

基于 BTC 日线的**周期位置参考**（非交易信号，仅供参考）：

- **图 1 日历**：锚定历次熊市底部，牛市 1064 天 → 预计见顶，熊市 364 天 → 预计见底
- **图 2 狼波**：RSI + 短期动量近似 TradingView 狼波指数，红区过热、蓝区超卖
- **提醒**：`cycle_outlook` **每天**推一次当前周期位置；各币 `cycle_switch` 仓位变化上页面并触发 AI，**可交易才推 TG**（`ai_plan`）

```bash
analyst cycle-outlook              # 终端查看当前相位与倒计时
analyst cycle-outlook --telegram   # 同时推 TG
analyst cycle-status               # cycle_switch 各币实时目标仓位
```

Web：`GET /api/monitor/cycle-timeline` · 顶栏「周期」专页（盯盘后第二项）

---

## 本地会生成什么

| 路径 | 内容 |
|------|------|
| `analyst.db` | AI 会话、计划、验证、聊天 |
| `.cache/data/paper_account.json` | 纸面模拟账户（权益/持仓/成交） |
| `.cache/data/monitor_daemon.json` | 常驻盯盘品种（页面观察列表可同步过来） |
| `.cache/data/cycle_outlook_tg.json` | 周期位置日更 TG 日戳 |
| `.cache/data/ai_confirm_cooldown.json` | AI 候选确认冷却 |
| `.cache/data/` | REST 短缓存（可删） |
| `.env` / `.venv/` | 本地密钥与虚拟环境（已 gitignore） |

实时 WS K 线只在内存滚动，**不**当历史库存。

---

## 开发

```bash
uv sync --extra web --extra dev
pytest tests/ -q
python scripts/generate_favicon.py   # 重新生成 favicon.ico
```

```
crypto-analyst/
├── prompts/           # LLM 提示词（v1 完整 / groq 短版；含波段锁点短纪律）
├── scripts/run-web.sh
├── scripts/generate_favicon.py
├── src/analyst/
│   ├── backtest/classic.py           # 组合策略回测
│   ├── compute/cycle_theory.py       # Wolfy 日历 + 狼波
│   ├── compute/jack_levels.py        # 波段锁点预计算
│   ├── compute/position_sizing.py    # 头仓/补仓分层
│   ├── trading/paper.py              # 纸面模拟账本
│   └── compute/strategies/           # double_line / cycle_switch / registry
└── tests/
```

---

## 说明

- **不自动下单**；盈亏与决策自负。
- 周期日历为「刻舟求剑」模型，里程碑日期有**过拟合历史**风险，请与盘面结合判断。
- 波段锁点为结构/斐波启发式，**不是**对任何个人交易员的复刻保证；请与盘面与风控一并使用。
- 需能访问 Binance 行情；Python **3.11+**。
