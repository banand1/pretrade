"""
ingest.py
---------
Pre-market data job. Run on a schedule (cron / launchd) BEFORE the open, or hit the
"Run ingest now" button in the dashboard. Pulls free data and snapshots to DuckDB:

  prices       rolling OHLCV history (for MAs / IV-rank / pullback math)
  snapshot     per-symbol daily metrics (trend, pullback flag, setup score)
  yields       treasury proxies + daily bps change
  options_oi   OTM-call snapshot per watchlist name (OI / vol / IV / vol-OI)
  iv_atm       daily ATM IV per name (history -> IV rank)
  news         scored geo/macro headlines
  meta         last-ingest timestamp

Data sources (all free / keyless): yfinance (prices, yields, option chains),
Google-News RSS (headlines). No paid keys required.

Compute functions (compute_price_metrics / pullback / iv_rank / atm_iv) are pure and
unit-tested offline; the yfinance/RSS fetchers are thin wrappers around them.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import warnings

import duckdb
import numpy as np
import pandas as pd

import config as C

warnings.filterwarnings("ignore")

PRICE_COLS = ["symbol", "date", "open", "high", "low", "close", "volume"]
SNAP_COLS = [
    "snapshot_date", "symbol", "kind", "close", "prev_close", "pct_1d", "pct_5d",
    "pct_20d", "sma10", "sma20", "sma50", "sma200", "above20", "above50", "above200",
    "high20", "off_high20_pct", "dist20_pct", "dist50_pct", "dist200_pct", "atr14",
    "pullback_flag", "setup_score",
]
YIELD_COLS = ["snapshot_date", "tenor", "label", "yld", "chg_bps"]
OPT_COLS = [
    "snapshot_date", "symbol", "expiry", "strike", "type", "oi", "volume", "iv",
    "spot", "vol_oi", "otm_pct",
]
IVATM_COLS = ["symbol", "date", "atm_iv"]
NEWS_COLS = ["snapshot_date", "ts", "source", "title", "link", "score"]
EARN_COLS = ["symbol", "next_earnings"]
SENT_COLS = ["snapshot_date", "metric", "score", "rating",
             "prev_close", "prev_week", "prev_month", "prev_year"]


# --------------------------------------------------------------------------- #
# pure compute
# --------------------------------------------------------------------------- #
def normalize_yield(v: float | None) -> float | None:
    """^TNX et al. are sometimes quoted x10 (e.g. 42.5 == 4.25%). Collapse to %."""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    return round(v / 10.0, 3) if v > 20 else round(float(v), 3)


def _f(x) -> float | None:
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(x) else x


def compute_price_metrics(df: pd.DataFrame) -> dict:
    """df: single symbol OHLCV, ascending date index. Returns latest-bar metrics."""
    df = df.dropna(subset=["close"]).sort_index()
    if df.empty:
        return {}
    close = df["close"]
    n = len(close)
    out: dict = {
        "close": _f(close.iloc[-1]),
        "prev_close": _f(close.iloc[-2]) if n >= 2 else None,
    }
    out["pct_1d"] = _pct(out["close"], out["prev_close"])
    out["pct_5d"] = _pct(out["close"], _f(close.iloc[-6]) if n >= 6 else None)
    out["pct_20d"] = _pct(out["close"], _f(close.iloc[-21]) if n >= 21 else None)
    for p in (10, 20, 50, 200):
        out[f"sma{p}"] = _f(close.rolling(p).mean().iloc[-1]) if n >= p else None
    c = out["close"]
    out["above20"] = bool(c and out["sma20"] and c >= out["sma20"])
    out["above50"] = bool(c and out["sma50"] and c >= out["sma50"])
    out["above200"] = bool(c and out["sma200"] and c >= out["sma200"])
    hi = _f(close.rolling(C.PULLBACK_HIGH_LOOKBACK).max().iloc[-1]) if n >= 2 else None
    out["high20"] = hi
    out["off_high20_pct"] = round((hi - c) / hi * 100, 2) if (hi and c) else None
    out["dist20_pct"] = _pct(c, out["sma20"])
    out["dist50_pct"] = _pct(c, out["sma50"])
    out["dist200_pct"] = _pct(c, out["sma200"])
    out["atr14"] = _atr(df, 14)
    flag, score = _pullback(out)
    out["pullback_flag"] = flag
    out["setup_score"] = score
    return out


def _pct(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return round((a - b) / b * 100, 2)


def _atr(df: pd.DataFrame, period: int) -> float | None:
    if len(df) < period + 1:
        return None
    h, l, pc = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return _f(tr.rolling(period).mean().iloc[-1])


def _pullback(m: dict) -> tuple[bool, int]:
    """Transparent price-only pullback proxy. Swap in your news/state-machine signal
    here — this is the scaffold, not the edge."""
    near_high = m.get("off_high20_pct") is not None and m["off_high20_pct"] <= C.PULLBACK_NEAR_HIGH_PCT
    s20, s50, c, hi = m.get("sma20"), m.get("sma50"), m.get("close"), m.get("high20")
    uptrend = bool(s20 and s50 and s20 >= s50)
    holding = bool(c and s20 and c >= s20)
    pulled = bool(c and hi and c < hi)
    flag = near_high and uptrend and holding and pulled
    score = sum([
        m.get("above20", False), m.get("above50", False), m.get("above200", False),
        flag, bool(flag and (m.get("pct_1d") or 0) > 0),
    ])
    return flag, int(score)


def iv_rank(hist: pd.Series, window: int = 252) -> float | None:
    """IV rank = (cur - min) / (max - min) over the trailing window. None if thin."""
    s = pd.Series(hist).dropna()
    if len(s) < 30:
        return None
    s = s.iloc[-window:]
    lo, hi, cur = s.min(), s.max(), s.iloc[-1]
    if hi == lo:
        return None
    return round((cur - lo) / (hi - lo) * 100, 1)


def realized_vol(closes: pd.Series, window: int = 20) -> float | None:
    """Annualized realized volatility (%) over the trailing window."""
    closes = closes.dropna()
    if len(closes) < window + 1:
        return None
    log_ret = np.log(closes / closes.shift(1)).dropna()
    if len(log_ret) < window:
        return None
    return round(float(log_ret.iloc[-window:].std() * np.sqrt(252) * 100), 1)


def atm_iv_from_chain(calls: pd.DataFrame, puts: pd.DataFrame, spot: float) -> float | None:
    ivs = []
    for chain in (calls, puts):
        if chain is None or chain.empty or "impliedVolatility" not in chain:
            continue
        row = chain.iloc[(chain["strike"] - spot).abs().argsort().iloc[0]]
        iv = _f(row.get("impliedVolatility"))
        if iv:
            ivs.append(iv)
    return round(float(np.mean(ivs)) * 100, 1) if ivs else None


# --------------------------------------------------------------------------- #
# DuckDB layer
# --------------------------------------------------------------------------- #
def connect(path: str = C.DB_PATH) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(path)


def create_tables(con) -> None:
    con.execute("""
    CREATE TABLE IF NOT EXISTS prices(
      symbol VARCHAR, date DATE, open DOUBLE, high DOUBLE, low DOUBLE,
      close DOUBLE, volume BIGINT, PRIMARY KEY(symbol, date));
    CREATE TABLE IF NOT EXISTS snapshot(
      snapshot_date DATE, symbol VARCHAR, kind VARCHAR, close DOUBLE, prev_close DOUBLE,
      pct_1d DOUBLE, pct_5d DOUBLE, pct_20d DOUBLE, sma10 DOUBLE, sma20 DOUBLE,
      sma50 DOUBLE, sma200 DOUBLE, above20 BOOLEAN, above50 BOOLEAN, above200 BOOLEAN,
      high20 DOUBLE, off_high20_pct DOUBLE, dist20_pct DOUBLE, dist50_pct DOUBLE,
      dist200_pct DOUBLE, atr14 DOUBLE, pullback_flag BOOLEAN, setup_score INTEGER,
      PRIMARY KEY(snapshot_date, symbol));
    CREATE TABLE IF NOT EXISTS yields(
      snapshot_date DATE, tenor VARCHAR, label VARCHAR, yld DOUBLE, chg_bps DOUBLE,
      PRIMARY KEY(snapshot_date, tenor));
    CREATE TABLE IF NOT EXISTS options_oi(
      snapshot_date DATE, symbol VARCHAR, expiry DATE, strike DOUBLE, type VARCHAR,
      oi BIGINT, volume BIGINT, iv DOUBLE, spot DOUBLE, vol_oi DOUBLE, otm_pct DOUBLE,
      PRIMARY KEY(snapshot_date, symbol, expiry, strike, type));
    CREATE TABLE IF NOT EXISTS iv_atm(
      symbol VARCHAR, date DATE, atm_iv DOUBLE, PRIMARY KEY(symbol, date));
    CREATE TABLE IF NOT EXISTS news(
      snapshot_date DATE, ts TIMESTAMP, source VARCHAR, title VARCHAR, link VARCHAR,
      score INTEGER, PRIMARY KEY(snapshot_date, link));
    CREATE TABLE IF NOT EXISTS earnings(
      symbol VARCHAR PRIMARY KEY, next_earnings DATE);
    CREATE TABLE IF NOT EXISTS sentiment(
      snapshot_date DATE, metric VARCHAR, score DOUBLE, rating VARCHAR,
      prev_close DOUBLE, prev_week DOUBLE, prev_month DOUBLE, prev_year DOUBLE,
      PRIMARY KEY(snapshot_date, metric));
    CREATE TABLE IF NOT EXISTS meta(key VARCHAR PRIMARY KEY, value VARCHAR);
    """)


def upsert(con, table: str, df: pd.DataFrame, keys: list[str], cols: list[str]) -> None:
    if df is None or df.empty:
        return
    df = df[cols].copy()
    con.register("tmp_df", df)
    cond = " AND ".join(f"{table}.{k} = tmp_df.{k}" for k in keys)
    con.execute(f"DELETE FROM {table} WHERE EXISTS (SELECT 1 FROM tmp_df WHERE {cond})")
    con.execute(f"INSERT INTO {table} SELECT * FROM tmp_df")
    con.unregister("tmp_df")


# --------------------------------------------------------------------------- #
# yfinance / RSS fetchers
# --------------------------------------------------------------------------- #
def _extract(all_df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Pull one ticker's OHLCV out of a yf.download(group_by='ticker') frame."""
    try:
        if isinstance(all_df.columns, pd.MultiIndex):
            if ticker not in all_df.columns.get_level_values(0):
                return pd.DataFrame()
            d = all_df[ticker].copy()
        else:
            d = all_df.copy()
    except Exception:
        return pd.DataFrame()
    d.columns = [str(c).lower() for c in d.columns]
    keep = [c for c in ["open", "high", "low", "close", "volume"] if c in d.columns]
    return d[keep].dropna(how="all")


