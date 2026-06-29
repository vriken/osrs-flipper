"""Static configuration: API, tax rules, parameter defaults, watchlists.

Plain-Python module (no YAML/pydantic) to match the sibling `market-flows` repo.
Override the User-Agent contact and Discord webhook via environment variables.
"""

import datetime as dt
import os
from pathlib import Path

# --- Paths -------------------------------------------------------------------
DATA_DIR = Path(__file__).parent.parent / "data"
DB_PATH = DATA_DIR / "osrs.duckdb"

# --- API ---------------------------------------------------------------------
API_BASE = "https://prices.runescape.wiki/api/v1/osrs"

# The Wiki API requires a descriptive User-Agent with contact info. python-requests
# and curl default UAs are pre-emptively blocked. Set OSRS_FLIPPER_CONTACT to your
# Discord/email so they can reach you if your usage causes problems.
_CONTACT = os.environ.get("OSRS_FLIPPER_CONTACT", "set OSRS_FLIPPER_CONTACT env var")
USER_AGENT = f"osrs-flipper (flip analysis, manual trading) - {_CONTACT}"

# Be a good citizen: the API has no hard rate limit but asks you not to sustain
# multiple large queries per second. The bulk endpoints return all items at once.
MIN_REQUEST_INTERVAL_S = 1.0
HTTP_TIMEOUT = int(os.environ.get("OSRS_FLIPPER_HTTP_TIMEOUT", 15))  # fail fast, don't freeze the REPL

# Response caching. TTLs are matched to how fast each endpoint actually changes, so
# we avoid redundant calls WITHOUT serving stale data — per-item staleness is judged
# from each item's own lowTime/highTime, which are correct even in a cached response.
CACHE_ENABLED = os.environ.get("OSRS_FLIPPER_NO_CACHE", "0") != "1"
CACHE_TTL_S = {
    "/mapping": 21600,   # 6h — static metadata
    "/latest": 30,
    "/5m": 60,
    "/1h": 120,
    "/timeseries": 300,  # 5m — the heavy per-item endpoint (40 calls per scan)
}
CACHE_DEFAULT_TTL_S = 60

# --- Grand Exchange tax (dated: rate changed 1% -> 2% on 2025-05-29) ----------
TAX_RATE_NUM = 2  # current rate numerator (2%)
TAX_RATE_NUM_OLD = 1  # pre-2025-05-29 (1%)
TAX_RATE_DEN = 100
TAX_CHANGE_DATE = dt.date(2025, 5, 29)
TAX_CAP = 5_000_000  # max tax per item, regardless of price
TAX_MIN_PRICE = 50  # items below this are effectively exempt (tax floors to 0)

# Items fully exempt from GE tax. Bonds (13190) charge their own conversion fee;
# the wiki also exempts ~45 new-player tools. This is intentionally a PARTIAL set:
# unknown items default to TAXED (conservative — we'd under-rank a true exempt item
# rather than over-rank a taxed one). Extend from:
# https://oldschool.runescape.wiki/w/Category:Items_exempt_from_Grand_Exchange_tax
EXEMPT_ITEM_IDS: frozenset[int] = frozenset({13190})  # Old school bond

# --- Fill model parameters (see fills.py) ------------------------------------
BETA = 0.25  # spread haircut: buy at low+β·spread, sell at high−β·spread
GAMMA = 0.15  # per-bar fill capture as fraction of contra-volume
ALPHA = 0.10  # capacity capture as fraction of window volume
ADVERSE_GATE = True  # only fill in bars where price moved against you (flat-or-worse)
WINDOW_BARS_5M = 24  # holding window = 24×5m = 2h (reset-bounded to 4h buy-limit window)
BUY_LIMIT_WINDOW_H = 4  # rolling buy-limit window

