"""结构识别测试。"""

from analyst.compute.structure import cluster_levels, find_pivots


def test_find_pivots_simple():
    """构造一个明显的高点和低点。

    fractal 算法只在 [window, n-window) 范围内检查中心 K 线，
    所以 pivot 必须距离两端至少 window 根。
    """
    #          0   1   2   3   4   5   6   7   8   9  10  11  12
    highs = [10, 20, 30, 25, 20, 15, 25, 30, 35, 30, 25, 20, 15]
    lows = [5, 10, 20, 15, 10, 5, 15, 20, 25, 20, 15, 10, 5]
    p_h, p_l = find_pivots(highs, lows, window=3)
    # 35(idx=8) 是局部高，距离两端各 4 根，足够
    assert 8 in p_h
    # 5(idx=5) 是局部低，距离两端各 5 根，足够
    assert 5 in p_l


def test_cluster_levels_merges_close_prices():
    prices = [100.0, 100.3, 100.4, 105.0, 105.2, 110.0]
    clusters = cluster_levels(prices, threshold_pct=0.005)
    # 100.x 一类、105.x 一类、110 一类
    assert len(clusters) == 3
    assert abs(clusters[0] - 100.23) < 1


def test_cluster_levels_empty():
    assert cluster_levels([]) == []


def test_cluster_levels_no_merge():
    """阈值很低，每个价格独立成簇。"""
    prices = [100.0, 200.0, 300.0]
    clusters = cluster_levels(prices, threshold_pct=0.005)
    assert clusters == [100.0, 200.0, 300.0]