def fetch_prices(symbols: list[str]) -> dict[str, pd.DataFrame]:
    import yfinance as yf
    syms = sorted(set(symbols))
    raw = yf.download(syms, period=f"{C.HIST_DAYS}d", interval="1d",
                      group_by="ticker", auto_adjust=True, threads=True, progress=False)
    out = {}
    for s in syms:
        d = _extract(raw, s)
        if not d.empty:
            out[s] = d
    return out


def fetch_options(symbol: str):
    """Return (list[option rows], atm_iv) for nearest expiries; ([],None) on failure."""
    import yfinance as yf
    rows, atm = [], None
    try:
        tk = yf.Ticker(symbol)
        spot = _f(tk.fast_info.get("last_price")) if hasattr(tk, "fast_info") else None
        if not spot:
            h = tk.history(period="5d")
            spot = _f(h["Close"].iloc[-1]) if not h.empty else None
        if not spot:
            return [], None
        expiries = list(tk.options or [])[: C.MAX_EXPIRIES]
        for i, exp in enumerate(expiries):
            ch = tk.option_chain(exp)
            calls, puts = ch.calls, ch.puts
            if i == 0:
                atm = atm_iv_from_chain(calls, puts, spot)
            hi = spot * (1 + C.OTM_CALL_MAX_PCT / 100.0)
            otm = calls[(calls["strike"] >= spot) & (calls["strike"] <= hi)]
            for _, r in otm.iterrows():
                oi = int(r.get("openInterest") or 0)
                vol = int(r.get("volume") or 0)
                rows.append({
                    "expiry": pd.to_datetime(exp).date(), "strike": float(r["strike"]),
                    "type": "C", "oi": oi, "volume": vol,
                    "iv": round(_f(r.get("impliedVolatility")) * 100, 1) if _f(r.get("impliedVolatility")) else None,
                    "spot": round(spot, 2), "vol_oi": round(vol / max(oi, 1), 2),
                    "otm_pct": round((float(r["strike"]) - spot) / spot * 100, 1),
                })
    except Exception as e:
        print(f"  ! options {symbol}: {e}", file=sys.stderr)
    return rows, atm


