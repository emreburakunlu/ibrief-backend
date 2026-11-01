"""Entry point: python -m ibrief_backend.build  ->  site/brief.json"""
from __future__ import annotations

import json
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import calendar_data, markets, news

NEWS_CATEGORIES = ("finance", "globalPolitics", "turkeyPolitics", "turkeyDaily")
SITE_DIR = Path(__file__).resolve().parent.parent / "site"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

PLACEHOLDER = """<!doctype html><meta charset="utf-8"><title>iBrief backend</title>
<p>Generic data feed for a personal app. <a href="brief.json">brief.json</a></p>
"""


def safe(fn, empty):
    """Run a block builder; never raise. Returns (value, status)."""
    try:
        return fn(), "ok"
    except Exception as exc:  # noqa: BLE001
        print(f"block failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return empty, "failed"


def build_brief(now: datetime | None = None, news_builder=news.build_news) -> dict:
    now = now or datetime.now(timezone.utc)
    status: dict[str, str] = {}

    news_out, _ = safe(lambda: news_builder(now=now), {})
    news_block = {}
    for cat in NEWS_CATEGORIES:
        items, st = news_out.get(cat, (None, "failed"))
        news_block[cat] = items or []
        status[f"news.{cat}"] = st

    # market blocks run concurrently so one hung upstream cannot serialise the timeouts
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {
            "gold": ex.submit(safe, lambda: markets.build_gold(), None),
            "indices": ex.submit(safe, lambda: markets.build_indices(), []),
            "crypto": ex.submit(safe, lambda: markets.build_crypto(), []),
            "fx": ex.submit(safe, lambda: markets.build_usdtry(), None),
        }
        res = {k: f.result() for k, f in futs.items()}
    gold, status["gold"] = res["gold"]
    indices, status["indices"] = res["indices"]
    crypto, status["crypto"] = res["crypto"]
    usdtry, status["fx"] = res["fx"]
    macro, status["macroCalendar"] = safe(lambda: calendar_data.macro_calendar(now.date()), [])
    hols, status["holidays"] = safe(lambda: calendar_data.holidays(now.date()), [])

    return {
        "meta": {"schemaVersion": 1, "generatedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ")},
        "news": news_block,
        "markets": {"gold": gold, "indices": indices, "crypto": crypto},
        "fx": {"USDTRY": usdtry},
        "macroCalendar": macro,
        "holidays": hols,
        "status": status,
    }


def write_site(brief: dict, site_dir: Path = SITE_DIR) -> Path:
    site_dir.mkdir(parents=True, exist_ok=True)
    out = site_dir / "brief.json"
    out.write_text(json.dumps(brief, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (site_dir / "index.html").write_text(PLACEHOLDER, encoding="utf-8")
    for page in STATIC_DIR.glob("*.html"):
        shutil.copy(page, site_dir / page.name)
    return out


def main() -> int:
    brief = build_brief()
    if all(brief["status"][f"news.{c}"] == "failed" for c in NEWS_CATEGORIES):
        print("all news blocks failed; refusing to publish (keeping last good deploy)", file=sys.stderr)
        return 1
    path = write_site(brief)
    print(f"wrote {path} ({path.stat().st_size} bytes)")
    for k, v in brief["status"].items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
