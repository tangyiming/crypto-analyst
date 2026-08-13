# Crypto Analyst

本地跑的 **AI 行情分析 + U 本位永续盯盘 + 牛熊周期工具**。AI 给出观点/计划并落库复盘；规则引擎常驻盯盘，命中后推 Telegram——**只提醒，不下单**。

顶栏：**盯盘** → **周期** → **日程** → **策略库** → **回测** → **AI 助手**。

---

## 市场日程

顶栏 **「日程」** 独立页（不挤盯盘）：

- **多时区时钟**：浏览器本地时区置顶（如迪拜 `Asia/Dubai`）+ 北京 / 伦敦 / 纽约对照
- **交易时段**：亚盘活跃 · 欧盘开盘 · 美盘开盘（UTC 窗口 + 倒计时）
- **资金费**：下次结算倒计时（复用 mark 流）
- **宏观日历**：Forex Factory 本周公开 JSON，默认只保留 **USD · High**（CPI / FOMC 等）
- **TG 提前提醒**：时段 30/15 分钟、资金费 30 分钟、宏观 60/30/15 分钟（可关）

API：`GET /api/schedule?tz=Asia/Dubai` · 开关见 `MONITOR_SCHEDULE_*`。

盯盘图表周期 **`5m / 15m / 1h / 4h`**（`MONITOR_CHART_TIMEFRAMES`）。

---

## 能力一览

| | AI 分析 | 实时盯盘 | 周期与组合策略 |
|---|---|---|---|
| **做什么** | 多周期数据 + **波段锁点预计算** → LLM 出观点与计划 → 到期验证 | 规则 + `cycle_switch` / `xs_momentum` / `funding_carry`；候选可再调 AI 确认后推 TG | Wolfy 四年周期 + `cycle_switch`；经典策略长周期回测 |
| **入口** | Web 右侧「AI 行情分析」，或 `analyst practice` | 打开 Web；开常驻后关页面也推 TG | 顶栏「周期」；`analyst cycle-outlook` / `backtest-classic` |
| **数据** | 会话写入 `analyst.db`（含 `jack_levels`） | 观察列表 + 告警；K 线本身不长期落库 | BTC 日线定日历相位；组合回测分页拉 2–5 年历史 |

```
盯盘推送（Web / TG）  ←→  选币做 AI 分析（锁点注入）落库  →  到期验证  →  历史复盘
                              ↑
              周期图 / cycle_switch / 转折点倒计时
```

### 告警怎么推（简要）

| 类型 | 页面 | Telegram |
|------|------|----------|
| 规则噪音（放量、触及等） | 有 | 默认不推（可改白名单） |
| 收盘有候选 → AI 点评 `long`/`short`（`ai_plan`） | 有 | 推；仅提醒（含 SL/TP 计划） |
| 各币 `cycle_switch` 仓位变化 | 有（触发 AI 候选） | 不直推；等 AI 确认 |
| 周期位置日更（`cycle_outlook`，BTC） | 有 | UTC **每天最多 1 条** |
| 日程：时段 / 资金费 / 宏观高影响 | 「日程」页 | 提前期推（`MONITOR_SCHEDULE_TG`） |

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
| 日程 | 交易时段 · 本地时区时钟 · 资金费 · USD 高影响宏观日历 + TG 提前提醒 |
| 策略库 / 回测 | 本平台策略目录；经典组合回测与 CLI `backtest-classic` 同源 |
| AI 助手 | 交易日报 · 研究假设 · 新闻风险事件 |

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

同一 Wi-Fi 下用手机浏览器看，可加 `--lan`：

```bash
./scripts/run-web.sh --lan
```

---

## 配置速查

详细注释见 [`.env.example`](.env.example)。常用项：

