"""Pre-trade dashboard — Streamlit UI, reads DuckDB (read-only)."""
from __future__ import annotations
import datetime as dt, os, subprocess, sys
import altair as alt, duckdb, pandas as pd, streamlit as st
import config as C, market_calendar as mc

st.set_page_config(page_title="Pre-Trade Dashboard", layout="wide",
                   initial_sidebar_state="collapsed")
GREEN, RED, AMBER = "#16a34a", "#dc2626", "#d97706"

# --- data access ---
def get_con():
    return duckdb.connect(C.DB_PATH, read_only=True) if os.path.exists(C.DB_PATH) else None

def q(con, sql, params=None):
    try: return con.execute(sql, params or []).fetchdf()
    except Exception: return pd.DataFrame()

def last_ingest(con):
    df = q(con, "SELECT value FROM meta WHERE key='last_ingest'")
    return None if df.empty else df.iloc[0, 0]

def latest_snapshot_date(con):
    df = q(con, "SELECT max(snapshot_date) d FROM snapshot")
    return None if df.empty or pd.isna(df.iloc[0, 0]) else pd.to_datetime(df.iloc[0, 0]).date()

def run_ingest_button(label="Run ingest now", con=None):
    if st.button(label, type="primary"):
        if con is not None: con.close()
        with st.spinner("Pulling market data…"):
            r = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "ingest.py")],
                               capture_output=True, text=True)
        st.code((r.stdout or "") + (r.stderr or ""), language="text")
        st.success("Ingest complete.") if r.returncode == 0 else st.error("Ingest failed.")
        if r.returncode == 0: st.rerun()

# --- banner logic ---
def gather_events(today):
    inw, ltq, td_left = mc.quarter_end_window(today)
    extra = [mc.MacroEvent(nm, d := dt.date.fromisoformat(ds), note,
             (d - today).days, mc.trading_days_between(today, d))
             for nm, ds, note in C.EXTRA_MACRO_EVENTS]
    return {"opex": mc.next_monthly_opex(today), "witch": mc.next_quarterly_witching(today),
            "fomc": mc.next_fomc(today), "nfp": mc.next_nfp(today),
            "qe_in": inw, "qe_last": ltq, "qe_left": td_left,
            "extra": extra, "is_trading_day": mc.is_trading_day(today)}

_RANK = {"GO": 0, "CAUTION": 1, "STAND DOWN": 2}
def _esc(level, to): return to if _RANK[to] > _RANK[level] else level

def compute_banner(today, ev, regime):
    level, reasons = "GO", []
    if not ev["is_trading_day"]:
        return "STAND DOWN", ["Market closed today (holiday/weekend)"]
    if ev["nfp"].calendar_days == 0:
        level = _esc(level, "STAND DOWN"); reasons.append("NFP drops 8:30 ET")
    if ev["fomc"] and ev["fomc"].calendar_days == 0:
        level = _esc(level, "STAND DOWN"); reasons.append("FOMC rate decision today")
    for e in ev["extra"]:
        if e.calendar_days == 0:
            level = _esc(level, "STAND DOWN"); reasons.append(f"{e.name} today")
    if ev["opex"].calendar_days == 0 and ev["opex"].is_quarterly:
        level = _esc(level, "CAUTION"); reasons.append("Triple/quad witching today")
    if ev["qe_in"]:
        level = _esc(level, "CAUTION"); reasons.append("Quarter-end rebalance window")
    if ev["fomc"] and 0 < ev["fomc"].trading_days <= 1:
        level = _esc(level, "CAUTION"); reasons.append("FOMC tomorrow")
    if ev["nfp"].trading_days == 1:
        level = _esc(level, "CAUTION"); reasons.append("Jobs report next session")
    vix = regime.get("vix")
    if regime.get("qqq_above20") is False and regime.get("qqq_above50") is False:
        level = _esc(level, "STAND DOWN"); reasons.append("QQQ below 20 & 50 DMA — distribution")
    elif regime.get("qqq_above20") is False:
        level = _esc(level, "CAUTION"); reasons.append("QQQ below 20 DMA")
    if vix is not None:
        if vix >= C.VIX_PANIC:
            level = _esc(level, "CAUTION"); reasons.append(f"VIX {vix:.1f} — high fear")
        elif vix >= C.VIX_ELEVATED:
            level = _esc(level, "CAUTION"); reasons.append(f"VIX elevated ({vix:.1f})")
    vpct = regime.get("vix_pct")
    if vpct is not None and vpct >= 12:
        level = _esc(level, "CAUTION"); reasons.append(f"VIX spiking +{vpct:.0f}%")
    fr = (regime.get("fng_rating") or "").lower()
    fs = regime.get("fng_score")
    if "extreme greed" in fr:
        level = _esc(level, "CAUTION"); reasons.append(f"F&G {fs:.0f} — Extreme Greed")
    elif "extreme fear" in fr:
        level = _esc(level, "CAUTION"); reasons.append(f"F&G {fs:.0f} — Extreme Fear")
    if level == "GO" and not reasons:
        reasons.append("Clean tape — setups in play")
    return level, reasons

