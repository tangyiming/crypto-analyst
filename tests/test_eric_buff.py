"""Eric Buff / EMA / MTF 内化测试。"""

from datetime import datetime, timedelta

from analyst.compute.band_filter import compute_band_filter, wavetrend_series
from analyst.compute.eric_signals import evaluate_eric, score_buff
from analyst.compute.band_filter import BandFilterSnapshot
from analyst.data.fetcher import Candle, CandleSeries


def _c(i: int, close: float) -> Candle:
    return Candle(
        timestamp=datetime(2026, 1, 1) + timedelta(hours=i),
        open=close,
        high=close + 1.5,
        low=close - 1.5,
        close=close,
        volume=1000,
    )


def test_buff_score_stacks():
    bf = BandFilterSnapshot(
        value=-65,
        prev=-50,
        zone="deep_os",
        entered_oversold=False,
        entered_deep_os=True,
        entered_overbought=False,
        entered_deep_ob=False,
        channel_len=10,
        average_len=21,
    )
    htf = BandFilterSnapshot(
        value=-45,
        prev=-30,
        zone="oversold",
        entered_oversold=True,
        entered_deep_os=False,
        entered_overbought=False,
        entered_deep_ob=False,
        channel_len=10,
        average_len=21,
    )
    b = score_buff(
        side="long",
        bf=bf,
        near_structure=True,
        div=True,
        ema_lvl=100.0,
        htf_bf=htf,
        htf_bias="bull",
        rr=1.8,
    )
    assert b.score >= 4
    assert "深超卖" in b.tags
    assert "HTF超卖共振" in b.tags


def test_mtf_align_emits_when_htf_oversold():
    # 构造 LTF 下穿 -40
    closes = [100.0 + i * 0.5 for i in range(80)]
    closes += [closes[-1] - i * 2.2 for i in range(1, 45)]
    candles = [_c(i, closes[i]) for i in range(len(closes))]
    # HTF：同样偏弱，强制放进超卖区（用同一序列截更长急跌）
    htf_closes = [100.0 + i * 0.3 for i in range(60)]
    htf_closes += [htf_closes[-1] - i * 3.0 for i in range(1, 40)]
    htf = CandleSeries("BTC/USDT", "4h", [_c(i, htf_closes[i]) for i in range(len(htf_closes))])

    found = False
    for n in range(70, len(candles) + 1):
        series = CandleSeries("BTC/USDT", "1d", candles[:n])
        bf = compute_band_filter(series)
        if not bf or not (bf.entered_oversold or bf.entered_deep_os):
            continue
        sigs = evaluate_eric(series, atr=3.0, htf_series=htf, min_buff=4)
        kinds = {s.kind for s in sigs}
        if "mtf_align" in kinds or "oversold" in kinds:
            found = True
            # 若 HTF 也在超卖区，应有 mtf
            hbf = compute_band_filter(htf)
            if hbf and hbf.zone in ("oversold", "deep_os"):
                assert "mtf_align" in kinds
            break
    assert found