# --- Strategy parameters -----------------------------------------------------
Z_ENTRY = 2.0  # mean-reversion: enter when z < −Z_ENTRY
Z_EXIT = 0.5  # mean-reversion: exit when z ≥ −Z_EXIT
Z_WINDOW = 168  # rolling window for z-score (1h bars = 7 days)
VOL_Z_BREAKOUT = 2.0  # momentum: require volume z-score above this
BREAKOUT_RANGE_DAYS = 14  # momentum: lookback for the price range break

# --- Liquidity / staleness gating (see features.py) --------------------------
TAU_S = 1800  # staleness decay for liquidity score (30 min)
STALENESS_MAX_S = 3600  # exclude items whose last trade is older than this (1h)
V_MIN_1H = 50  # minimum 1h volume (binding side) to be considered tradeable
V_SUSPICIOUS_1H = 100  # below this + wide spread => flag manipulation-suspect

# --- Spread persistence (see persistence.py) ---------------------------------
PERSIST_TIMESTEP = "1h"  # recent history to judge spread stability against
PERSIST_CANDIDATES = 40  # only deep-check the top snapshot candidates (1 API call each)
PERSIST_MIN_BARS = 48  # need ≥48 bars (~2 days at 1h) for an honest stat
PERSIST_MIN_FRAC = 0.6  # the spread must exist in ≥60% of recent bars

# Quote fill-rate estimation uses only the most recent bars so rates reflect the
# CURRENT price regime — stale volume from an old price level must not suggest
# buying below today's bid (e.g. an item that trended up).
QUOTE_RECENT_BARS = 72

# --- Scanner filters ---------------------------------------------------------
MIN_MARGIN_PCT = 0.02  # must clear the 2% tax to be worth listing
MIN_PRICE = 50  # ignore sub-tax-threshold junk
MAX_PRICE = None  # set to cap by item price; None = no cap
HIGH_VALUE_THRESHOLD = 250_000_000  # above this, effective tax dips below 2%

# --- Account type ------------------------------------------------------------
# Free-to-play accounts can only trade non-members items and have fewer GE slots.
# Set OSRS_FLIPPER_MEMBERS=1 (or flip the default) once you redeem a bond to unlock
# the full members market.
MEMBERS = os.environ.get("OSRS_FLIPPER_MEMBERS", "0") == "1"
GE_SLOTS = 8 if MEMBERS else 3  # simultaneous active offers — hard cap on parallel flips
BOND_ITEM_ID = 13190  # F2P-tradeable, tax-exempt; redeeming it converts the account to members

# --- Trader context ----------------------------------------------------------
# Live bankroll is the binding constraint for a small account: with little capital
# you hit `capital / buy_price` long before an item's buy limit, so the scanner caps
# suggested quantity by what you can actually afford. Backtests use a larger notional
# to read a strategy's capacity ceiling at scale.
BANKROLL = int(os.environ.get("OSRS_FLIPPER_BANKROLL", 200_000))
BACKTEST_BANKROLL = int(os.environ.get("OSRS_FLIPPER_BACKTEST_BANKROLL", 5_000_000))
SECONDS_PER_OFFER = 30  # manual click cost, for gp-per-active-minute metric

# --- Schedule (drives the time-aware `brief`) --------------------------------
# Active hours = fast online flips; outside them = overnight/patient plan.
AWAKE_START = int(os.environ.get("OSRS_FLIPPER_AWAKE_START", 9))   # hour you wake
AWAKE_END = int(os.environ.get("OSRS_FLIPPER_AWAKE_END", 23))     # hour you sleep
# Overnight buys need a fat margin cushion so a small overnight price drift can't go red.
OVERNIGHT_MIN_MARGIN = float(os.environ.get("OSRS_FLIPPER_OVERNIGHT_MIN_MARGIN", 0.04))

# --- Output ------------------------------------------------------------------
DISCORD_WEBHOOK_URL = os.environ.get("OSRS_FLIPPER_DISCORD_WEBHOOK")  # optional