def fetch_earnings(symbol: str) -> dt.date | None:
    """Best-effort next earnings date. yfinance's calendar is flaky -> guard hard."""
    import yfinance as yf
    try:
        tk = yf.Ticker(symbol)
        ed = tk.get_earnings_dates(limit=8)
        if ed is not None and not ed.empty:
            future = [d.date() for d in ed.index.to_pydatetime() if d.date() >= dt.date.today()]
            if future:
                return min(future)
        cal = getattr(tk, "calendar", None)
        if isinstance(cal, dict):
            vals = cal.get("Earnings Date") or []
            ds = [v if isinstance(v, dt.date) else pd.to_datetime(v).date() for v in (vals if isinstance(vals, list) else [vals])]
            ds = [d for d in ds if d >= dt.date.today()]
            if ds:
                return min(ds)
    except Exception as e:
        print(f"  ! earnings {symbol}: {e}", file=sys.stderr)
    return None


def fetch_fear_greed() -> list[dict]:
    """CNN stock-market Fear & Greed (0-100) + its 7 components. Unofficial dataviz
    endpoint — needs a browser UA; degrades to [] on any failure."""
    import requests
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/json",
    }
    rows: list[dict] = []
    try:
        r = requests.get(C.FEAR_GREED_URL, headers=headers, timeout=15)
        r.raise_for_status()
        j = r.json()
        fg = j.get("fear_and_greed", {}) or {}
        if fg.get("score") is not None:
            rows.append({
                "metric": "fng", "score": _f(fg.get("score")), "rating": fg.get("rating"),
                "prev_close": _f(fg.get("previous_close")),
                "prev_week": _f(fg.get("previous_1_week")),
                "prev_month": _f(fg.get("previous_1_month")),
                "prev_year": _f(fg.get("previous_1_year")),
            })
        comps = {
            "momentum": "market_momentum_sp500", "strength": "stock_price_strength",
            "breadth": "stock_price_breadth", "put_call": "put_call_options",
            "volatility": "market_volatility_vix", "safe_haven": "safe_haven_demand",
            "junk_bond": "junk_bond_demand",
        }
        for label, key in comps.items():
            c = j.get(key) or {}
            if c.get("score") is not None:
                rows.append({"metric": f"fng_{label}", "score": _f(c.get("score")),
                             "rating": c.get("rating"), "prev_close": None,
                             "prev_week": None, "prev_month": None, "prev_year": None})
    except Exception as e:
        print(f"  ! fear&greed: {e}", file=sys.stderr)
    return rows