def render_banner(level, reasons):
    color = {"GO": GREEN, "CAUTION": AMBER, "STAND DOWN": RED}[level]
    bullets = "".join(f"<li>{r}</li>" for r in reasons)
    st.markdown(f'<div style="background:{color};color:white;padding:14px 18px;border-radius:10px;">'
                f'<span style="font-size:1.5rem;font-weight:800;">{level}</span>'
                f'<ul style="margin:.4rem 0 0 1.1rem;padding:0;font-size:.92rem;">{bullets}</ul>'
                f'</div>', unsafe_allow_html=True)

# --- styling ---
def _csign(v):
    return "" if pd.isna(v) else (f"color:{GREEN}" if v > 0 else (f"color:{RED}" if v < 0 else ""))
def _cbool(v):
    if v is True: return "background-color:rgba(22,163,74,.18)"
    if v is False: return "background-color:rgba(220,38,38,.10)"
    return ""

def show_table(df, signed_cols=(), bool_cols=(), height=None):
    if df.empty: st.caption("— no data —"); return
    sty = df.style.format(precision=2, na_rep="—")
    for c in signed_cols:
        if c in df.columns: sty = sty.map(_csign, subset=[c])
    for c in bool_cols:
        if c in df.columns: sty = sty.map(_cbool, subset=[c])
    kw = {"width": "stretch", "hide_index": True}
    if height: kw["height"] = height
    st.dataframe(sty, **kw)

# --- regime helpers ---
def vix_zone(v):
    if v is None: return "—"
    for thresh, label in [(13, "very calm"), (C.VIX_CALM, "calm"),
                          (C.VIX_ELEVATED, "rising"), (C.VIX_PANIC, "elevated")]:
        if v < thresh: return label
    return "high fear"

def _sentiment_vals(con, today):
    out = {}
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
        for comp in ("momentum","strength","breadth","put_call","volatility","safe_haven","junk_bond"):
            if f"fng_{comp}" in m:
                out[f"fng_{comp}"] = m[f"fng_{comp}"].score
                out[f"fng_{comp}_rating"] = m[f"fng_{comp}"].rating
    return out

def _regime_vals(con, today):
    out = _sentiment_vals(con, today)
    snap = q(con, "SELECT symbol,above20,above50 FROM snapshot "
                  "WHERE snapshot_date=? AND symbol IN ('SPY','QQQ')", [today])
    d = {r.symbol: r for r in snap.itertuples()}
    if "QQQ" in d: out["qqq_above20"], out["qqq_above50"] = d["QQQ"].above20, d["QQQ"].above50
    if "SPY" in d: out["spy_above20"] = d["SPY"].above20
    return out

