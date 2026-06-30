# Pre-Trade Dashboard

A free, keyless morning dashboard that walks your filter stack top-to-bottom and answers
**"do I trade today, and what?"** before the open. No paid data, no API keys.

```
ingest.py   pre-market job -> snapshots free data into DuckDB
dashboard.py  Streamlit UI -> reads DuckDB, renders panels in decision order
config.py   the one file you edit (tickers, thresholds, RSS feeds)
market_calendar.py  holiday-aware expiry + FOMC/NFP/quarter-end logic
```

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python ingest.py          # first pull (a minute or two)
streamlit run dashboard.py
```

The dashboard also has a **Run ingest now** button, so you can refresh without the terminal.

## What each panel does (decision order — the order is the point)

| # | Panel | Question it answers |
|---|-------|---------------------|
| — | **Banner** | GO / CAUTION / STAND DOWN, from the event gate + regime |
| 1 | **Event gate** | Is there an NFP / FOMC / CPI print pre-open? Next OPEX (holiday-adjusted), witching, quarter-end rebalance window |
| 2 | **Regime** | VIX (level/Δ/zone) + CNN **Fear & Greed** (score, rating, components) and SPY/QQQ/IWM/DIA vs 20/50/200 DMA → risk-on / mixed / distribution |
| 3 | **Rates** | 13wk / 5y / 10y / 30y yields + daily bps move, 2s10s proxy |
| 4 | **Sector rotation** | SPDR sectors ranked by relative strength vs SPY |
| 5 | **ETF pullback** | SOXL / SMH / GLD / … holding a rising 20DMA (buyable) vs breaking down |
| 6 | **Watchlist setups** | Pullback flag, distance to MAs, setup score, IV rank, earnings-before-OPEX flag |
| 7 | **Options flow** | OTM-call OI, **overnight OI delta** (opening tell), vol/OI, IV rank, nearest expiry |
| 8 | **Geo/macro news** | Scored headlines (war / escalation / sanctions / tariff / Fed) |

## Scheduling the pre-market pull

**cron** (Linux/macOS), 7:00 AM local, weekdays:
```
0 7 * * 1-5 cd /ABS/PATH/pretrade && /ABS/PATH/.venv/bin/python ingest.py >> ingest.log 2>&1
```

**launchd** (macOS, more reliable on laptops) — `~/Library/LaunchAgents/com.pretrade.ingest.plist`:
```xml
<plist version="1.0"><dict>
  <key>Label</key><string>com.pretrade.ingest</string>
  <key>ProgramArguments</key>
  <array><string>/ABS/PATH/.venv/bin/python</string><string>/ABS/PATH/pretrade/ingest.py</string></array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardErrorPath</key><string>/ABS/PATH/pretrade/ingest.log</string>
</dict></plist>
```
`launchctl load ~/Library/LaunchAgents/com.pretrade.ingest.plist`

## Customize

Everything is in **config.py**: `WATCHLIST`, `PULLBACK_ETFS`, `SECTORS`, MA periods, the
flow thresholds (`VOL_OI_MIN`, `OI_DELTA_MIN`), `IV_RANK_HOT`, and the news `GEO_KEYWORDS`.
FOMC dates live in `market_calendar.FOMC_DECISIONS` (2026 verified, 2027 tentative).

## Known limits (designed around, not hidden)

- **Bought-vs-sold isn't free.** Overnight ΔOI tells you they *added* (opening), not the
  aggressor side. Confirm direction with context + Market Chameleon. The flow panel says so.
- **IV rank needs history.** It shows `—` until ~30 daily snapshots accumulate; ramps up over the
  252-day window. OI delta needs ≥ 2 daily snapshots (available the day after first run).
- **`DRAM`** has no liquid US ETF on Yahoo — it'll show "no data". Use MU / SMH / SOXX as the
  memory/semis proxy (already in the list).
- **Earnings dates** come from yfinance's flaky calendar; treat the ⚠ flag as a prompt to verify,
  not gospel. Missing = blank, everything else still works.
- **NFP/CPI dates**: NFP is computed as first-Friday and auto-shifts off a closed Friday
  (e.g. it correctly lands Thu **Jul 2, 2026** because Jul 3 is the closed Independence-Day obs).
  Still labeled "verify on bls.gov". CPI/PPI/PCE aren't fabricated — paste exact dates into
  `config.EXTRA_MACRO_EVENTS`.
- **Free data is ~15-min delayed.** Fine for a pre-market read; not for intraday execution.
- **Fear & Greed** comes from CNN's unofficial dataviz endpoint (needs a browser User-Agent;
  it's the stock-market index, 0–100, with its 7 components). If CNN changes it, the panel shows
  "—" and the banner just skips the sentiment line — everything else still works. Endpoint is in
  `config.FEAR_GREED_URL`.

## Hooks for your existing stack

- `ingest._pullback()` is a transparent price-only proxy — swap in your ICT/SMC state machine +
  news signal here to replace it.
- `fetch_news()` does keyword scoring — route titles through your local Ollama scorer for real
  stock-moving classification.
- A Telegram pre-market digest is a natural next add: read the same DuckDB tables, format the
  GO/CAUTION line + top setups, push via your bot.