| 变量 | 作用 |
|------|------|
| `DEEPSEEK_API_KEY` / `LLM_*` | 主分析线路（还可切 b.ai / Anthropic / Groq 等） |
| `LLM_PROMPT_VERSION` | 提示词版本，默认 `v1`（完整）；Groq 前置层固定用短版 `groq` |
| `DEFAULT_ACCOUNT_USD` / `MAX_*` | 注入 AI 分析 prompt 的账户规模、单笔风险%、杠杆上限（仅建议，不下单） |
| `DEFAULT_SYMBOLS` | **唯一品种列表**（盯盘 / cycle / xs / carry 默认都跟这份） |
| `MONITOR_ALWAYS_ON` | `true`：Web 进程在跑时关页面也继续盯盘并推 TG |
| `MONITOR_DAEMON_TIMEFRAMES` | 常驻多周期，默认 `15m,1h,4h` |
| `MONITOR_CHART_TIMEFRAMES` | 盯盘图表可选周期，默认 `5m,15m,1h,4h` |
| `MONITOR_DAEMON_SYMBOLS` | 常驻品种覆盖；空则跟 `DEFAULT_SYMBOLS`（常驻模式下加减币**无需重启**） |
| `MONITOR_CYCLE_SWITCH_ENABLED` | `true`：各盯盘币对跑 `cycle_switch`；**新开仓**且 ADX 达标 → 页面 + AI 候选（平仓/震荡开仓只上页面，**不直推 TG**） |
| `MONITOR_ADX_MIN_TREND` | 趋势规则（MACD/EMA/布林）最低 ADX，默认 18；震荡市不报 |
| `MONITOR_HTF_FILTER` | `true`：逆更高周期 EMA 排列的趋势信号丢掉（1h 看 4h） |
| `MONITOR_CYCLE_ADX_MIN` | cycle 新开仓 ADX 门槛，默认 20 |
| `MONITOR_CYCLE_SYMBOLS` / `MONITOR_XS_SYMBOLS` / `MONITOR_CARRY_SYMBOLS` | 策略观察池覆盖；空=跟随盯盘品种 |
| `MONITOR_CYCLE_OUTLOOK_ENABLED` | `true`：每天提醒一次当前周期位置（BTC，**UTC 每天最多 1 条**） |
| `MONITOR_AI_ON_CANDIDATE` | `true`：收盘有合格候选时才调 AI；`long`/`short` 推「盯盘点评」（仅提醒） |
| `MONITOR_AI_REQUIRE_QUALITY` | `true`：单条放量/触及不够；需质量规则或同根 ≥2 条 |
| `MONITOR_AI_FREE_ONLY` | `true`：盯盘自动确认**只用免费层**（Groq/Cerebras/Gemini/OpenRouter/SambaNova），失败不回落付费 |
| `LLM_FREE_ORDER` | 免费层顺序，默认 `nvidia,groq,cerebras,openrouter,sambanova,gemini`（有 key 才实际调用） |
| `CEREBRAS_API_KEY` / `NVIDIA_API_KEY` / `GEMINI_API_KEY` / `OPENROUTER_API_KEY` / `SAMBANOVA_API_KEY` | 额外免费线路；任选配置即可 failover |
| `MONITOR_AI_COOLDOWN_MINUTES` | 同品种+AI 周期冷却（默认 240），防候选刷屏 |
| `MONITOR_SCHEDULE_ENABLED` / `MONITOR_SCHEDULE_TG` | 市场日程页与 TG 提前提醒 |
| `MONITOR_SCHEDULE_SESSION_LEADS` 等 | 时段 / 资金费 / 宏观提前提醒分钟（逗号分隔） |
| `MONITOR_TG_TRADE_RULES` | TG 白名单；默认 AI 点评 + MACD/均线/放量/布林/突破/资金费等异动 |
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
| **组合** | `cycle_switch` | 牛熊周期切换（D）：减半日历×200 日线双确认；牛市唐奇安只多，熊市反弹做空半仓 |
| **组合** | `xs_momentum` | 横截面动量：多币 top2 做多、熊市空最弱 |
| **组合** | `funding_carry` | 资金费 delta 中性套利：现货多+永续空收资金费 |

