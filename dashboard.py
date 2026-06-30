"""
dashboard.py
------------
Pre-trade morning dashboard. Walks your filter stack top-to-bottom and answers
"do I trade today, and what?" before the open.

  run ingest.py first (or use the button), then:  streamlit run dashboard.py

Panels, in decision order:
  banner   GO / CAUTION / STAND DOWN  (event gate + regime)
  1 snapshot  events + regime + rates merged: NFP/FOMC/OPEX/VIX/F&G/yields/curve at a glance
  2 sector rotation: SPDR sectors ranked by relative strength
  3 etfs   pullback monitor for SOXL / SMH / GLD / ... (holding 20dma vs breaking down)
  4 names  watchlist setups: pullback flag, distance to MAs, setup score, earnings flag
  5 flow   OTM-call OI, OVERNIGHT OI delta (opening tell), vol/OI, IV rank, nearest expiry
  6 news   scored geo/macro headlines
  7 seasonality  monthly + day-of-week avg returns (SPY)
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys

import altair as alt
import duckdb
import pandas as pd
import streamlit as st

import config as C
import market_calendar as mc

st.set_page_config(page_title="Pre-Trade Dashboard", layout="wide",
                   initial_sidebar_state="collapsed")

GREEN, RED, AMBER, MUTED = "#16a34a", "#dc2626", "#d97706", "#6b7280"


# --------------------------------------------------------------------------- #
# data access
# --------------------------------------------------------------------------- #
def get_con() -> duckdb.DuckDBPyConnection | None:
    if not os.path.exists(C.DB_PATH):
        return None
    return duckdb.connect(C.DB_PATH, read_only=True)


def q(con, sql: str, params: list | None = None) -> pd.DataFrame:
    try:
        return con.execute(sql, params or []).fetchdf()
    except Exception:
        return pd.DataFrame()


def last_ingest(con) -> str | None:
    df = q(con, "SELECT value FROM meta WHERE key='last_ingest'")
    return None if df.empty else df.iloc[0, 0]


def latest_snapshot_date(con) -> dt.date | None:
    df = q(con, "SELECT max(snapshot_date) d FROM snapshot")
    return None if df.empty or pd.isna(df.iloc[0, 0]) else pd.to_datetime(df.iloc[0, 0]).date()


def run_ingest_button(label: str = "Run ingest now") -> None:
    if st.button(label, type="primary"):
        with st.spinner("Pulling market data (yfinance + RSS)…"):
            r = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "ingest.py")],
                               capture_output=True, text=True)
        st.code((r.stdout or "") + (r.stderr or ""), language="text")
        if r.returncode == 0:
            st.success("Ingest complete.")
            st.rerun()
        else:
            st.error("Ingest failed — see log above.")


# --------------------------------------------------------------------------- #
# event gate + banner
# --------------------------------------------------------------------------- #
def gather_events(today: dt.date) -> dict:
    inw, ltq, td_left = mc.quarter_end_window(today)
    extra = []
    for nm, ds, note in C.EXTRA_MACRO_EVENTS:
        d = dt.date.fromisoformat(ds)
        extra.append(mc.MacroEvent(nm, d, note, (d - today).days, mc.trading_days_between(today, d)))
    return {
        "opex": mc.next_monthly_opex(today),
        "witch": mc.next_quarterly_witching(today),
        "fomc": mc.next_fomc(today),
        "nfp": mc.next_nfp(today),
        "qe_in": inw, "qe_last": ltq, "qe_left": td_left,
        "extra": extra,
        "is_trading_day": mc.is_trading_day(today),
    }


_RANK = {"GO": 0, "CAUTION": 1, "STAND DOWN": 2}


def _esc(level: str, to: str) -> str:
    return to if _RANK[to] > _RANK[level] else level


def compute_banner(today: dt.date, ev: dict, regime: dict) -> tuple[str, list[str]]:
    level, reasons = "GO", []
    if not ev["is_trading_day"]:
        return "STAND DOWN", ["Market closed today (holiday/weekend)"]

    if ev["nfp"].calendar_days == 0:
        level = _esc(level, "STAND DOWN"); reasons.append("Jobs report (NFP) drops 8:30 ET")
    if ev["fomc"] and ev["fomc"].calendar_days == 0:
        level = _esc(level, "STAND DOWN"); reasons.append("FOMC rate decision today, 2:00 ET")
    for e in ev["extra"]:
        if e.calendar_days == 0:
            level = _esc(level, "STAND DOWN"); reasons.append(f"{e.name} today")
    if ev["opex"].calendar_days == 0 and ev["opex"].is_quarterly:
        level = _esc(level, "CAUTION"); reasons.append("Triple/quad witching today")
    if ev["qe_in"]:
        level = _esc(level, "CAUTION")
        reasons.append(f"Quarter-end rebalance window (last session {ev['qe_last']:%a %b %d})")
    if ev["fomc"] and 0 < ev["fomc"].trading_days <= 1:
        level = _esc(level, "CAUTION"); reasons.append("FOMC tomorrow")
    if ev["nfp"].trading_days == 1:
        level = _esc(level, "CAUTION"); reasons.append("Jobs report next session")

    # regime — trend
    vix = regime.get("vix")
    qqq_a20, spy_a20 = regime.get("qqq_above20"), regime.get("spy_above20")
    qqq_a50 = regime.get("qqq_above50")
    if qqq_a20 is False and qqq_a50 is False:
        level = _esc(level, "STAND DOWN"); reasons.append("QQQ below 20 & 50 DMA — distribution")
    elif qqq_a20 is False:
        level = _esc(level, "CAUTION"); reasons.append("QQQ below 20 DMA")

    # regime — volatility
    if vix is not None:
        if vix >= C.VIX_PANIC:
            level = _esc(level, "CAUTION")
            reasons.append(f"VIX {vix:.1f} — high fear, premium pricey & whippy")
        elif vix >= C.VIX_ELEVATED:
            level = _esc(level, "CAUTION"); reasons.append(f"VIX elevated ({vix:.1f})")
    vpct = regime.get("vix_pct")
    if vpct is not None and vpct >= 12:
        level = _esc(level, "CAUTION"); reasons.append(f"VIX spiking +{vpct:.0f}% — risk-off")

    # regime — sentiment (Fear & Greed extremes are a "don't chase / be careful" flag)
    fr = (regime.get("fng_rating") or "").lower()
    fs = regime.get("fng_score")
    if "extreme greed" in fr:
        level = _esc(level, "CAUTION")
        reasons.append(f"Fear & Greed {fs:.0f} — Extreme Greed, frothy: don't chase extended names")
    elif "extreme fear" in fr:
        level = _esc(level, "CAUTION")
        reasons.append(f"Fear & Greed {fs:.0f} — Extreme Fear, elevated risk (contrarian watch)")

    if level == "GO" and not reasons:
        reasons.append("Clean tape, no major print pre-open — setups in play")
    return level, reasons


def render_banner(level: str, reasons: list[str]) -> None:
    color = {"GO": GREEN, "CAUTION": AMBER, "STAND DOWN": RED}[level]
    bullets = "".join(f"<li>{r}</li>" for r in reasons)
    st.markdown(
        f"""<div style="background:{color};color:white;padding:14px 18px;border-radius:10px;">
        <span style="font-size:1.5rem;font-weight:800;letter-spacing:.5px;">{level}</span>
        <ul style="margin:.4rem 0 0 1.1rem;padding:0;font-size:.92rem;">{bullets}</ul>
        </div>""", unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# styling helpers
# --------------------------------------------------------------------------- #
def color_signed(v):
    if pd.isna(v):
        return ""
    return f"color:{GREEN}" if v > 0 else (f"color:{RED}" if v < 0 else "")


def bool_bg(v):
    if v is True:
        return "background-color:rgba(22,163,74,.18)"
    if v is False:
        return "background-color:rgba(220,38,38,.10)"
    return ""


def show_table(df: pd.DataFrame, signed_cols=(), bool_cols=(), height=None):
    if df.empty:
        st.caption("— no data —")
        return
    sty = df.style.format(precision=2, na_rep="—")
    for c in signed_cols:
        if c in df.columns:
            sty = sty.map(color_signed, subset=[c])
    for c in bool_cols:
        if c in df.columns:
            sty = sty.map(bool_bg, subset=[c])
    kwargs = {"width": "stretch", "hide_index": True}
    if height is not None:
        kwargs["height"] = height
    st.dataframe(sty, **kwargs)


# --------------------------------------------------------------------------- #
# panels
# --------------------------------------------------------------------------- #
def vix_zone(v: float | None) -> str:
    if v is None:
        return "—"
    if v < 13:
        return "very calm"
    if v < C.VIX_CALM:
        return "calm"
    if v < C.VIX_ELEVATED:
        return "rising"
    if v < C.VIX_PANIC:
        return "elevated"
    return "high fear"


def _sentiment_vals(con, today: dt.date) -> dict:
    """VIX (level/Δ/%) + CNN Fear & Greed headline and components."""
    out: dict = {}
    vix = q(con, "SELECT close FROM prices WHERE symbol=? ORDER BY date DESC LIMIT 2", [C.VIX])
    if not vix.empty:
        out["vix"] = float(vix.iloc[0, 0])
        if len(vix) >= 2:
            p = float(vix.iloc[1, 0])
            out["vix_chg"] = round(out["vix"] - p, 2)
            out["vix_pct"] = round((out["vix"] - p) / p * 100, 1) if p else None
    s = q(con, "SELECT metric,score,rating,prev_close,prev_week,prev_month,prev_year "
               "FROM sentiment WHERE snapshot_date=?", [today])
    if not s.empty:
        m = {r.metric: r for r in s.itertuples()}
        if "fng" in m:
            f = m["fng"]
            out.update(fng_score=f.score, fng_rating=f.rating, fng_prev=f.prev_close,
                       fng_week=f.prev_week, fng_month=f.prev_month, fng_year=f.prev_year)
        for comp in ("momentum", "strength", "breadth", "put_call",
                     "volatility", "safe_haven", "junk_bond"):
            k = f"fng_{comp}"
            if k in m:
                out[f"fng_{comp}"] = m[k].score
                out[f"fng_{comp}_rating"] = m[k].rating
    return out


def _regime_vals(con, today: dt.date) -> dict:
    """Sentiment + index-vs-MA flags, merged — what the banner consumes."""
    out = _sentiment_vals(con, today)
    snap = q(con, "SELECT symbol,above20,above50 FROM snapshot "
                  "WHERE snapshot_date=? AND symbol IN ('SPY','QQQ')", [today])
    d = {r.symbol: r for r in snap.itertuples()}
    if "QQQ" in d:
        out["qqq_above20"], out["qqq_above50"] = d["QQQ"].above20, d["QQQ"].above50
    if "SPY" in d:
        out["spy_above20"] = d["SPY"].above20
    return out


def panel_overview(con, today: dt.date, ev: dict, regime: dict) -> None:
    """Panels ①②③ merged: events + regime + rates at a glance."""
    st.subheader("① Market snapshot — events · regime · rates")
    o, w = ev["opex"], ev["witch"]
    v = regime.get("vix")
    fs, frt = regime.get("fng_score"), regime.get("fng_rating")

    # --- top metric strip: events + VIX + F&G + spread ---
    ydf = q(con, "SELECT label,yld,chg_bps FROM yields WHERE snapshot_date=? "
                 "ORDER BY CASE label WHEN '13-wk' THEN 1 WHEN '5-yr' THEN 2 "
                 "WHEN '10-yr' THEN 3 ELSE 4 END", [today])
    y_map = {r.label: r.yld for r in ydf.itertuples() if pd.notna(r.yld)}

    c = st.columns(6)
    c[0].metric("NFP", f"{ev['nfp'].date:%b %d}", f"in {ev['nfp'].calendar_days}d")
    if ev["fomc"]:
        c[1].metric("FOMC", f"{ev['fomc'].date:%b %d}", f"in {ev['fomc'].calendar_days}d")
    opex_d = f"{o.trading_days} sess" + (" · WITCH" if o.is_quarterly else "")
    c[2].metric("OPEX", f"{o.date:%b %d}", opex_d)
    if v is not None:
        chg, pct = regime.get("vix_chg"), regime.get("vix_pct")
        delta = f"{chg:+.2f} ({pct:+.1f}%)" if (chg is not None and pct is not None) else None
        c[3].metric(f"VIX · {vix_zone(v)}", f"{v:.2f}", delta, delta_color="inverse")
    if fs is not None:
        prev = regime.get("fng_prev")
        c[4].metric(f"F&G · {frt}", f"{fs:.0f}",
                    f"{fs - prev:+.0f} vs yest" if prev is not None else None)
    if "10-yr" in y_map and "13-wk" in y_map:
        spread = (y_map["10-yr"] - y_map["13-wk"]) * 100
        inv_tag = " INV" if spread < 0 else ""
        c[5].metric("2s10s", f"{spread:+.0f} bps{inv_tag}",
                    f"10y {y_map['10-yr']:.2f}%")

    # --- futures / macro strip: overnight gap + DXY + crude + BTC ---
    fut = q(con, "SELECT symbol,close,pct_1d FROM snapshot "
                 "WHERE snapshot_date=? AND kind='futures' ORDER BY symbol", [today])
    if not fut.empty:
        fc = st.columns(len(fut))
        for col, r in zip(fc, fut.itertuples()):
            label = C.FUTURES.get(r.symbol, r.symbol)
            pct = r.pct_1d
            delta = f"{pct:+.2f}%" if pd.notna(pct) else None
            price = f"{r.close:,.2f}" if pd.notna(r.close) else "—"
            col.metric(label, price, delta)

    # --- two-column body: index table | yields + curve ---
    left, right = st.columns([3, 2])
    with left:
        df = q(con, "SELECT symbol,close,pct_1d,sma20,sma50,sma200,above20,above50,above200 "
                    "FROM snapshot WHERE snapshot_date=? AND symbol IN ('SPY','QQQ','IWM','DIA') "
                    "ORDER BY symbol", [today])
        show_table(df.rename(columns={"pct_1d": "%1d"}),
                   signed_cols=["%1d"], bool_cols=["above20", "above50", "above200"])

    with right:
        if not ydf.empty:
            show_table(ydf.rename(columns={"yld": "yield%", "chg_bps": "Δbps"}),
                       signed_cols=["Δbps"])
            curve = ydf[ydf["yld"].notna()].copy()
            if len(curve) >= 2:
                tenor_order = ["13-wk", "5-yr", "10-yr", "30-yr"]
                base = alt.Chart(curve).encode(
                    x=alt.X("label:N", sort=tenor_order, title=""),
                    y=alt.Y("yld:Q", title="Yield %", scale=alt.Scale(zero=False)),
                    tooltip=[alt.Tooltip("label:N", title="Tenor"),
                             alt.Tooltip("yld:Q", format=".2f"),
                             alt.Tooltip("chg_bps:Q", title="Δbps", format="+.1f")],
                )
                chart = (base.mark_line(strokeWidth=2.5, color="#2563eb")
                         + base.mark_point(size=60, filled=True, color="#2563eb")
                         ).properties(height=160)
                st.altair_chart(chart, use_container_width=True)

    # --- verdict ---
    froth = "extreme greed" in (frt or "").lower()
    fear = "extreme fear" in (frt or "").lower()
    if regime.get("qqq_above20") and regime.get("spy_above20") and (v or 99) < C.VIX_ELEVATED and not froth:
        st.success("Risk-on: QQQ & SPY above 20DMA, VIX subdued. Long-call bar is normal.")
    elif regime.get("qqq_above20") is False and regime.get("qqq_above50") is False:
        st.error("Distribution: QQQ below 20 & 50 DMA — theta donation territory.")
    elif froth:
        st.warning("Extreme Greed — chase risk high. Demand pullback + acceptance.")
    elif fear:
        st.warning("Extreme Fear — contrarian watch, sized small.")
    else:
        st.warning("Mixed tape. Be selective; let RS + acceptance do the filtering.")

    # --- compact event notes ---
    notes = []
    if ev["qe_in"]:
        notes.append(f"Quarter-end rebalance window ({ev['qe_left']} sessions left)")
    if o.holiday_adjusted:
        notes.append("OPEX shifted off closed 3rd-Friday")
    for e in ev["extra"]:
        notes.append(f"{e.name}: {e.date:%b %d} — {e.note}")
    if not ev["extra"]:
        notes.append("CPI/PPI/PCE: add dates to config.EXTRA_MACRO_EVENTS")
    if notes:
        st.caption(" · ".join(notes))


def panel_sectors(con, today: dt.date) -> None:
    st.subheader("④ Sector rotation")
    df = q(con, "SELECT symbol,pct_1d,pct_5d,pct_20d,above50 FROM snapshot "
                "WHERE snapshot_date=? AND kind='sector'", [today])
    if df.empty:
        st.caption("— no data —"); return
    spy = q(con, "SELECT pct_20d FROM snapshot WHERE snapshot_date=? AND symbol='SPY'", [today])
    spy20 = float(spy.iloc[0, 0]) if not spy.empty and pd.notna(spy.iloc[0, 0]) else 0.0
    df["name"] = df["symbol"].map(C.SECTORS)
    df["RS_20d_vs_SPY"] = (df["pct_20d"] - spy20).round(2)
    df = df.sort_values("RS_20d_vs_SPY", ascending=False)
    df = df[["name", "symbol", "pct_1d", "pct_5d", "pct_20d", "RS_20d_vs_SPY", "above50"]]
    show_table(df.rename(columns={"pct_1d": "%1d", "pct_5d": "%5d", "pct_20d": "%20d"}),
               signed_cols=["%1d", "%5d", "%20d", "RS_20d_vs_SPY"], bool_cols=["above50"])
    lead = df.iloc[0]["name"]; lag = df.iloc[-1]["name"]
    st.caption(f"Leading: **{lead}** · Lagging: **{lag}** — money rotating toward the top of this list.")


def panel_etfs(con, today: dt.date) -> None:
    st.subheader("⑤ ETF pullback monitor")
    df = q(con, "SELECT symbol,close,pct_1d,off_high20_pct,dist20_pct,dist50_pct,"
                "above20,above50,pullback_flag FROM snapshot WHERE snapshot_date=? AND kind='etf' "
                "ORDER BY pullback_flag DESC, off_high20_pct ASC", [today])
    if df.empty:
        st.caption("— no data (check tickers in config.PULLBACK_ETFS) —"); return
    show_table(df.rename(columns={"pct_1d": "%1d", "off_high20_pct": "off_20dHi%",
                                  "dist20_pct": "vs20dma%", "dist50_pct": "vs50dma%"}),
               signed_cols=["%1d", "vs20dma%", "vs50dma%"],
               bool_cols=["above20", "above50", "pullback_flag"])
    st.caption("**pullback_flag** = pulled off the 20d high but holding a rising 20DMA in an "
               "uptrend (buyable dip). Below 20DMA with a deep off-high% = breaking down, stand aside.")


def panel_watchlist(con, today: dt.date, ev: dict) -> None:
    st.subheader("⑥ Watchlist setups")
    df = q(con, """
        SELECT s.symbol, s.pct_1d, s.off_high20_pct, s.dist20_pct, s.dist50_pct,
               s.pullback_flag, s.setup_score, iv.atm_iv, e.next_earnings
        FROM snapshot s
        LEFT JOIN iv_atm iv ON iv.symbol=s.symbol AND iv.date=s.snapshot_date
        LEFT JOIN earnings e ON e.symbol=s.symbol
        WHERE s.snapshot_date=? AND s.kind='watch'
        ORDER BY s.setup_score DESC, s.off_high20_pct ASC""", [today])
    if df.empty:
        st.caption("— no data —"); return
    # IV rank from history
    ivr = q(con, "SELECT symbol, atm_iv, date FROM iv_atm ORDER BY symbol, date")
    rank_map = {}
    if not ivr.empty:
        for sym, g in ivr.groupby("symbol"):
            from ingest import iv_rank
            rank_map[sym] = iv_rank(g["atm_iv"])
    df["iv_rank"] = df["symbol"].map(rank_map)
    # HV (20-day realized vol) vs IV
    wl_syms = list(df["symbol"])
    placeholders = ",".join(["?"] * len(wl_syms))
    wl_prices = q(con, f"SELECT symbol, date, close FROM prices "
                       f"WHERE symbol IN ({placeholders}) ORDER BY symbol, date",
                  wl_syms)
    hv_map = {}
    if not wl_prices.empty:
        from ingest import realized_vol
        for sym, g in wl_prices.groupby("symbol"):
            hv_map[sym] = realized_vol(g["close"])
    df["HV20"] = df["symbol"].map(hv_map)
    df["HV/IV"] = (df["HV20"] / df["atm_iv"]).round(2).where(
        df["atm_iv"].notna() & df["HV20"].notna())
    opex_date = ev["opex"].date
    df["earn_flag"] = df["next_earnings"].apply(
        lambda d: "⚠ before OPEX" if (pd.notna(d) and pd.to_datetime(d).date() <= opex_date) else "")
    df = df[["symbol", "setup_score", "pullback_flag", "pct_1d", "off_high20_pct",
             "dist20_pct", "dist50_pct", "atm_iv", "HV20", "HV/IV", "iv_rank",
             "next_earnings", "earn_flag"]]
    show_table(df.rename(columns={"pct_1d": "%1d", "off_high20_pct": "off_20dHi%",
                                   "dist20_pct": "vs20dma%", "dist50_pct": "vs50dma%",
                                   "atm_iv": "ATM_IV%"}),
               signed_cols=["%1d", "vs20dma%", "vs50dma%"], bool_cols=["pullback_flag"])
    st.caption(f"setup_score = trend(above 20/50/200) + pullback flag + turning up (0–5). "
               f"IV rank ≥ {C.IV_RANK_HOT:.0f} = rich → favor call **spreads** over naked calls. "
               "**HV/IV < 0.8** = IV expensive vs realized (spreads/sells); "
               "**HV/IV > 1.2** = IV cheap (naked buys OK). "
               "⚠ = earnings before the next OPEX (crush risk).")


def panel_flow(con, today: dt.date) -> None:
    st.subheader("⑦ Options flow — OTM calls, OI delta, vol/OI")
    prev = q(con, "SELECT max(snapshot_date) d FROM options_oi WHERE snapshot_date<?", [today])
    prev_date = None if prev.empty or pd.isna(prev.iloc[0, 0]) else pd.to_datetime(prev.iloc[0, 0]).date()
    cur = q(con, "SELECT symbol,expiry,strike,oi,volume,iv,otm_pct,vol_oi,spot "
                 "FROM options_oi WHERE snapshot_date=?", [today])
    if cur.empty:
        st.caption("— no options snapshot yet —"); return
    if prev_date:
        pr = q(con, "SELECT symbol,expiry,strike,oi AS prev_oi FROM options_oi WHERE snapshot_date=?",
               [prev_date])
        cur = cur.merge(pr, on=["symbol", "expiry", "strike"], how="left")
        cur["oi_delta"] = (cur["oi"] - cur["prev_oi"]).astype("Int64")
    else:
        cur["oi_delta"] = pd.NA
        st.caption("Overnight OI delta needs ≥ 2 daily snapshots — available tomorrow.")

    flagged = cur[(cur["vol_oi"] >= C.VOL_OI_MIN) |
                  (cur["oi_delta"].fillna(0) >= C.OI_DELTA_MIN)].copy()
    view = flagged if not flagged.empty else cur.sort_values("vol_oi", ascending=False).head(20)
    view = view.sort_values(["oi_delta", "vol_oi"], ascending=False)
    view = view[["symbol", "expiry", "strike", "otm_pct", "oi", "oi_delta",
                 "volume", "vol_oi", "iv", "spot"]]
    show_table(view.rename(columns={"otm_pct": "OTM%", "oi_delta": "ΔOI(o/n)",
                                     "vol_oi": "vol/OI", "iv": "IV%"}),
               signed_cols=["OTM%"], height=360)
    st.caption(f"Showing contracts with vol/OI ≥ {C.VOL_OI_MIN} or overnight ΔOI ≥ {C.OI_DELTA_MIN}. "
               "**Rising ΔOI = opening interest** (the tell you want); flat/negative = closing/hedging. "
               "Side (bought vs sold) isn't free — confirm with context + Market Chameleon.")


def panel_news(con, today: dt.date) -> None:
    st.subheader("⑧ Geo / macro headlines")
    df = q(con, "SELECT score,title,source,link FROM news WHERE snapshot_date=? "
                "ORDER BY score DESC, ts DESC LIMIT 25", [today])
    if df.empty:
        st.caption("— no headlines (RSS runs in ingest) —"); return
    hi = df[df["score"] >= 2]
    if not hi.empty:
        st.markdown("**Elevated:**")
        for r in hi.itertuples():
            st.markdown(f"- `{r.score}` [{r.title}]({r.link})")
    with st.expander(f"All headlines ({len(df)})"):
        for r in df.itertuples():
            st.markdown(f"- `{r.score}` [{r.title}]({r.link})")
    st.caption("Score = sum of geo/macro keyword weights (config.GEO_KEYWORDS). Route through your "
               "Ollama scorer for real stock-moving classification.")


def panel_seasonality(con, today: dt.date) -> None:
    st.subheader("⑨ Seasonality — monthly & day-of-week")
    df = q(con, "SELECT date, close FROM prices WHERE symbol='SPY' ORDER BY date")
    if len(df) < 60:
        st.caption("— need more history (run ingest daily to accumulate) —")
        return
    df["date"] = pd.to_datetime(df["date"])
    df["ret"] = df["close"].pct_change() * 100
    df = df.dropna(subset=["ret"])

    MONTH_NAMES = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
                   7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}
    DAY_NAMES = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri"}
    MONTH_ORDER = list(MONTH_NAMES.values())
    DAY_ORDER = list(DAY_NAMES.values())

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("**Monthly avg daily return (SPY)**")
        df["month"] = df["date"].dt.month
        monthly = df.groupby("month")["ret"].agg(["mean", "count"]).reset_index()
        monthly.columns = ["month", "ret", "n"]
        monthly["month_name"] = monthly["month"].map(MONTH_NAMES)
        monthly["_color"] = monthly.apply(
            lambda r: "#2563eb" if r["month"] == today.month
            else (GREEN if r["ret"] > 0 else RED), axis=1)
        chart = alt.Chart(monthly).mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(
            x=alt.X("month_name:N", sort=MONTH_ORDER, title=""),
            y=alt.Y("ret:Q", title="Avg daily return (%)"),
            color=alt.Color("_color:N", scale=None),
            tooltip=[alt.Tooltip("month_name:N", title="Month"),
                     alt.Tooltip("ret:Q", title="Avg %", format=".3f"),
                     alt.Tooltip("n:Q", title="# days")],
        ).properties(height=220)
        st.altair_chart(chart, use_container_width=True)

    with col2:
        st.markdown("**Day-of-week avg return (SPY)**")
        df["dow"] = df["date"].dt.dayofweek
        dow = df.groupby("dow")["ret"].agg(["mean", "count"]).reset_index()
        dow.columns = ["dow", "ret", "n"]
        dow["day_name"] = dow["dow"].map(DAY_NAMES)
        dow["_color"] = dow.apply(
            lambda r: "#2563eb" if r["dow"] == today.weekday()
            else (GREEN if r["ret"] > 0 else RED), axis=1)
        chart = alt.Chart(dow).mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(
            x=alt.X("day_name:N", sort=DAY_ORDER, title=""),
            y=alt.Y("ret:Q", title="Avg daily return (%)"),
            color=alt.Color("_color:N", scale=None),
            tooltip=[alt.Tooltip("day_name:N", title="Day"),
                     alt.Tooltip("ret:Q", title="Avg %", format=".3f"),
                     alt.Tooltip("n:Q", title="# days")],
        ).properties(height=220)
        st.altair_chart(chart, use_container_width=True)

    n_days = len(df)
    span = f"{df['date'].iloc[0]:%b %Y} – {df['date'].iloc[-1]:%b %Y}"
    st.caption(f"Based on {n_days} trading days ({span}). "
               "Blue = current month/day. Accumulates history with each ingest run.")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> None:
    today = dt.date.today()
    st.title("Pre-Trade Dashboard")
    st.caption(f"{today:%A, %B %d, %Y}")

    con = get_con()
    if con is None:
        st.warning("No database yet. Run the ingestion job to populate today's data.")
        run_ingest_button("Run first ingest")
        return

    li = last_ingest(con)
    snap_date = latest_snapshot_date(con)
    top = st.columns([3, 1])
    with top[0]:
        if li:
            st.caption(f"Last ingest: **{li}**" + (f" · data date **{snap_date}**" if snap_date else ""))
        if snap_date and snap_date != today:
            st.info(f"Showing latest available data ({snap_date}); today's ingest hasn't run.")
    with top[1]:
        run_ingest_button()

    use_date = snap_date or today
    ev = gather_events(today)
    regime = _regime_vals(con, use_date)
    level, reasons = compute_banner(today, ev, regime)
    render_banner(level, reasons)
    st.divider()

    panel_overview(con, use_date, ev, regime); st.divider()
    panel_sectors(con, use_date); st.divider()
    panel_etfs(con, use_date); st.divider()
    panel_watchlist(con, use_date, ev); st.divider()
    panel_flow(con, use_date); st.divider()
    panel_news(con, use_date); st.divider()
    panel_seasonality(con, use_date)
    con.close()


if __name__ == "__main__":
    main()