def fetch_news() -> list[dict]:
    import feedparser
    items = []
    for url in C.NEWS_FEEDS:
        try:
            feed = feedparser.parse(url)
            src = feed.feed.get("title", "RSS")
            for e in feed.entries[:25]:
                title = e.get("title", "")
                score = sum(w for k, w in C.GEO_KEYWORDS.items() if k in title.lower())
                items.append({
                    "ts": pd.Timestamp(getattr(e, "published", dt.datetime.now())),
                    "source": src, "title": title,
                    "link": e.get("link", ""), "score": int(score),
                })
        except Exception as e:
            print(f"  ! news {url[:40]}: {e}", file=sys.stderr)
    return items


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def kind_of(sym: str) -> str:
    if sym in C.INDICES or sym == C.VIX:
        return "index"
    if sym in C.FUTURES:
        return "futures"
    if sym in C.SECTORS:
        return "sector"
    if sym in C.PULLBACK_ETFS:
        return "etf"
    return "watch"


def run(today: dt.date | None = None) -> None:
    today = today or dt.date.today()
    con = connect()
    create_tables(con)

    universe = list(C.INDICES) + [C.VIX] + list(C.YIELDS) + list(C.FUTURES) \
        + list(C.SECTORS) + C.PULLBACK_ETFS + C.WATCHLIST
    print(f"[{today}] fetching {len(set(universe))} symbols ...")
    prices = fetch_prices(universe)
    missing = sorted(set(universe) - set(prices))
    if missing:
        print(f"  no data for: {', '.join(missing)}")

    # prices history (upsert last ~5 sessions per symbol to keep it light)
    prow = []
    for s, d in prices.items():
        for idx, r in d.iterrows():
            prow.append({"symbol": s, "date": pd.to_datetime(idx).date(),
                         "open": _f(r.get("open")), "high": _f(r.get("high")),
                         "low": _f(r.get("low")), "close": _f(r.get("close")),
                         "volume": int(r.get("volume") or 0)})
    upsert(con, "prices", pd.DataFrame(prow), ["symbol", "date"], PRICE_COLS)

    # snapshot metrics
    srow = []
    for s, d in prices.items():
        if s in C.YIELDS or s == C.VIX:
            continue
        m = compute_price_metrics(d)
        if not m:
            continue
        m.update({"snapshot_date": today, "symbol": s, "kind": kind_of(s)})
        srow.append(m)
    upsert(con, "snapshot", pd.DataFrame(srow), ["snapshot_date", "symbol"], SNAP_COLS)

    # yields
    yrow = []
    for sym, label in C.YIELDS.items():
        d = prices.get(sym)
        if d is None or d.empty:
            continue
        cur = normalize_yield(_f(d["close"].iloc[-1]))
        prev = normalize_yield(_f(d["close"].iloc[-2])) if len(d) >= 2 else None
        yrow.append({"snapshot_date": today, "tenor": sym, "label": label, "yld": cur,
                     "chg_bps": round((cur - prev) * 100, 1) if (cur and prev) else None})
    upsert(con, "yields", pd.DataFrame(yrow), ["snapshot_date", "tenor"], YIELD_COLS)

    # options + IV rank (watchlist only)
    orow, ivrow = [], []
    for s in C.WATCHLIST:
        rows, atm = fetch_options(s)
        for r in rows:
            r.update({"snapshot_date": today, "symbol": s})
            orow.append(r)
        if atm is not None:
            ivrow.append({"symbol": s, "date": today, "atm_iv": atm})
    upsert(con, "options_oi", pd.DataFrame(orow),
           ["snapshot_date", "symbol", "expiry", "strike", "type"], OPT_COLS)
    upsert(con, "iv_atm", pd.DataFrame(ivrow), ["symbol", "date"], IVATM_COLS)

    # earnings (best-effort, guarded)
    erow = []
    for s in C.WATCHLIST:
        ed = fetch_earnings(s)
        if ed:
            erow.append({"symbol": s, "next_earnings": ed})
    upsert(con, "earnings", pd.DataFrame(erow), ["symbol"], EARN_COLS)

    # sentiment: CNN Fear & Greed (+ components)
    frow = [{**f, "snapshot_date": today} for f in fetch_fear_greed()]
    upsert(con, "sentiment", pd.DataFrame(frow), ["snapshot_date", "metric"], SENT_COLS)

    # news
    nrow = [{**n, "snapshot_date": today} for n in fetch_news()]
    nrow = list({n["link"]: n for n in nrow if n["link"]}.values())  # dedupe by link
    upsert(con, "news", pd.DataFrame(nrow), ["snapshot_date", "link"], NEWS_COLS)

    con.execute("INSERT OR REPLACE INTO meta VALUES ('last_ingest', ?)",
                [dt.datetime.now().isoformat(timespec="seconds")])
    con.close()
    print(f"  done: {len(srow)} snapshots, {len(orow)} option rows, "
          f"{len(ivrow)} IV pts, {len(erow)} earnings, {len(frow)} sentiment, "
          f"{len(nrow)} headlines")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="override snapshot date YYYY-MM-DD (testing)")
    a = ap.parse_args()
    run(dt.date.fromisoformat(a.date) if a.date else None)
