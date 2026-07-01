"""
config.py
---------
Everything you tune lives here. Edit tickers / thresholds; the rest reads from this.
"""
import os

DB_PATH = os.environ.get("PRETRADE_DB", os.path.join(os.path.dirname(__file__), "pretrade.duckdb"))

# --- Universe -------------------------------------------------------------- #
INDICES = ["SPY", "QQQ", "IWM", "DIA"]
VIX = "^VIX"

# Treasury-yield proxies (yfinance, no API key). Quoted in %; legacy x10 auto-normalized.
YIELDS = {
    "^IRX": "13-wk",
    "^FVX": "5-yr",
    "^TNX": "10-yr",
    "^TYX": "30-yr",
}

# Futures & macro proxies (overnight gap / dollar / oil / crypto)
FUTURES = {
    "ES=F": "S&P Fut",
    "NQ=F": "Nasdaq Fut",
    "RTY=F": "Russell Fut",
    "DX-Y.NYB": "DXY",
    "CL=F": "Crude",
    "BTC-USD": "Bitcoin",
}

# SPDR sectors + key semis, for rotation ranking.
SECTORS = {
    "XLK": "Tech", "XLF": "Financials", "XLE": "Energy", "XLV": "Health Care",
    "XLI": "Industrials", "XLY": "Cons Disc", "XLP": "Cons Staples",
    "XLU": "Utilities", "XLB": "Materials", "XLRE": "Real Estate", "XLC": "Comm Svcs",
    "SMH": "Semis (SMH)", "SOXX": "Semis (SOXX)",
}

# ETFs you watch for pullbacks.
# NOTE: "DRAM" has no liquid US ETF on Yahoo — it will come back empty and be flagged.
#       Use MU / SMH / SOXX as memory/semis proxies. Left in because you named it.
PULLBACK_ETFS = ["SOXL", "SMH", "SOXX", "GLD", "GDX", "TLT", "URA", "XLE", "QQQ", "DRAM"]

# Single-name watchlist (your AI-infra universe). Edit freely.
WATCHLIST = [
    "AXTI", "AAOI", "NBIS", "IREN", "APLD", "CRWV", "OKLO", "MP", "USAR", "CRML",
    "WULF", "GEV", "VST", "RGTI",
]

# Scanner — S&P sector leaders + momentum/growth + popular swing names.
# Combined with WATCHLIST + INDICES + PULLBACK_ETFS → ~100 tickers total.
SCANNER_TICKERS = [
    # Mega tech
    "AAPL", "MSFT", "GOOG", "AMZN", "META", "NVDA", "TSLA", "NFLX",
    # Semis
    "AVGO", "AMD", "MU", "QCOM", "ARM", "MRVL", "INTC", "LRCX", "AMAT",
    # Software / cloud / cyber
    "PLTR", "CRWD", "NET", "SNOW", "DDOG", "ZS", "PANW", "NOW",
    # AI / momentum
    "SMCI", "IONQ", "RKLB", "ANET", "DELL", "MSTR",
    # Crypto-adjacent
    "COIN", "MARA", "CLSK", "RIOT",
    # Energy
    "XOM", "CVX", "OXY", "SLB",
    # Financials
    "JPM", "GS", "BAC", "MS",
    # Industrials / defense
    "CAT", "DE", "BA", "LMT", "RTX",
    # Healthcare
    "LLY", "UNH", "ABBV", "JNJ",
    # Consumer
    "COST", "WMT", "HD",
]

# --- Parameters ------------------------------------------------------------ #
MA_PERIODS = [10, 20, 50, 200]
HIST_DAYS = 420                  # yfinance lookback (covers 200dma + IV-rank window)
PULLBACK_HIGH_LOOKBACK = 20      # "recent high" window
PULLBACK_NEAR_HIGH_PCT = 12.0    # had strength if within this % of the 20d high
RS_LOOKBACKS = [1, 5, 20]        # return windows for sector / RS ranking

VOL_OI_MIN = 2.0                 # vol/OI flow threshold
OI_DELTA_MIN = 250               # min overnight OI rise to flag "opening" interest
OTM_CALL_MAX_PCT = 30.0          # snapshot OTM calls up to +X% above spot
MAX_EXPIRIES = 3                 # nearest N expiries to snapshot per name
IV_RANK_WINDOW = 252             # trading days for the IV-rank percentile
IV_RANK_HOT = 70.0               # IV rank above this = "rich / crush risk"

# VIX regime zones
VIX_CALM = 17.0                  # below this = calm / normal
VIX_ELEVATED = 22.0              # at/above = elevated fear, premium pricey
VIX_PANIC = 30.0                 # at/above = high fear / whippy

# --- News (keyless RSS) ---------------------------------------------------- #
NEWS_FEEDS = [
    "https://news.google.com/rss/search?q=Federal+Reserve+OR+%22jobs+report%22+OR+CPI+OR+treasury+yield+when:2d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=war+OR+escalation+OR+sanctions+OR+tariff+OR+ceasefire+OR+strike+when:2d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=semiconductor+OR+%22export+controls%22+OR+%22rare+earth%22+OR+quantum+when:2d&hl=en-US&gl=US&ceid=US:en",
]
# Severity weights for the geo/macro headline scorer (matched case-insensitively).
GEO_KEYWORDS = {
    "war": 3, "invasion": 3, "missile": 3, "nuclear": 3, "airstrike": 3,
    "escalation": 2, "ceasefire": 2, "sanction": 2, "tariff": 2, "embargo": 2,
    "export control": 2, "retaliation": 2, "blockade": 2, "attack": 2, "strike": 1,
    "rate cut": 2, "rate hike": 2, "shutdown": 2, "default": 2,
}

# --- Sentiment ------------------------------------------------------------ #
# CNN stock-market Fear & Greed (0-100). Unofficial dataviz endpoint; needs a UA header.
# Components: momentum, strength, breadth, put/call, the VIX component, safe-haven, junk-bond.
FEAR_GREED_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"

# Optional: paste exact CPI/PPI/PCE dates here as (name, "YYYY-MM-DD", "note").
# Left empty on purpose — don't trust fabricated macro dates; fill from bls.gov / bea.gov.
EXTRA_MACRO_EVENTS: list[tuple[str, str, str]] = [
    # ("CPI", "2026-07-14", "8:30 ET — verify bls.gov"),
    # ("PPI", "2026-07-15", "8:30 ET — verify bls.gov"),
]