# --- panels ---
def panel_overview(con, today, ev, regime):
    st.subheader("① Snapshot")
    o, v = ev["opex"], regime.get("vix")
    fs, frt = regime.get("fng_score"), regime.get("fng_rating")
    ydf = q(con, "SELECT label,yld,chg_bps FROM yields WHERE snapshot_date=? "
                 "ORDER BY CASE label WHEN '13-wk' THEN 1 WHEN '5-yr' THEN 2 "
                 "WHEN '10-yr' THEN 3 ELSE 4 END", [today])
    y_map = {r.label: r.yld for r in ydf.itertuples() if pd.notna(r.yld)}
    # key metrics — compact 1-2 line strip
    parts = [f"**NFP** {ev['nfp'].date:%b %d} ({ev['nfp'].calendar_days}d)"]
    if ev["fomc"]: parts.append(f"**FOMC** {ev['fomc'].date:%b %d} ({ev['fomc'].calendar_days}d)")
    parts.append(f"**OPEX** {o.date:%b %d} ({o.trading_days}d{'·WITCH' if o.is_quarterly else ''})")
    if v is not None:
        chg, pct = regime.get("vix_chg"), regime.get("vix_pct")
        vd = f" {chg:+.1f}" if chg is not None else ""
        parts.append(f"**VIX** {v:.1f}{vd} _{vix_zone(v)}_")
    if fs is not None:
        parts.append(f"**F&G** {fs:.0f} _{frt}_")
    if "10-yr" in y_map and "13-wk" in y_map:
        sp = (y_map["10-yr"] - y_map["13-wk"]) * 100
        parts.append(f"**2s10s** {sp:+.0f}bps{' INV' if sp < 0 else ''}")
    if ev["qe_in"]:
        parts.append(f"**Qtr-end** {ev['qe_left']}d left")
    st.markdown(" · ".join(parts))
    # futures — compact table instead of 6 metric cards
    fut = q(con, "SELECT symbol,close,prev_close,pct_1d FROM snapshot "
                 "WHERE snapshot_date=? AND kind='futures' ORDER BY symbol", [today])
    if not fut.empty:
        fut["name"] = fut["symbol"].map(C.FUTURES)
        fut["pts"] = (fut["close"] - fut["prev_close"]).round(2)
        fut = fut[["name","close","pts","pct_1d"]]
        show_table(fut.rename(columns={"pct_1d":"%1d"}), signed_cols=["pts","%1d"])
    # index table
    df = q(con, "SELECT symbol,close,prev_close,pct_1d,sma20,sma50,above20,above50 "
                "FROM snapshot WHERE snapshot_date=? "
                "AND symbol IN ('SPY','QQQ','IWM','DIA') ORDER BY symbol", [today])
    if not df.empty:
        df["pts"] = (df["close"] - df["prev_close"]).round(2)
        df = df[["symbol","close","pts","pct_1d","sma20","sma50","above20","above50"]]
        show_table(df.rename(columns={"pct_1d":"%1d"}),
                   signed_cols=["pts","%1d"], bool_cols=["above20","above50"])
    # yields — inline
    if not ydf.empty:
        show_table(ydf.rename(columns={"yld":"yield%","chg_bps":"Δbps"}), signed_cols=["Δbps"])
    # verdict
    froth = "extreme greed" in (frt or "").lower()
    fear = "extreme fear" in (frt or "").lower()
    if regime.get("qqq_above20") and regime.get("spy_above20") and (v or 99) < C.VIX_ELEVATED and not froth:
        st.success("Risk-on: QQQ & SPY above 20DMA, VIX subdued.")
    elif regime.get("qqq_above20") is False and regime.get("qqq_above50") is False:
        st.error("Distribution: QQQ below 20 & 50 DMA.")
    elif froth: st.warning("Extreme Greed — don't chase.")
    elif fear: st.warning("Extreme Fear — contrarian watch.")
    else: st.warning("Mixed tape. Be selective.")

def panel_sectors(con, today):
    st.subheader("② Sector rotation")
    df = q(con, "SELECT symbol,pct_1d,pct_5d,pct_20d,above50 FROM snapshot "
                "WHERE snapshot_date=? AND kind='sector'", [today])
    if df.empty: st.caption("— no data —"); return
    spy = q(con, "SELECT pct_20d FROM snapshot WHERE snapshot_date=? AND symbol='SPY'", [today])
    spy20 = float(spy.iloc[0, 0]) if not spy.empty and pd.notna(spy.iloc[0, 0]) else 0.0
    df["name"] = df["symbol"].map(C.SECTORS)
    df["RS_20d"] = (df["pct_20d"] - spy20).round(2)
    df = df.sort_values("RS_20d", ascending=False)
    df = df[["name","symbol","pct_1d","pct_5d","pct_20d","RS_20d","above50"]]
    show_table(df.rename(columns={"pct_1d":"%1d","pct_5d":"%5d","pct_20d":"%20d"}),
               signed_cols=["%1d","%5d","%20d","RS_20d"], bool_cols=["above50"])
    st.caption(f"Leading: **{df.iloc[0]['name']}** · Lagging: **{df.iloc[-1]['name']}**")

def panel_etfs(con, today):
    st.caption("**ETF pullback monitor**")
    df = q(con, "SELECT symbol,close,pct_1d,off_high20_pct,dist20_pct,dist50_pct,"
                "above20,above50,pullback_flag FROM snapshot WHERE snapshot_date=? AND kind='etf' "
                "ORDER BY pullback_flag DESC, off_high20_pct ASC", [today])
    if df.empty: st.caption("— no data —"); return
    show_table(df.rename(columns={"pct_1d":"%1d","off_high20_pct":"off_20dHi%",
               "dist20_pct":"vs20dma%","dist50_pct":"vs50dma%"}),
               signed_cols=["%1d","vs20dma%","vs50dma%"],
               bool_cols=["above20","above50","pullback_flag"])
    st.caption("**pullback_flag** = off 20d high but holding rising 20DMA (buyable dip).")

