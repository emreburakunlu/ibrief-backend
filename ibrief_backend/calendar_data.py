"""Hand-curated macro calendar + holidays, filtered to the upcoming window."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
WINDOW_DAYS = 45


def _window(rows: list[dict], today: date, days: int) -> list[dict]:
    end = today + timedelta(days=days)
    return [r for r in rows if today.isoformat() <= r["date"] <= end.isoformat()]


def macro_calendar(today: date, path: Path = DATA_DIR / "macro.json") -> list[dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))["events"]
    return sorted(_window(rows, today, WINDOW_DAYS), key=lambda r: r["date"])


def holidays(today: date, path: Path = DATA_DIR / "holidays.json") -> list[dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))["holidays"]
    return sorted(_window(rows, today, WINDOW_DAYS), key=lambda r: (r["date"], r["country"]))