组合策略看长周期仓位与牛熊相位；实时盯盘走 Web 规则引擎 + `cycle_switch`。

---

## CLI（可选）

```bash
analyst practice BTC          # 创建分析会话
analyst verify                # 验证已到期会话
analyst history               # 历史列表
analyst backtest BTC -t 15m   # 规则告警历史回放（前瞻命中率）
analyst backtest-classic BTC -s cycle_switch --days 1825   # 组合策略长周期回测
analyst cycle-outlook         # Wolfy 周期展望（终端）
analyst cycle-status          # 当前牛熊相位 + 各币 cycle_switch 目标仓位
```

| 命令 | 作用 |
|------|------|
| `analyst web` | Web + 常驻盯盘 + 周期图 API |
| `analyst practice <symbol>` | AI 分析并落库 |
| `analyst verify` | 验证到期会话 |
| `analyst backtest <symbol>` | 规则告警前瞻命中率回放 |
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

### 规则回放（`analyst backtest`）

用**和实时盯盘同一套规则代码**在历史 K 线上向前回放，量化告警质量：

```bash
analyst backtest BTC -t 15m --bars 1000        # 最近 1000 根 15m
analyst backtest SOL -t 1h --bars 1500 --json r.json   # 结果另存 JSON
```

每条带方向的规则告警做 ATR 屏障前瞻，输出样本数 / 命中率 / 平均前瞻收益。

### 经典组合策略（`analyst backtest-classic`）

长周期仓位回测，含单边手续费/滑点、复利收益、牛熊震荡分段贡献与样本外验证：

```bash
analyst backtest-classic BTC -s cycle_switch --days 1825  # 牛熊周期切换 5 年
analyst backtest-classic BTC -s buy_hold --days 1825      # 买入持有基准
analyst backtest-xs --days 1825                           # 横截面动量
analyst backtest-carry BTC --days 1825                    # 资金费套利
```

可选策略（`backtest-classic`）：`-s cycle_switch | buy_hold`

读数参考：规则命中率 ≈50% 说明单独使用无优势；组合策略在加密市场**做空腿普遍拖累收益**，`cycle_switch` 仅在熊市用反弹做空；样本 < 10 或日历边界过拟合需谨慎。

---

## 四年周期（Wolfy 刻舟求剑 + 狼波）

基于 BTC 日线的**周期位置参考**（非交易信号，仅供参考）：

- **图 1 日历**：锚定历次熊市底部，牛市 1064 天 → 预计见顶，熊市 364 天 → 预计见底
- **图 2 狼波**：RSI + 短期动量近似 TradingView 狼波指数，红区过热、蓝区超卖
- **提醒**：异动规则（MACD 金叉死叉、放量、突破等）+ AI 盯盘点评推 TG；`cycle_outlook` 每天推周期位置；**日程**推时段/资金费/宏观；`xs_momentum` / `funding_carry` 信号变化上页面告警

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
| `.cache/data/monitor_daemon.json` | 常驻盯盘品种（页面观察列表可同步过来） |
| `.cache/data/schedule_reminders.json` | 日程 TG 提醒去重键 |
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
│   ├── compute/market_schedule.py    # 时段 / 时钟 / FF 宏观日历
│   ├── compute/jack_levels.py        # 波段锁点预计算
│   ├── compute/position_sizing.py    # 头仓/补仓分层
│   ├── monitor/schedule_reminders.py # 日程 TG 提前提醒轮询
│   ├── web/schedule_routes.py        # GET /api/schedule
│   └── compute/strategies/           # cycle_switch / xs_momentum / registry
└── tests/
```

---

## 说明

- **不自动下单**；盈亏与决策自负。（纸面模拟交易功能已移除，系统只提醒不下单。）
- 周期日历为「刻舟求剑」模型，里程碑日期有**过拟合历史**风险，请与盘面结合判断。
- 波段锁点为结构/斐波启发式，**不是**对任何个人交易员的复刻保证；请与盘面与风控一并使用。
- 需能访问 Binance 行情；Python **3.11+**。
