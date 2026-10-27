"""Generic market snapshot: gold, indices, crypto, USD/TRY. All keyless, free sources.

Verified 2026-10-27:
  - Yahoo chart endpoint (unofficial, keyless): GC=F, ^GSPC, ^NDX, ^DJI, XU100.IS, BTC-USD... OK.
  - Stooq CSV: now behind a JS bot challenge -> unusable from CI, NOT used.
  - gold-api.com /price/XAU (spot, no change data): OK, used as gold fallback.
  - Frankfurter (ECB) api.frankfurter.dev/v1 incl. date ranges: OK.
  - CoinGecko simple/price: unreachable from the dev network (connection reset), so Yahoo
    BTC-USD/ETH-USD/SOL-USD is the fallback and was verified instead.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from .feeds import TIMEOUT, USER_AGENT

HEADERS = {"User-Agent": USER_AGENT}
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"


def _get_json(url: str, **params):
    r = requests.get(url, params=params or None, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def make_quote(symbol: str, name: str, price: float, prev: float | None, as_of: datetime) -> dict:
    change = changePct = None
    if prev:
        change = round(price - prev, 4)
        changePct = round((price - prev) / prev * 100, 3)
    return {"symbol": symbol, "name": name, "price": round(price, 4), "change": change,
            "changePct": changePct, "asOf": as_of.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def parse_yahoo_chart(payload: dict, symbol: str, name: str) -> dict:
    result = payload["chart"]["result"][0]
    meta = result["meta"]
    price = float(meta["regularMarketPrice"])
    closes = [c for c in result["indicators"]["quote"][0].get("close", []) if c is not None]
    prev = None
    if closes:
        # last daily candle is today's (live) when it equals the price; then previous is the one before
        if abs(closes[-1] - price) <= abs(price) * 1e-6 and len(closes) >= 2:
            prev = closes[-2]
        else:
            prev = closes[-1]
    as_of = datetime.fromtimestamp(meta.get("regularMarketTime") or result["timestamp"][-1], tz=timezone.utc)
    return make_quote(symbol, name, price, prev, as_of)


def yahoo_quote(yahoo_symbol: str, symbol: str, name: str) -> dict:
    return parse_yahoo_chart(_get_json(YAHOO.format(symbol=yahoo_symbol.replace("^", "%5E"))), symbol, name)


# ---- gold -------------------------------------------------------------------------------
def gold_yahoo() -> dict:
    return yahoo_quote("GC=F", "XAUUSD", "Gold (USD/oz)")


def gold_spot_fallback() -> dict:
    d = _get_json("https://api.gold-api.com/price/XAU")
    as_of = datetime.fromisoformat(d["updatedAt"].replace("Z", "+00:00"))
    return make_quote("XAUUSD", "Gold (USD/oz)", float(d["price"]), None, as_of)


def first_working(*fns):
    last = None
    for fn in fns:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise last  # type: ignore[misc]


def build_gold() -> dict:
    return first_working(gold_yahoo, gold_spot_fallback)


# ---- indices ----------------------------------------------------------------------------
INDICES = [("^GSPC", "SPX", "S&P 500"), ("^NDX", "NDX", "Nasdaq 100"),
           ("^DJI", "DJI", "Dow Jones"), ("XU100.IS", "XU100", "BIST 100")]


def build_indices(fetch=yahoo_quote) -> list[dict]:
    out = []
    for ys, sym, name in INDICES:
        try:
            out.append(fetch(ys, sym, name))
        except Exception:  # noqa: BLE001 - one index failing keeps the others
            continue
    if not out:
        raise RuntimeError("no index quote available")
    return out


# ---- crypto -----------------------------------------------------------------------------
CRYPTO = [("bitcoin", "BTC-USD", "BTC", "Bitcoin"), ("ethereum", "ETH-USD", "ETH", "Ethereum"),
          ("solana", "SOL-USD", "SOL", "Solana")]


def parse_coingecko(payload: dict) -> list[dict]:
    out = []
    for cg_id, _y, sym, name in CRYPTO:
        d = payload.get(cg_id)
        if not d or "usd" not in d:
            continue
        price = float(d["usd"])
        pct = d.get("usd_24h_change")
        prev = price / (1 + pct / 100) if pct is not None else None
        ts = d.get("last_updated_at")
        as_of = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else datetime.now(timezone.utc)
        out.append(make_quote(sym, name, price, prev, as_of))
    if not out:
        raise ValueError("coingecko payload had no coins")
    return out


def crypto_coingecko() -> list[dict]:
    payload = _get_json("https://api.coingecko.com/api/v3/simple/price",
                        ids=",".join(c[0] for c in CRYPTO), vs_currencies="usd",
                        include_24hr_change="true", include_last_updated_at="true")
    return parse_coingecko(payload)


def crypto_yahoo() -> list[dict]:
    out = []
    for _cg, ys, sym, name in CRYPTO:
        try:
            out.append(yahoo_quote(ys, sym, name))
        except Exception:  # noqa: BLE001
            continue
    if not out:
        raise RuntimeError("no crypto quote available")
    return out


def build_crypto() -> list[dict]:
    return first_working(crypto_coingecko, crypto_yahoo)


# ---- fx ---------------------------------------------------------------------------------
def parse_frankfurter_range(payload: dict) -> dict:
    rates = payload["rates"]
    days = sorted(rates)
    last = days[-1]
    price = float(rates[last]["TRY"])
    prev = float(rates[days[-2]]["TRY"]) if len(days) >= 2 else None
    as_of = datetime.fromisoformat(last).replace(tzinfo=timezone.utc) + timedelta(hours=15)  # ECB ~16:00 CET
    return make_quote("USDTRY", "US Dollar / Turkish Lira", price, prev, as_of)


def fx_frankfurter() -> dict:
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=10)
    payload = _get_json(f"https://api.frankfurter.dev/v1/{start}..{today}", base="USD", symbols="TRY")
    return parse_frankfurter_range(payload)


def fx_yahoo() -> dict:
    return yahoo_quote("TRY=X", "USDTRY", "US Dollar / Turkish Lira")


def build_usdtry() -> dict:
    return first_working(fx_frankfurter, fx_yahoo)