def panel_watchlist(con, today, ev):
    st.caption("**Watchlist setups**")
    df = q(con, """SELECT s.symbol, s.pct_1d, s.off_high20_pct, s.dist20_pct, s.dist50_pct,
               s.pullback_flag, s.setup_score, iv.atm_iv, e.next_earnings
        FROM snapshot s LEFT JOIN iv_atm iv ON iv.symbol=s.symbol AND iv.date=s.snapshot_date
        LEFT JOIN earnings e ON e.symbol=s.symbol
        WHERE s.snapshot_date=? AND s.kind='watch'
        ORDER BY s.setup_score DESC, s.off_high20_pct ASC""", [today])
    if df.empty: st.caption("— no data —"); return
    from ingest import iv_rank, realized_vol
    # IV rank
    ivr = q(con, "SELECT symbol, atm_iv, date FROM iv_atm ORDER BY symbol, date")
    rank_map = {sym: iv_rank(g["atm_iv"]) for sym, g in ivr.groupby("symbol")} if not ivr.empty else {}
    df["iv_rank"] = df["symbol"].map(rank_map)
    # HV vs IV
    wl = list(df["symbol"])
    ph = q(con, f"SELECT symbol,date,close FROM prices WHERE symbol IN ({','.join(['?']*len(wl))}) "
               "ORDER BY symbol,date", wl)
    hv_map = {sym: realized_vol(g["close"]) for sym, g in ph.groupby("symbol")} if not ph.empty else {}
    df["HV20"] = df["symbol"].map(hv_map)
    df["HV/IV"] = (df["HV20"] / df["atm_iv"]).round(2).where(df["atm_iv"].notna() & df["HV20"].notna())
    opex_date = ev["opex"].date
    df["earn"] = df["next_earnings"].apply(
        lambda d: "⚠" if pd.notna(d) and pd.to_datetime(d).date() <= opex_date else "")
    df = df[["symbol","setup_score","pullback_flag","pct_1d","off_high20_pct",
             "dist20_pct","dist50_pct","atm_iv","HV20","HV/IV","iv_rank","next_earnings","earn"]]
    show_table(df.rename(columns={"pct_1d":"%1d","off_high20_pct":"off_20dHi%",
               "dist20_pct":"vs20dma%","dist50_pct":"vs50dma%","atm_iv":"ATM_IV%"}),
               signed_cols=["%1d","vs20dma%","vs50dma%"], bool_cols=["pullback_flag"])
    st.caption(f"IV rank ≥ {C.IV_RANK_HOT:.0f} = rich → spreads. "
               "HV/IV < 0.8 = IV expensive; > 1.2 = IV cheap. ⚠ = earnings before OPEX.")

def panel_flow(con, today):
    st.caption("**Options flow — OI delta, vol/OI**")
    prev = q(con, "SELECT max(snapshot_date) d FROM options_oi WHERE snapshot_date<?", [today])
    prev_date = None if prev.empty or pd.isna(prev.iloc[0, 0]) else pd.to_datetime(prev.iloc[0, 0]).date()
    cur = q(con, "SELECT symbol,expiry,strike,oi,volume,iv,otm_pct,vol_oi,spot "
                 "FROM options_oi WHERE snapshot_date=?", [today])
    if cur.empty: st.caption("— no options snapshot —"); return
    if prev_date:
        pr = q(con, "SELECT symbol,expiry,strike,oi AS prev_oi FROM options_oi WHERE snapshot_date=?",
               [prev_date])
        cur = cur.merge(pr, on=["symbol","expiry","strike"], how="left")
        cur["oi_delta"] = (cur["oi"] - cur["prev_oi"]).astype("Int64")
    else:
        cur["oi_delta"] = pd.NA
        st.caption("OI delta needs ≥ 2 daily snapshots.")
    flagged = cur[(cur["vol_oi"] >= C.VOL_OI_MIN) | (cur["oi_delta"].fillna(0) >= C.OI_DELTA_MIN)].copy()
    view = flagged if not flagged.empty else cur.sort_values("vol_oi", ascending=False).head(20)
    view = view.sort_values(["oi_delta","vol_oi"], ascending=False)
    view = view[["symbol","expiry","strike","otm_pct","oi","oi_delta","volume","vol_oi","iv","spot"]]
    show_table(view.rename(columns={"otm_pct":"OTM%","oi_delta":"ΔOI(o/n)",
               "vol_oi":"vol/OI","iv":"IV%"}), signed_cols=["OTM%"], height=360)
    st.caption(f"vol/OI ≥ {C.VOL_OI_MIN} or ΔOI ≥ {C.OI_DELTA_MIN}. Rising ΔOI = opening interest.")

