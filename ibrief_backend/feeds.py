"""RSS feed configuration.

Verified with real requests on 2026-10-27 (HTTP 200 + parseable entries) unless noted.
Dropped (dead on 2026-10-27): Reuters (feeds.reuters.com DNS/reset, reutersagency 404),
Yahoo Finance /news/rssindex and /rss/topstories (404), BirGun category feed (empty),
Sozcu /feeds-rss-category-siyaset (503), AA /cat=politika (stale since April),
Haberturk /rss/siyaset.xml and Hurriyet /rss/siyaset (stale, 2019).

NOT verifiable from the dev network (connection reset): CoinDesk and Cointelegraph.
They are kept because they are standard public feeds; if they fail in CI the
finance block simply loses them (per-feed failures are tolerated).

`keywords`: optional regex; for mixed feeds only items whose title/summary match are kept.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Feed:
    source: str
    url: str
    lang: str = "en"
    keywords: str | None = None
    weight: float = 1.0  # small source-quality bias used in ranking


TR_POLITICS_RE = (
    r"(?i)siyaset|AK Parti|AKP|CHP|MHP|DEM Parti|İYİ Parti|Erdoğan|Bahçeli|Özgür Özel|İmamoğlu|"
    r"TBMM|Meclis|milletvekili|seçim|cumhurbaşkan|bakan\b|bakanı|bakanlığı|belediye başkan|"
    r"muhalefet|iktidar|anayasa|Yargıtay|Danıştay|Anayasa Mahkemesi|dışişleri|parti"
)

FEEDS: dict[str, list[Feed]] = {
    "finance": [
        Feed("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
        Feed("CNBC Markets", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
        Feed("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
        Feed("Yahoo Finance", "https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC&region=US&lang=en-US"),
        Feed("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
        Feed("Bloomberg HT", "https://www.bloomberght.com/rss", lang="tr"),
        Feed("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
        Feed("Cointelegraph", "https://cointelegraph.com/rss"),
    ],
    "globalPolitics": [
        Feed("BBC News", "https://feeds.bbci.co.uk/news/world/rss.xml"),
        Feed("BBC Politics", "https://feeds.bbci.co.uk/news/politics/rss.xml"),
        Feed("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
        Feed("The Guardian", "https://www.theguardian.com/world/rss"),
        Feed("Anadolu Agency", "https://www.aa.com.tr/en/rss/default?cat=world"),
    ],
    "turkeyPolitics": [
        Feed("Cumhuriyet", "https://www.cumhuriyet.com.tr/rss/siyaset", lang="tr"),
        Feed("Hürriyet", "https://www.hurriyet.com.tr/rss/gundem", lang="tr", keywords=TR_POLITICS_RE),
        Feed("BirGün", "https://www.birgun.net/rss/home", lang="tr", keywords=TR_POLITICS_RE),
        Feed("Sözcü", "https://www.sozcu.com.tr/feeds-rss-category-sozcu", lang="tr", keywords=TR_POLITICS_RE),
        Feed("Anadolu Ajansı", "https://www.aa.com.tr/tr/rss/default?cat=guncel", lang="tr", keywords=TR_POLITICS_RE),
        Feed("Habertürk", "https://www.haberturk.com/rss", lang="tr", keywords=TR_POLITICS_RE),
    ],
    "turkeyDaily": [
        Feed("Anadolu Ajansı", "https://www.aa.com.tr/tr/rss/default?cat=guncel", lang="tr"),
        Feed("Hürriyet", "https://www.hurriyet.com.tr/rss/anasayfa", lang="tr"),
        Feed("Habertürk", "https://www.haberturk.com/rss", lang="tr"),
        Feed("Sözcü", "https://www.sozcu.com.tr/feeds-rss-category-sozcu", lang="tr"),
        Feed("BirGün", "https://www.birgun.net/rss/home", lang="tr"),
        Feed("Cumhuriyet", "https://www.cumhuriyet.com.tr/rss", lang="tr"),
    ],
}

USER_AGENT = "Mozilla/5.0 (compatible; iBriefBot/1.0; personal morning brief)"
TIMEOUT = 10
