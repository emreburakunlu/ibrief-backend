# iBrief backend

Builds `site/brief.json`: the **generic** half of the iBrief morning brief (same for anyone). It never receives, stores or publishes holdings, watchlists or any personal data.

Contents: ranked/deduped RSS headlines (finance, global politics, Turkey politics, Turkey daily), gold, index and crypto snapshots, USD/TRY, a 45-day macro calendar (FOMC/CPI/jobs) and market/public holidays. Each block is built independently; a failing source sets `status["<block>"] = "failed"` and an empty/null value, never failing the build.

## Run
```
python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest -q          # offline tests (fixtures only)
.venv/bin/python -m ibrief_backend.build   # writes site/brief.json
```
`brief.sample.json` is a real output kept as a fixture for the app.

## Deploy
`.github/workflows/build.yml` runs daily at 03:00 UTC (GitHub may start it late) and on manual dispatch: tests, build, deploy `site/` to GitHub Pages. Enable Pages with source "GitHub Actions" in the repo settings.

## Sources (free, keyless; verified 2026-10-27)
- News (RSS only; headline, source, link, time and a short feed summary; no article scraping): see `ibrief_backend/feeds.py`.
- Markets: Yahoo chart endpoint (gold futures GC=F, S&P 500, Nasdaq 100, Dow, BIST 100, crypto fallback), gold-api.com spot (gold fallback), CoinGecko (crypto primary), Frankfurter/ECB (USD/TRY; fallback Yahoo). Stooq is no longer usable (bot challenge).
- Macro/holiday data: `data/macro.json` (federalreserve.gov, bls.gov) and `data/holidays.json` (NYSE, nager.date). Update a few times a year; 2027 FOMC dates are tentative and Turkish Bayram dates for 2027 are tentative.

Feed content belongs to its publishers; only headlines/short summaries and links are republished, for personal use.
