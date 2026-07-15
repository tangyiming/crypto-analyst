请分析以下品种当前结构并给出可执行交易计划。

# 标的与时间

- 品种：{symbol}
- 当前时间（UTC）：{captured_at}
- 主分析周期：{primary_timeframe}

# 当前价位

- 现价：{current_price}
- 24h 高 / 低：{high_24h} / {low_24h}
- 7d 高 / 低：{high_7d} / {low_7d}
- 30d 高 / 低：{high_30d} / {low_30d}

# 多周期指标

## 日线
- MACD: dif={d_dif}, dea={d_dea}, hist={d_hist}, 零轴={d_zero}, 信号={d_signal}
- EMA: 7={d_ema7}, 30={d_ema30}, 52={d_ema52}
- BOLL: 上轨={d_boll_u}, 中轨={d_boll_m}, 下轨={d_boll_l}
- 量能: {d_vol_signal}（OBV {d_obv_trend}，量比 {d_vol_ratio}×）

## 4h
- MACD: dif={h4_dif}, dea={h4_dea}, hist={h4_hist}, 零轴={h4_zero}, 信号={h4_signal}
- EMA: 7={h4_ema7}, 30={h4_ema30}, 52={h4_ema52}
- BOLL: 上轨={h4_boll_u}, 中轨={h4_boll_m}, 下轨={h4_boll_l}
- 量能: {h4_vol_signal}（OBV {h4_obv_trend}，量比 {h4_vol_ratio}×）

## 1h
- MACD: dif={h1_dif}, dea={h1_dea}, hist={h1_hist}, 零轴={h1_zero}, 信号={h1_signal}
- EMA: 7={h1_ema7}, 30={h1_ema30}, 52={h1_ema52}
- 量能: {h1_vol_signal}（OBV {h1_obv_trend}）

# 资金面（永续合约）

{derivatives_block}

# 全市场情绪

{macro_block}

# 波段锁点（代码预计算，请直接采用，勿重算）

{jack_block}

# 账户参数

- 账户规模：{account_usd} USDT
- 单笔最大风险：{max_risk_pct}%
- 最大可用杠杆：{max_leverage}x

# 近期已验证的 AI 计划复盘（自我校准）

{recent_lessons}

# 重要

请用工具调用 `submit_analysis` 提交结果。不要输出非工具内容。
