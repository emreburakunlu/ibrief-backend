from datetime import datetime, timezone
from pathlib import Path

import pytest

from ibrief_backend import build, calendar_data, markets, news
from ibrief_backend.feeds import FEEDS, Feed

NOW = datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc)
XML = (Path(__file__).parent / "fixtures" / "sample.xml").read_bytes()
FEED = Feed("Ex", "https://example.com/rss")


def test_strip_html_and_truncate():
    assert news.strip_html("<p>The <b>Fed</b> &amp; rates&nbsp;x</p><script>evil()</script>") == "The Fed & rates x"
    long = news.strip_html("word " * 200)
    assert len(long) <= 300 and long.endswith("…")


def test_item_id_stable_and_tracking_insensitive():
    a = news.item_id("https://Example.com/a/?utm_source=x#frag")
    assert a == news.item_id("https://example.com/a")
    assert len(a) == 12 and a != news.item_id("https://example.com/b")


def test_parse_feed_window_and_summary():
    items = news.parse_feed(XML, FEED, NOW)
    titles = [i["title"] for i in items]
    assert "Old story" not in titles and len(items) == 3
    assert items[0]["summary"] == "The Fed held rates steady."
    assert items[0]["publishedAt"] == "2026-10-27T06:00:00Z"


def test_keyword_filter():
    f = Feed("Ex", "u", keywords=r"(?i)oil")
    assert [i["title"] for i in news.parse_feed(XML, f, NOW)] == ["Unrelated oil story"]


def test_dedupe_merges_near_duplicates():
    items = news.parse_feed(XML, FEED, NOW)
    items[1]["source"] = "Other"
    ranked = news.dedupe_and_rank(items, NOW)
    assert len(ranked) == 2
    assert not any(k.startswith("_") for k in ranked[0])


def test_turkish_normalization_dedupe():
    assert news.similar(news.normalize_title("İstanbul'da SAĞANAK alarmı!"), news.normalize_title("Istanbul'da sağanak alarmı"))
    assert not news.similar(news.normalize_title("Altın fiyatları düştü"), news.normalize_title("Seçim tarihi açıklandı"))


def test_ranking_prefers_multi_source_and_recent():
    def mk(t, src, hrs, w=1.0):
        from datetime import timedelta
        when = NOW - timedelta(hours=hrs)
        return {"id": news.item_id("https://x/" + t), "title": t, "source": src, "url": "https://x/" + t,
                "publishedAt": news.iso(when), "summary": "", "lang": "en", "_when": when, "_weight": w}
    items = [mk("Big merger announced between giants today", "A", 20), mk("Big merger announced between giants today!", "B", 22),
             mk("Fresh minor local story appears", "C", 1)]
    ranked = news.dedupe_and_rank(items, NOW)
    assert ranked[0]["title"].startswith("Big merger")


WORDS = [f"{a} {b} {c}" for a, b, c in zip("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa quebec romeo sierra tango uniform victor whiskey xray yankee zulu apple banana cherry date".split(), "red green blue cyan pink gold teal navy lime plum rose sand sky tan wine jade ruby onyx opal iris fern moss sage rust mint peach lemon grape melon kiwi".split(), "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty a1 a2 a3 a4 a5 a6 a7 a8 a9 a0".split())]


def test_per_source_and_category_cap():
    from datetime import timedelta
    items = [{"id": str(i), "title": WORDS[i], "source": "S", "url": f"https://x/{i}",
              "publishedAt": "", "summary": "", "lang": "en", "_when": NOW - timedelta(minutes=i), "_weight": 1.0}
             for i in range(30)]
    assert len(news.dedupe_and_rank(items, NOW)) == news.PER_SOURCE_CAP


def test_news_block_failure_isolation():
    def fetch(feed):
        if "bbci" in feed.url or "aljazeera" in feed.url or "theguardian" in feed.url or "/en/rss" in feed.url:
            raise RuntimeError("down")
        return XML
    out = news.build_news(fetch=fetch, now=NOW)
    assert out["globalPolitics"] == (None, "failed")
    assert out["finance"][1] == "ok" and out["turkeyDaily"][1] == "ok"


def test_one_dead_feed_does_not_fail_category():
    def fetch(feed):
        if feed.source == "CNBC":
            raise RuntimeError("down")
        return XML
    out = news.build_news(fetch=fetch, now=NOW)
    assert out["finance"][1] == "ok"


def _brief(monkeypatch, fail=()):
    def boom(*a, **k):
        raise RuntimeError("down")
    q = {"symbol": "X", "name": "X", "price": 1.0, "change": 0.0, "changePct": 0.0, "asOf": "2026-10-27T00:00:00Z"}
    for name, val in (("build_gold", q), ("build_indices", [q]), ("build_crypto", [q]), ("build_usdtry", q)):
        monkeypatch.setattr(markets, name, boom if name in fail else (lambda v=val: v))
    fetch_news = lambda now: news.build_news(fetch=lambda f: XML, now=now)  # noqa: E731
    return build.build_brief(now=NOW, news_builder=lambda now: fetch_news(now))


