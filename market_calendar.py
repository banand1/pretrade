"""
market_calendar.py
------------------
Holiday-aware expiry + macro-event logic for the pre-trade dashboard.
Pure date math on top of the real NYSE trading calendar (no network).

Surfaces, for any `today`:
  - next monthly OPEX (3rd Friday, rolled to prior session if the market is closed)
  - next quarterly triple/quad witching (3rd Fri of Mar/Jun/Sep/Dec)
  - quarter-end rebalance window (last N trading sessions of the quarter)
  - next FOMC rate decision (hardcoded, verified vs federalreserve.gov)
  - next jobs report / NFP (first Friday, shifted earlier if that day is a holiday)
  - the next NYSE market holiday
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pandas_market_calendars as mcal

NYSE = mcal.get_calendar("NYSE")

# FOMC rate-decision dates = day 2 of each meeting. Source: federalreserve.gov.
# 2026 verified Jun 2026; 2027 is the Fed's tentative schedule.
FOMC_DECISIONS = [
    dt.date(2026, 1, 28), dt.date(2026, 3, 18), dt.date(2026, 4, 29), dt.date(2026, 6, 17),
    dt.date(2026, 7, 29), dt.date(2026, 9, 16), dt.date(2026, 10, 28), dt.date(2026, 12, 9),
    dt.date(2027, 1, 27), dt.date(2027, 3, 17), dt.date(2027, 4, 28), dt.date(2027, 6, 9),
    dt.date(2027, 7, 28), dt.date(2027, 9, 15), dt.date(2027, 10, 27), dt.date(2027, 12, 8),
]
SEP_MONTHS = {3, 6, 9, 12}  # meetings that also publish the dot plot / projections
WITCHING_MONTHS = {3, 6, 9, 12}


# --------------------------------------------------------------------------- #
# trading-day primitives
# --------------------------------------------------------------------------- #
def _sched_dates(start: dt.date, end: dt.date) -> list[dt.date]:
    sched = NYSE.schedule(start_date=start, end_date=end)
    return [d.date() for d in sched.index]


def is_trading_day(d: dt.date) -> bool:
    return len(_sched_dates(d, d)) > 0


def prev_trading_day(d: dt.date) -> dt.date:
    probe = d - dt.timedelta(days=1)
    while not is_trading_day(probe):
        probe -= dt.timedelta(days=1)
    return probe


def next_trading_day(d: dt.date) -> dt.date:
    probe = d + dt.timedelta(days=1)
    while not is_trading_day(probe):
        probe += dt.timedelta(days=1)
    return probe


def trading_days_between(a: dt.date, b: dt.date) -> int:
    """Count of trading sessions in the half-open interval (a, b]. 0 if b <= a."""
    if b <= a:
        return 0
    return len(_sched_dates(a + dt.timedelta(days=1), b))


# --------------------------------------------------------------------------- #
# calendar helpers
# --------------------------------------------------------------------------- #
def first_friday(year: int, month: int) -> dt.date:
    d = dt.date(year, month, 1)
    return d + dt.timedelta(days=(4 - d.weekday()) % 7)  # Mon=0 .. Fri=4


def third_friday(year: int, month: int) -> dt.date:
    return first_friday(year, month) + dt.timedelta(days=14)


def add_months(d: dt.date, n: int) -> dt.date:
    m = d.month - 1 + n
    return dt.date(d.year + m // 12, m % 12 + 1, 1)


def monthly_opex(year: int, month: int) -> dt.date:
    """Equity options expiry = 3rd Friday, rolled back to the prior session if closed."""
    tf = third_friday(year, month)
    return tf if is_trading_day(tf) else prev_trading_day(tf)


def quarter_end(today: dt.date) -> dt.date:
    q_end_month = ((today.month - 1) // 3) * 3 + 3
    return add_months(dt.date(today.year, q_end_month, 1), 1) - dt.timedelta(days=1)


def last_trading_day_of_quarter(today: dt.date) -> dt.date:
    qe = quarter_end(today)
    return qe if is_trading_day(qe) else prev_trading_day(qe)


# --------------------------------------------------------------------------- #
# typed results
# --------------------------------------------------------------------------- #
@dataclass
class Expiry:
    date: dt.date
    is_quarterly: bool        # triple/quad witching
    holiday_adjusted: bool    # rolled off a closed 3rd Friday
    calendar_days: int
    trading_days: int


@dataclass
class MacroEvent:
    name: str
    date: dt.date
    note: str = ""
    calendar_days: int = 0
    trading_days: int = 0


def next_monthly_opex(today: dt.date) -> Expiry:
    y, m = today.year, today.month
    opex = monthly_opex(y, m)
    if opex < today:
        nm = add_months(dt.date(y, m, 1), 1)
        y, m = nm.year, nm.month
        opex = monthly_opex(y, m)
    tf = third_friday(y, m)
    return Expiry(
        date=opex,
        is_quarterly=m in WITCHING_MONTHS,
        holiday_adjusted=opex != tf,
        calendar_days=(opex - today).days,
        trading_days=trading_days_between(today, opex),
    )


def next_quarterly_witching(today: dt.date) -> Expiry:
    for off in range(13):
        cand = add_months(dt.date(today.year, today.month, 1), off)
        if cand.month in WITCHING_MONTHS:
            opex = monthly_opex(cand.year, cand.month)
            if opex >= today:
                tf = third_friday(cand.year, cand.month)
                return Expiry(opex, True, opex != tf,
                              (opex - today).days, trading_days_between(today, opex))
    raise RuntimeError("no witching found in 13-month window")


def quarter_end_window(today: dt.date, window: int = 5) -> tuple[bool, dt.date, int]:
    """(in_window, last_session_of_quarter, trading_days_until_it)."""
    ltq = last_trading_day_of_quarter(today)
    if today > ltq:                       # already rolled into the new quarter
        return (False, ltq, -1)
    td_left = trading_days_between(today, ltq)   # 0 if today == ltq
    return (td_left <= window, ltq, td_left)


def next_fomc(today: dt.date) -> MacroEvent | None:
    for d in FOMC_DECISIONS:
        if d >= today:
            note = "SEP / dot plot" if d.month in SEP_MONTHS else "statement + presser"
            return MacroEvent("FOMC rate decision", d, note,
                              (d - today).days, trading_days_between(today, d))
    return None


def next_nfp(today: dt.date) -> MacroEvent:
    """Jobs report ~ first Friday, 8:30 ET. If that Friday is a market holiday the BLS
    typically releases one business day earlier (e.g. Thu Jul 2, 2026)."""
    y, m = today.year, today.month
    ff = first_friday(y, m)
    if ff < today:
        nm = add_months(dt.date(y, m, 1), 1)
        ff = first_friday(nm.year, nm.month)
    note = "approx (first Friday) — verify on bls.gov"
    rel = ff
    if not is_trading_day(ff):
        rel = prev_trading_day(ff)
        note = f"shifted earlier — market closed {ff:%b %d} — verify on bls.gov"
    return MacroEvent("Jobs report (NFP)", rel, note,
                      (rel - today).days, trading_days_between(today, rel))


def next_market_holiday(today: dt.date) -> tuple[str, dt.date] | None:
    hols = NYSE.holidays().holidays  # numpy array of datetime64
    for h in hols:
        hd = (h.astype("datetime64[D]")).astype(dt.date) if hasattr(h, "astype") else h
        if isinstance(hd, dt.datetime):
            hd = hd.date()
        if hd >= today:
            # pandas_market_calendars exposes names via a separate map; keep it simple.
            return ("NYSE holiday", hd)
    return None
