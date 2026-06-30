# CLAUDE.md — Pre-Trade Dashboard

Context for Claude Code working in this repo. Read this first every session.

## What this is
A free, **keyless** pre-market dashboard that walks a discretionary options trader's filter
stack top-to-bottom and answers "do I trade today, and what?" before the open. Python +
Streamlit + DuckDB. No paid data, no API keys.

## File map
- `config.py` — the only file the user edits: tickers, watchlist, thresholds, RSS feeds, F&G URL.
- `market_calendar.py` — holiday-aware expiry + macro-event logic on the real NYSE calendar
  (pandas_market_calendars). FOMC dates are hardcoded in `FOMC_DECISIONS` (2026 verified, 2027 tentative).
- `ingest.py` — pre-market job. Pulls data, computes metrics, snapshots to DuckDB. Pure compute
  functions are separated from network fetchers.
- `dashboard.py` — Streamlit UI. Reads DuckDB **read-only**, renders 8 panels + a GO/CAUTION/
  STAND-DOWN banner, has a "Run ingest now" button (shells out to ingest.py).
- `requirements.txt`, `README.md`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python ingest.py            # writes pretrade.duckdb
streamlit run dashboard.py
```

## Architecture / conventions (do not break)
- **One DuckDB store, two surfaces.** `ingest.py` is the only writer; `dashboard.py` opens
  `read_only=True`. Schedule ingest via cron/launchd pre-market (see README).
- **Upsert depends on column order.** `ingest.upsert()` does `INSERT INTO t SELECT * FROM df`,
  so each DataFrame's columns MUST match the `CREATE TABLE` order. The `*_COLS` constants in
  ingest.py (PRICE_COLS, SNAP_COLS, YIELD_COLS, OPT_COLS, IVATM_COLS, NEWS_COLS, EARN_COLS,
  SENT_COLS) are the source of truth. If you add a column, update the CREATE TABLE, the constant,
  and the row dicts together.
- **Compute functions are pure and unit-tested** (compute_price_metrics, _pullback, iv_rank,
  atm_iv_from_chain, normalize_yield). Keep them pure — no I/O — so they stay testable offline.
- **Tables:** prices, snapshot, yields, options_oi, iv_atm, earnings, sentiment, news, meta.

## Data sources & their limits
- **yfinance** — prices, ^VIX, treasury proxies (^IRX/^FVX/^TNX/^TYX, auto-normalized for legacy
  x10 quoting), option chains, earnings. Free tier is ~15-min delayed. `yf.download` returns a
  MultiIndex when given a list — `_extract()` handles both shapes. Option chains can be empty on
  thin names; everything is guarded.
- **CNN Fear & Greed** — `config.FEAR_GREED_URL` (unofficial dataviz endpoint). Needs a browser
  User-Agent header (set in `fetch_fear_greed`). Returns `fear_and_greed` headline + 7 components.
  If it 403s or the shape changes, the panel shows "—" and the banner drops the sentiment line.
- **Google News RSS** — keyless; keyword-scored in `fetch_news`.
- `DRAM` has no liquid US ETF on Yahoo — expected to return empty; MU/SMH/SOXX are the proxies.
- **IV rank** needs ~30 daily snapshots to populate; **OI delta** needs ≥2 daily snapshots.
- NFP is computed (first Friday, auto-shifts off a closed Friday); CPI/PPI/PCE are NOT fabricated —
  user pastes exact dates into `config.EXTRA_MACRO_EVENTS`.

## Tested vs untested
- **Tested offline (sandbox):** all calendar math vs known 2026 dates, the pure compute funcs,
  DuckDB schema + idempotent upserts, every panel's SQL/joins against a seeded DB, the F&G parser
  against a mocked payload, the banner decision logic.
- **NEVER run in sandbox (no network there):** the live yfinance / CNN / RSS fetches. The first
  `python ingest.py` on a real machine is the true smoke test — expect to debug real-data quirks.

## Extension hooks (where net-new work goes)
- `ingest._pullback()` is a transparent price-only proxy. Swap in the user's ICT/SMC state machine
  + news signal here to replace it. Keep it pure or split I/O out.
- `ingest.fetch_news()` does keyword scoring. Route titles through the user's local Ollama instance
  for real stock-moving classification.
- **Telegram pre-market digest** (planned): new module that reads the same DuckDB tables, formats
  the GO/CAUTION line + top setups, pushes via the user's existing bot. Read-only on the DB.

## Guardrails
- Don't add paid-key dependencies; keyless-by-default is a hard requirement.
- Don't rewrite working modules wholesale — extend. Preserve the upsert column-order contract.
- Keep the dashboard read-only on the DB to avoid lock conflicts with scheduled ingest.
