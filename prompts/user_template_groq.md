分析 {symbol}，UTC {captured_at}，主周期 {primary_timeframe}。现价 {current_price} | 24h {low_24h}-{high_24h} | 7d {low_7d}-{high_7d} | 30d {low_30d}-{high_30d}

D: MACD d/h/z/sig={d_dif}/{d_dea}/{d_hist}/{d_zero}/{d_signal} EMA7/30/52={d_ema7}/{d_ema30}/{d_ema52} BOLL u/m/l={d_boll_u}/{d_boll_m}/{d_boll_l} 量={d_vol_signal}|OBV {d_obv_trend}|比{d_vol_ratio}x
4h: MACD {h4_dif}/{h4_dea}/{h4_hist}/{h4_zero}/{h4_signal} EMA {h4_ema7}/{h4_ema30}/{h4_ema52} BOLL {h4_boll_u}/{h4_boll_m}/{h4_boll_l} 量 {h4_vol_signal}|{h4_obv_trend}|{h4_vol_ratio}x
1h: MACD {h1_dif}/{h1_dea}/{h1_hist}/{h1_zero}/{h1_signal} EMA {h1_ema7}/{h1_ema30}/{h1_ema52} 量 {h1_vol_signal}|{h1_obv_trend}

衍生品: {derivatives_block}
情绪: {macro_block}
锁点: {jack_block}
账户 USDT {account_usd} 风险{max_risk_pct}% 杠杆≤{max_leverage}x

复盘: {recent_lessons}

调用 submit_analysis，勿输出工具外文字。