def test_schema_shape(monkeypatch):
    b = _brief(monkeypatch)
    assert b["meta"] == {"schemaVersion": 1, "generatedAt": "2026-10-27T08:00:00Z"}
    assert set(b["news"]) == {"finance", "globalPolitics", "turkeyPolitics", "turkeyDaily"}
    assert set(b["markets"]) == {"gold", "indices", "crypto"} and "USDTRY" in b["fx"]
    for key in ("macroCalendar", "holidays", "status"):
        assert key in b
    item = b["news"]["finance"][0]
    assert set(item) == {"id", "title", "source", "url", "publishedAt", "summary", "lang"}
    assert all(v == "ok" for v in b["status"].values())
    assert {"news.finance", "gold", "indices", "crypto", "fx", "macroCalendar", "holidays"} <= set(b["status"])


def test_market_failures_isolated(monkeypatch):
    b = _brief(monkeypatch, fail=("build_gold", "build_crypto"))
    assert b["status"]["gold"] == "failed" and b["markets"]["gold"] is None
    assert b["status"]["crypto"] == "failed" and b["markets"]["crypto"] == []
    assert b["status"]["indices"] == "ok" and b["status"]["news.finance"] == "ok"


def test_news_builder_crash_marks_all_news_failed(monkeypatch):
    def boom(now):
        raise RuntimeError("x")
    q = {}
    b = build.build_brief(now=NOW, news_builder=boom)
    assert all(b["status"][f"news.{c}"] == "failed" for c in FEEDS) and b["news"]["finance"] == []


def test_write_site(tmp_path, monkeypatch):
    b = _brief(monkeypatch)
    p = build.write_site(b, tmp_path)
    import json
    assert json.loads(p.read_text())["meta"]["schemaVersion"] == 1
    assert (tmp_path / "index.html").exists()


def test_yahoo_parse_prev_close():
    payload = {"chart": {"result": [{"meta": {"regularMarketPrice": 110.0, "regularMarketTime": 1793000000},
               "timestamp": [1, 2, 3], "indicators": {"quote": [{"close": [90.0, 100.0, 110.0]}]}}]}}
    q = markets.parse_yahoo_chart(payload, "X", "Name")
    assert q["price"] == 110.0 and q["change"] == 10.0 and q["changePct"] == 10.0


def test_coingecko_parse():
    q = markets.parse_coingecko({"bitcoin": {"usd": 110.0, "usd_24h_change": 10.0, "last_updated_at": 1793000000}})
    assert q[0]["symbol"] == "BTC" and q[0]["change"] == pytest.approx(10.0)


def test_frankfurter_parse():
    q = markets.parse_frankfurter_range({"rates": {"2026-10-23": {"TRY": 49.0}, "2026-10-26": {"TRY": 50.0}}})
    assert q["price"] == 50.0 and q["change"] == 1.0


def test_calendar_window():
    from datetime import date
    ev = calendar_data.macro_calendar(date(2026, 10, 27))
    assert ev and all("2026-10-27" <= e["date"] <= "2026-12-11" for e in ev)
    assert set(ev[0]) == {"id", "title", "date", "kind", "summary"}
    h = calendar_data.holidays(date(2026, 10, 27))
    assert any(x["country"] == "TR" and x["date"] == "2026-10-29" for x in h)
    assert set(h[0]) == {"id", "date", "country", "name"}


def _it(i, title, source, minutes, summary="s"):
    from datetime import timedelta
    return {"id": str(i), "title": title, "source": source, "url": f"https://x/{i}", "publishedAt": "",
            "summary": summary, "lang": "en", "_when": NOW - timedelta(minutes=minutes), "_weight": 1.0}


def test_cap_fallback_story_not_dropped():
    # fill the cap with higher-scoring distinct stories, then a lone cluster led by capped source
    items = [_it(i, WORDS[i], "Big", i) for i in range(news.PER_SOURCE_CAP)]
    items += [_it(50 + i, WORDS[i], "Other", 60 + i) for i in range(news.PER_SOURCE_CAP)]  # coverage boost
    items += [_it(200, "Central bank surprises markets with rate move", "Big", 400),
              _it(201, "Central bank surprises markets with rate move", "Small", 500)]
    ranked = news.dedupe_and_rank(items, NOW)
    story = [r for r in ranked if r["title"].startswith("Central bank")]
    assert len(story) == 1 and story[0]["source"] == "Small"


