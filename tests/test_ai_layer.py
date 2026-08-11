"""AI 层测试：日报模板降级、新闻解析/去重/关键词兜底（不打真实 LLM）。"""

from analyst.data.news import NewsItem, _parse_rss
from analyst.llm.digest import _template_digest
from analyst.monitor.news_sentinel import (
    SEVERITY_ORDER,
    keyword_fallback_classify,
)

RSS_SAMPLE = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>t</title>
<item><title>Bitcoin rises 3% as ETF inflows continue</title>
<link>https://x.com/a1</link><guid>g1</guid>
<pubDate>Sat, 19 Jul 2026 08:00:00 GMT</pubDate></item>
<item><title>Exchange XYZ halts withdrawals amid insolvency fears</title>
<link>https://x.com/a2</link><guid>g2</guid></item>
</channel></rss>"""

ATOM_SAMPLE = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>SEC sues major exchange</title>
<id>atom1</id><link href="https://y.com/b1"/>
<updated>2026-07-19T08:00:00Z</updated></entry>
</feed>"""


def test_parse_rss_and_atom():
    items = _parse_rss(RSS_SAMPLE, "x.com")
    assert len(items) == 2
    assert items[0].id == "g1"
    assert items[0].url == "https://x.com/a1"
    atom = _parse_rss(ATOM_SAMPLE, "y.com")
    assert len(atom) == 1
    assert atom[0].id == "atom1"
    assert atom[0].url == "https://y.com/b1"


def test_parse_rss_malformed_returns_empty():
    assert _parse_rss("not xml at all <<<", "bad") == []


def test_keyword_fallback_flags_critical_only():
    items = [
        NewsItem(id="a", title="Bitcoin rises 3% today", source="s", url="", published=""),
        NewsItem(id="b", title="Exchange halts withdrawals after hack", source="s", url="", published=""),
        NewsItem(id="c", title="USDC depeg fears grow", source="s", url="", published=""),
    ]
    out = keyword_fallback_classify(items)
    ids = {o["id"] for o in out}
    assert ids == {"b", "c"}
    assert all(o["severity"] == "critical" for o in out)
    assert SEVERITY_ORDER["critical"] > SEVERITY_ORDER["high"]


def test_template_digest_renders_without_llm():
    facts = {
        "as_of_utc": "2026-07-19 05:00",
        "market": {"regime": "bear", "btc_price": 63910.0,
                   "btc_vs_ema200d_pct": -14.0},
        "relative_strength": {
            "pairs": {
                "ETH/BTC": {"state": "above", "vs_ema_pct": 2.1},
            },
        },
    }
    text = _template_digest(facts)
    assert "63910" in text
    assert "熊市" in text
    assert "ETH/BTC" in text
    assert "熔断" not in text
