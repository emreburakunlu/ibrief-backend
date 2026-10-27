"""RSS fetching, normalisation, dedupe and ranking. Pure functions where possible."""
from __future__ import annotations

import calendar
import hashlib
import html
import logging
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

log = logging.getLogger(__name__)

import feedparser
import requests

from .feeds import FEEDS, TIMEOUT, USER_AGENT, Feed

RECENCY_HOURS = 36
CATEGORY_CAP = 15
PER_SOURCE_CAP = 3
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
    for _ in range(3):  # also resolves double-escaped entities like &amp;amp;
        unescaped = html.unescape(text)
        if unescaped == text:
            break
        text = unescaped
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


def clean_link(link: str | None) -> str | None:
    """Return an absolute http(s) URL with a host, or None. '//host/x' resolves to https."""
    if not link:
        return None
    link = link.strip()
    if link.startswith("//"):
        link = "https:" + link
    try:
        parts = urlsplit(link)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    return link


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)
    return None


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_entries(content: bytes, feed: Feed, now: datetime) -> list[dict]:
    """Parse raw feed bytes once into normalised items inside the recency window (no keyword filter)."""
    parsed = feedparser.parse(content)
    cutoff = now - timedelta(hours=RECENCY_HOURS)
    out = []
    bad_links = 0
    for e in parsed.entries:
        title = strip_html(e.get("title"), 200)
        raw_link = e.get("link")
        when = _entry_time(e)
        if not title or not raw_link or not when:
            continue
        link = clean_link(raw_link)
        if link is None:
            bad_links += 1
            continue
        if when < cutoff or when > now + timedelta(hours=1):
            continue
        out.append({
            "id": item_id(link), "title": title, "source": feed.source, "url": link,
            "publishedAt": iso(when), "summary": strip_html(e.get("summary") or e.get("description")),
            "lang": feed.lang, "_when": when, "_weight": feed.weight,
        })
    if bad_links:
        log.warning("%s: dropped %d item(s) with invalid links", feed.source, bad_links)
    return out


def apply_keywords(items: list[dict], feed: Feed) -> list[dict]:
    if not feed.keywords:
        return items
    kw = re.compile(feed.keywords)
    return [i for i in items if kw.search(f"{i['title']} {i['summary']}")]


def parse_feed(content: bytes, feed: Feed, now: datetime) -> list[dict]:
    """Turn raw feed bytes into normalised items inside the recency window."""
    return apply_keywords(parse_entries(content, feed, now), feed)


def dedupe_and_rank(items: list[dict], now: datetime, cap: int = CATEGORY_CAP,
                    exclude_norms: list[str] | None = None) -> list[dict]:
    """Cluster near-duplicate titles, score clusters, return up to `cap` public items.

    A cluster's lead is its newest member from a source that is not yet at PER_SOURCE_CAP;
    if every member's source is capped the story is dropped.
    """
    clusters: list[dict] = []
    seen_ids: set[str] = set()
    for it in sorted(items, key=lambda x: x["_when"], reverse=True):
        if it["id"] in seen_ids:
            continue
        seen_ids.add(it["id"])
        norm = normalize_title(it["title"])
        if exclude_norms and any(similar(norm, e) for e in exclude_norms):
            continue
        for c in clusters:
            if similar(norm, c["norm"]):
                c["members"].append(it)
                break
        else:
            clusters.append({"norm": norm, "members": [it]})
    for c in clusters:
        head = c["members"][0]
        c["sources"] = {m["source"] for m in c["members"]}
        age_h = max(0.0, (now - head["_when"]).total_seconds() / 3600)
        recency = max(0.0, 1 - age_h / RECENCY_HOURS)
        coverage = min(len(c["sources"]) - 1, 3) * 0.6
        c["score"] = (recency + coverage) * head["_weight"]
        c["_when"] = head["_when"]
    clusters.sort(key=lambda c: (c["score"], c["_when"]), reverse=True)
    picked, per_source = [], {}
    for c in clusters:
        lead = next((m for m in c["members"] if per_source.get(m["source"], 0) < PER_SOURCE_CAP), None)
        if lead is None:
            continue
        lead = dict(lead)
        if not lead["summary"]:
            lead["summary"] = next((m["summary"] for m in c["members"] if m["summary"]), "")
        per_source[lead["source"]] = per_source.get(lead["source"], 0) + 1
        picked.append(lead)
        if len(picked) >= cap:
            break
    public = ("id", "title", "source", "url", "publishedAt", "summary", "lang")
    return [{k: it[k] for k in public} for it in picked]


def fetch_feed(feed: Feed, session: requests.Session | None = None) -> bytes:
    get = (session or requests).get
    r = get(feed.url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.content


def prefetch(feeds: list[Feed], fetch=fetch_feed, now: datetime | None = None) -> dict[str, list[dict] | Exception]:
    """Fetch (concurrently) and parse every distinct feed URL exactly once.

    Returns {url: parsed items (no keyword filter) | Exception}.
    """
    now = now or datetime.now(timezone.utc)
    distinct = {f.url: f for f in feeds}

    def work(feed: Feed):
        try:
            return feed.url, parse_entries(fetch(feed), feed, now)
        except Exception as exc:  # noqa: BLE001 - any failure isolates to this feed
            return feed.url, exc

    with ThreadPoolExecutor(max_workers=8) as ex:
        return dict(ex.map(work, distinct.values()))


def collect_category(feeds: list[Feed], parsed: dict) -> tuple[list[dict], int]:
    """Gather a category's items from prefetched results. Returns (items, ok_feed_count)."""
    items, ok = [], 0
    for feed in feeds:
        res = parsed.get(feed.url)
        if res is None or isinstance(res, Exception):
            continue
        ok += 1
        items.extend(apply_keywords(res, feed))
    return items, ok


def build_news(fetch=fetch_feed, now: datetime | None = None) -> dict[str, tuple[list[dict] | None, str]]:
    """Return {category: (items|None, status)}. Each category is isolated."""
    now = now or datetime.now(timezone.utc)
    parsed = prefetch([f for fs in FEEDS.values() for f in fs], fetch=fetch, now=now)
    out: dict = {}
    politics_norms: list[str] = []
    for cat in ("finance", "globalPolitics", "turkeyPolitics", "turkeyDaily"):
        try:
            items, ok = collect_category(FEEDS[cat], parsed)
            if ok == 0:
                out[cat] = (None, "failed")
                continue
            ranked = dedupe_and_rank(items, now, exclude_norms=politics_norms if cat == "turkeyDaily" else None)
            if cat == "turkeyPolitics":
                politics_norms = [normalize_title(i["title"]) for i in ranked]
            out[cat] = (ranked, "ok")
        except Exception:  # noqa: BLE001
            out[cat] = (None, "failed")
    return out