def test_turkey_daily_excludes_politics_by_title_similarity():
    politics = Feed("P", "https://p/rss", lang="tr")
    daily = Feed("D", "https://d/rss", lang="tr")
    rss = lambda title, n: (  # noqa: E731
        f'<?xml version="1.0"?><rss version="2.0"><channel><item><title>{title}</title>'
        f'<link>https://site/{n}</link><pubDate>Tue, 27 Oct 2026 07:00:00 GMT</pubDate></item></channel></rss>').encode()
    parsed = {
        "https://p/rss": news.parse_entries(rss("Meclis yeni anayasa taslağını görüşmeye başladı", "a"), politics, NOW),
        "https://d/rss": news.parse_entries(rss("Meclis yeni anayasa taslağını görüşmeye başladı!", "b"), daily, NOW)
        + news.parse_entries(rss("Hava durumu yarın yağmurlu", "c"), daily, NOW),
    }
    ranked_p = news.dedupe_and_rank(news.collect_category([politics], parsed)[0], NOW)
    norms = [news.normalize_title(i["title"]) for i in ranked_p]
    items, _ = news.collect_category([daily], parsed)
    ranked_d = news.dedupe_and_rank(items, NOW, exclude_norms=norms)
    assert [i["title"] for i in ranked_d] == ["Hava durumu yarın yağmurlu"]


def test_feeds_parsed_once(monkeypatch):
    calls = []
    real = news.parse_entries
    monkeypatch.setattr(news, "parse_entries", lambda c, f, n: (calls.append(f.url), real(c, f, n))[1])
    news.build_news(fetch=lambda f: XML, now=NOW)
    assert len(calls) == len(set(calls))


def test_main_fails_when_all_news_failed(monkeypatch, tmp_path):
    def boom(now):
        raise RuntimeError("x")
    real = build.build_brief
    monkeypatch.setattr(build, "build_brief", lambda: real(now=NOW, news_builder=boom))
    monkeypatch.setattr(build, "write_site", lambda *a, **k: (_ for _ in ()).throw(AssertionError("wrote")))
    q = {"symbol": "X", "name": "X", "price": 1.0, "change": 0.0, "changePct": 0.0, "asOf": "2026-10-27T00:00:00Z"}
    for name, val in (("build_gold", q), ("build_indices", [q]), ("build_crypto", [q]), ("build_usdtry", q)):
        monkeypatch.setattr(markets, name, lambda v=val: v)
    assert build.main() == 1


def test_main_succeeds_on_partial_news_failure(monkeypatch, tmp_path):
    real, real_write = build.build_brief, build.write_site
    partial = lambda now: {"finance": ([], "ok"), **{c: (None, "failed") for c in ("globalPolitics", "turkeyPolitics", "turkeyDaily")}}  # noqa: E731
    monkeypatch.setattr(build, "build_brief", lambda: real(now=NOW, news_builder=partial))
    monkeypatch.setattr(build, "write_site", lambda b, d=tmp_path: real_write(b, d))
    assert build.main() == 0


def test_frankfurter_asof_dst():
    summer = markets.parse_frankfurter_range({"rates": {"2026-10-23": {"TRY": 49.0}}})
    winter = markets.parse_frankfurter_range({"rates": {"2026-12-01": {"TRY": 49.0}}})
    assert summer["asOf"] == "2026-10-23T14:00:00Z" and winter["asOf"] == "2026-12-01T15:00:00Z"


def test_summary_has_no_entities():
    out = news.strip_html("Tom &amp;amp; Jerry &quot;x&quot; &#39;y&#39; &lt;b&gt;z")
    assert "&amp;" not in out and "&quot;" not in out and "&#39;" not in out


def _feed_xml(link: str) -> bytes:
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title><item>'
        f"<title>Some headline</title><link>{link}</link>"
        "<pubDate>Mon, 02 Jun 2025 10:00:00 GMT</pubDate></item></channel></rss>"
    ).encode()


@pytest.mark.parametrize("link", ["/relative/path", "javascript:alert(1)", "mailto:a@b.com"])
def test_invalid_links_dropped(link):
    now = datetime(2025, 6, 2, 12, 0, tzinfo=timezone.utc)
    assert news.parse_entries(_feed_xml(link), FEED, now) == []


def test_protocol_relative_link_resolved_to_https():
    now = datetime(2025, 6, 2, 12, 0, tzinfo=timezone.utc)
    items = news.parse_entries(_feed_xml("//example.com/a"), FEED, now)
    assert [i["url"] for i in items] == ["https://example.com/a"]


def test_normal_https_link_kept():
    now = datetime(2025, 6, 2, 12, 0, tzinfo=timezone.utc)
    items = news.parse_entries(_feed_xml("https://example.com/a?x=1"), FEED, now)
    assert [i["url"] for i in items] == ["https://example.com/a?x=1"]