def panel_news(con, today):
    st.caption("**Geo / macro headlines**")
    df = q(con, "SELECT score,title,source,link FROM news WHERE snapshot_date=? "
                "ORDER BY score DESC, ts DESC LIMIT 25", [today])
    if df.empty: st.caption("— no headlines —"); return
    hi = df[df["score"] >= 2]
    if not hi.empty:
        st.markdown("**Elevated:**")
        for r in hi.itertuples(): st.markdown(f"- `{r.score}` [{r.title}]({r.link})")
    with st.expander(f"All headlines ({len(df)})"):
        for r in df.itertuples(): st.markdown(f"- `{r.score}` [{r.title}]({r.link})")

def _season_chart(data, name_col, name_map, order, highlight_val, title):
    """Reusable bar chart for seasonality."""
    grp = data.groupby(name_col)["ret"].agg(["mean","count"]).reset_index()
    grp.columns = [name_col, "ret", "n"]
    grp["name"] = grp[name_col].map(name_map)
    grp["_color"] = grp.apply(lambda r: "#2563eb" if r[name_col] == highlight_val
                              else (GREEN if r["ret"] > 0 else RED), axis=1)
    chart = alt.Chart(grp).mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(
        x=alt.X("name:N", sort=order, title=""),
        y=alt.Y("ret:Q", title="Avg daily return (%)"),
        color=alt.Color("_color:N", scale=None),
        tooltip=["name:N", alt.Tooltip("ret:Q", title="Avg %", format=".3f"),
                 alt.Tooltip("n:Q", title="# days")]).properties(height=220)
    st.altair_chart(chart, width="stretch")

def panel_seasonality(con, today):
    st.caption("**Seasonality**")
    df = q(con, "SELECT date, close FROM prices WHERE symbol='SPY' ORDER BY date")
    if len(df) < 60: st.caption("— need more history —"); return
    df["date"] = pd.to_datetime(df["date"])
    df["ret"] = df["close"].pct_change() * 100
    df = df.dropna(subset=["ret"])
    MO = {1:"Jan",2:"Feb",3:"Mar",4:"Apr",5:"May",6:"Jun",
          7:"Jul",8:"Aug",9:"Sep",10:"Oct",11:"Nov",12:"Dec"}
    DA = {0:"Mon",1:"Tue",2:"Wed",3:"Thu",4:"Fri"}
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Monthly (SPY)**")
        df["month"] = df["date"].dt.month
        _season_chart(df, "month", MO, list(MO.values()), today.month, "Monthly")
    with c2:
        st.markdown("**Day-of-week (SPY)**")
        df["dow"] = df["date"].dt.dayofweek
        _season_chart(df, "dow", DA, list(DA.values()), today.weekday(), "DOW")
    st.caption(f"{len(df)} trading days · Blue = current. Accumulates with each ingest.")

# --- main ---
def main():
    today = dt.date.today()
    st.title("Pre-Trade Dashboard")
    st.caption(f"{today:%A, %B %d, %Y}")
    con = get_con()
    if con is None:
        st.warning("No database yet."); run_ingest_button("Run first ingest"); return
    li, snap_date = last_ingest(con), latest_snapshot_date(con)
    top = st.columns([3, 1])
    with top[0]:
        if li: st.caption(f"Last ingest: **{li}**" + (f" · data **{snap_date}**" if snap_date else ""))
        if snap_date and snap_date != today:
            st.info(f"Showing {snap_date}; today's ingest hasn't run.")
    with top[1]: run_ingest_button(con=con)
    use_date = snap_date or today
    ev = gather_events(today)
    regime = _regime_vals(con, use_date)
    render_banner(*compute_banner(today, ev, regime))
    panel_overview(con, use_date, ev, regime)
    panel_sectors(con, use_date)
    st.divider()
    tabs = st.tabs(["ETFs", "Watchlist", "Flow", "News", "Seasonality"])
    with tabs[0]: panel_etfs(con, use_date)
    with tabs[1]: panel_watchlist(con, use_date, ev)
    with tabs[2]: panel_flow(con, use_date)
    with tabs[3]: panel_news(con, use_date)
    with tabs[4]: panel_seasonality(con, use_date)
    con.close()

if __name__ == "__main__":
    main()
