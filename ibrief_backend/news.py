"""RSS fetching, normalisation, dedupe and ranking. Pure functions where possible."""
from __future__ import annotations

import calendar
import hashlib
import html
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests

from .feeds import FEEDS, TIMEOUT, USER_AGENT, Feed

RECENCY_HOURS = 36
CATEGORY_CAP = 15
PER_SOURCE_CAP = 5
SUMMARY_MAX = 300
TRACKING_PARAMS = {"fbclid", "gclid", "ocid", "cmpid", "ref", "ito", "at_medium", "at_campaign"}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_STOP = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "is", "at", "as", "by", "ve", "bir", "bu", "icin", "ile"}


def strip_html(text: str | None, limit: int = SUMMARY_MAX) -> str:
    if not text:
        return ""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:.-") + "…"
    return text


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query)
             if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def item_id(url: str) -> str:
    return hashlib.sha1(canonical_url(url).encode("utf-8")).hexdigest()[:12]


def normalize_title(title: str) -> str:
    t = title.replace("İ", "i").replace("I", "ı").lower()
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c)).replace("ı", "i")
    t = re.sub(r"[^\w\s]", " ", t)
    return _WS_RE.sub(" ", t).strip()


def _tokens(norm: str) -> set[str]:
    return {w for w in norm.split() if w not in _STOP and len(w) > 1}


def similar(a_norm: str, b_norm: str) -> bool:
    if not a_norm or not b_norm:
        return False
    if SequenceMatcher(None, a_norm, b_norm).ratio() >= 0.85:
        return True
    ta, tb = _tokens(a_norm), _tokens(b_norm)
    if len(ta) >= 4 and len(tb) >= 4:
        return len(ta & tb) / len(ta | tb) >= 0.6
    return False


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)
    return None


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_feed(content: bytes, feed: Feed, now: datetime) -> list[dict]:
    """Turn raw feed bytes into normalised items inside the recency window."""
    parsed = feedparser.parse(content)
    cutoff = now - timedelta(hours=RECENCY_HOURS)
    kw = re.compile(feed.keywords) if feed.keywords else None
    out = []
    for e in parsed.entries:
        title = strip_html(e.get("title"), 200)
        link = e.get("link")
        when = _entry_time(e)
        if not title or not link or not when:
            continue
        if when < cutoff or when > now + timedelta(hours=1):
            continue
        summary = strip_html(e.get("summary") or e.get("description"))
        if kw and not kw.search(f"{title} {summary}"):
            continue
        out.append({
            "id": item_id(link), "title": title, "source": feed.source, "url": link.strip(),
            "publishedAt": iso(when), "summary": summary, "lang": feed.lang,
            "_when": when, "_weight": feed.weight,
        })
    return out


def dedupe_and_rank(items: list[dict], now: datetime, cap: int = CATEGORY_CAP) -> list[dict]:
    """Cluster near-duplicate titles, score clusters, return up to `cap` public items."""
    clusters: list[dict] = []
    seen_ids: set[str] = set()
    for it in sorted(items, key=lambda x: x["_when"], reverse=True):
        if it["id"] in seen_ids:
            continue
        seen_ids.add(it["id"])
        norm = normalize_title(it["title"])
        for c in clusters:
            if similar(norm, c["norm"]):
                c["sources"].add(it["source"])
                # prefer the member with a summary as representative
                if not c["rep"]["summary"] and it["summary"]:
                    c["rep"]["summary"] = it["summary"]
                break
        else:
            clusters.append({"norm": norm, "rep": dict(it), "sources": {it["source"]}})
    for c in clusters:
        age_h = max(0.0, (now - c["rep"]["_when"]).total_seconds() / 3600)
        recency = max(0.0, 1 - age_h / RECENCY_HOURS)
        coverage = min(len(c["sources"]) - 1, 3) * 0.6
        c["score"] = (recency + coverage) * c["rep"]["_weight"]
    clusters.sort(key=lambda c: (c["score"], c["rep"]["_when"]), reverse=True)
    picked, per_source = [], {}
    for c in clusters:
        src = c["rep"]["source"]
        if per_source.get(src, 0) >= PER_SOURCE_CAP:
            continue
        per_source[src] = per_source.get(src, 0) + 1
        picked.append(c)
        if len(picked) >= cap:
            break
    public = ("id", "title", "source", "url", "publishedAt", "summary", "lang")
    return [{k: c["rep"][k] for k in public} for c in picked]


def fetch_feed(feed: Feed, session: requests.Session | None = None) -> bytes:
    get = (session or requests).get
    r = get(feed.url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.content


def collect_category(feeds: list[Feed], fetch=fetch_feed, now: datetime | None = None,
                     exclude_ids: set[str] | None = None, cache: dict | None = None):
    """Fetch all feeds of a category concurrently. Returns (items, ok_feed_count).

    `cache` maps feed.url -> bytes|Exception so shared feeds are fetched only once.
    """
    now = now or datetime.now(timezone.utc)
    cache = cache if cache is not None else {}

    def get(feed: Feed):
        if feed.url not in cache:
            try:
                cache[feed.url] = fetch(feed)
            except Exception as exc:  # noqa: BLE001 - any failure isolates to this feed
                cache[feed.url] = exc
        return feed, cache[feed.url]

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(get, feeds))
    items, ok = [], 0
    for feed, content in results:
        if isinstance(content, Exception):
            continue
        ok += 1
        items.extend(parse_feed(content, feed, now))
    if exclude_ids:
        items = [i for i in items if i["id"] not in exclude_ids]
    return items, ok


def build_news(fetch=fetch_feed, now: datetime | None = None) -> dict[str, tuple[list[dict] | None, str]]:
    """Return {category: (items|None, status)}. Each category is isolated."""
    now = now or datetime.now(timezone.utc)
    # prefetch every distinct URL once, concurrently
    all_feeds = {f.url: f for fs in FEEDS.values() for f in fs}
    cache: dict = {}
    collect_category(list(all_feeds.values()), fetch=fetch, now=now, cache=cache)
    out: dict = {}
    politics_ids: set[str] = set()
    for cat in ("finance", "globalPolitics", "turkeyPolitics", "turkeyDaily"):
        try:
            items, ok = collect_category(FEEDS[cat], fetch=fetch, now=now, cache=cache,
                                         exclude_ids=politics_ids if cat == "turkeyDaily" else None)
            if ok == 0:
                out[cat] = (None, "failed")
                continue
            ranked = dedupe_and_rank(items, now)
            if cat == "turkeyPolitics":
                politics_ids = {i["id"] for i in ranked}
            out[cat] = (ranked, "ok")
        except Exception:  # noqa: BLE001
            out[cat] = (None, "failed")
    return out
