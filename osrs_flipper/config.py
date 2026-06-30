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
# Reject an item when its live book and 1h average disagree by more than this fraction —
# the data is stale/pumped/manipulated and no price is trustworthy (e.g. a deflating pump).
PRICE_DIVERGENCE_MAX = float(os.environ.get("OSRS_FLIPPER_PRICE_DIVERGENCE_MAX", 0.5))
# Tighter, margin-aware guard layered on top of the absolute gate above: reject a flip when
# the recent DOWNWARD drift (1h-avg mid → live mid) has already eaten more than this fraction
# of the modeled margin. A 50% absolute divergence is far too loose — a 2% adverse move wipes
# a 3% flip. Tying the threshold to each item's own margin catches falling knives that the flat
# gate misses, while a fat-margin item tolerates more drift. Set to a large value to disable.
ADVERSE_MOVE_MAX_FRAC = float(os.environ.get("OSRS_FLIPPER_ADVERSE_MOVE_MAX_FRAC", 0.5))

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
# A HOLD accumulates and sells back over ~this many hours; caps hold qty by realizable volume
# (ALPHA × vol × HOLD_WINDOW_H) so the plan never tells you to hoard more than the market clears.
HOLD_WINDOW_H = 8

# --- Strategy parameters -----------------------------------------------------
Z_ENTRY = 2.0  # mean-reversion: enter when z < −Z_ENTRY
Z_EXIT = 0.5  # mean-reversion: exit when z ≥ −Z_EXIT
Z_WINDOW = 168  # rolling window for z-score (1h bars = 7 days)
VOL_Z_BREAKOUT = 2.0  # momentum: require volume z-score above this
BREAKOUT_RANGE_DAYS = 14  # momentum: lookback for the price range break

# --- Liquidity / staleness gating (see features.py) --------------------------
TAU_S = 1800  # staleness decay for liquidity score (30 min)
STALENESS_MAX_S = 3600  # exclude items whose last trade is older than this (1h)
V_MIN_1H = int(os.environ.get("OSRS_FLIPPER_V_MIN_1H", 500))  # min 1h volume (binding side) to recommend;
# thin items (low two-sided volume) are easily pumped and you can't reliably fill at the quoted price
# Liquidity is two-regime: bulk commodities trade in huge UNIT counts at tiny prices, while big-ticket
# gear (Barrows, GWD) trades a few units/hour at high prices — a flat units floor (V_MIN_1H) wrongly
# rejects the latter as illiquid, yet a handful of trades/hour clears your 1–8 unit position in minutes
# (Karil's skirt: 3 buys/h × 523k = ~1.5M gp/h turnover, faster to clear than a 11k-unit dart limit).
# So an item is liquid if EITHER side qualifies: enough UNITS (commodities, V_MIN_1H) OR enough gp
# TURNOVER on the binding side (gear). A tiny units floor (V_FLOOR_1H) guards the turnover branch so a
# 1-trade/hour item can't pass on price alone — you couldn't reliably fill even one unit. Manipulation
# is then caught by VALUE (turnover) + the spread-persistence/divergence guards, not by raw unit count.
TURNOVER_MIN_1H = int(os.environ.get("OSRS_FLIPPER_TURNOVER_MIN_1H", 1_000_000))  # gp/h, binding side
V_FLOOR_1H = int(os.environ.get("OSRS_FLIPPER_V_FLOOR_1H", 2))  # min binding units/h on the turnover branch
V_SUSPICIOUS_1H = 100  # below this + wide spread => flag manipulation-suspect
# Spread sanity: a wide bid-ask is only real if volume trades across it. Above REL_SPREAD_SUSPECT,
# require vol_binding ≥ SPREAD_VOL_K × rel_spread — a wide spread on thin volume is an illiquidity/
# manipulation artifact you can't capture (e.g. Curry leaf 47% spread @ ~900/h). Penny staples
# (fat % spread, huge volume — Air rune 4→5) pass because the volume backs them.
REL_SPREAD_SUSPECT = float(os.environ.get("OSRS_FLIPPER_REL_SPREAD_SUSPECT", 0.20))
SPREAD_VOL_K = int(os.environ.get("OSRS_FLIPPER_SPREAD_VOL_K", 50_000))
# A wide spread measured with one trade leg this stale spans two price regimes (a phantom margin,
# e.g. a 37-min-old low paired with a fresh high) — treat as suspect, not a flip.
STALE_LEG_MAX_S = int(os.environ.get("OSRS_FLIPPER_STALE_LEG_MAX_S", 1800))

# --- Anomaly / manipulation detector (see anomaly.py) ------------------------
# Flag items whose live price has dislocated from their recent baseline ON REAL VOLUME — pumps
# (avoid) and over-dumps (mean-revert buy). The scanner filters these out; the `anomaly` command
# surfaces them. Need both a sizable divergence AND an abnormal-volume z-score to call it
# manipulation rather than ordinary drift or thin-item noise.
ANOMALY_DIV_MIN = float(os.environ.get("OSRS_FLIPPER_ANOMALY_DIV_MIN", 0.15))   # live vs 1h-avg gap
ANOMALY_MIN_VOL = int(os.environ.get("OSRS_FLIPPER_ANOMALY_MIN_VOL", 1000))     # real volume floor
ANOMALY_VOL_Z_MIN = float(os.environ.get("OSRS_FLIPPER_ANOMALY_VOL_Z_MIN", 2.0))  # abnormal-volume z
ANOMALY_CANDIDATES = int(os.environ.get("OSRS_FLIPPER_ANOMALY_CANDIDATES", 30))   # deep-check cap

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
# Minimum after-tax margin PER UNIT (gp). A 1gp integer-tick flip on a cheap item shows a huge
# ROI% (Feather 2→3 reads "50%") but doesn't fill — you sit behind a wall of identical 1gp bids
# (the fill-rate calibration measured ≈0 on exactly these). The % floors can't catch it because
# the % is inflated by the tiny price; gate on absolute margin instead. FUTURE: replace this
# static floor by auto-applying the (shrunk) measured fill calibration so the model self-corrects.
MIN_NET_MARGIN = int(os.environ.get("OSRS_FLIPPER_MIN_NET_MARGIN", 2))
MAX_PRICE = None  # set to cap by item price; None = no cap
HIGH_VALUE_THRESHOLD = 250_000_000  # above this, effective tax dips below 2%

# Ranking blend: the composite score is gp/cycle ÷ fill_eta^time_weight, tilted toward margin% by
# × margin_pct^roi_weight. The roi_weight is TIME/MODE-aware so the ranking matches the plan:
#   ACTIVE flipping (daytime, online/balanced modes) → ROI_WEIGHT_FAST. 0 = pure throughput: rank on
#     total gold per cycle (margin × quantity), so high-volume, high-quantity, thin-margin commodity
#     flips you cycle while online rise to the top.
#   OVERNIGHT (offline/slow mode) → ROI_WEIGHT_SLOW. Favour fat-margin% items worth leaving for hours.
# `go` already switches day→fast, overnight→slow at AWAKE_END − NIGHT_SWITCH_H (20:00 by default),
# so volume leads while you're active and margin leads once you've gone to bed.
ROI_WEIGHT_FAST = float(os.environ.get("OSRS_FLIPPER_ROI_WEIGHT_FAST", 0.0))
ROI_WEIGHT_SLOW = float(os.environ.get("OSRS_FLIPPER_ROI_WEIGHT_SLOW", 0.5))
SCORE_ROI_WEIGHT = float(os.environ.get("OSRS_FLIPPER_SCORE_ROI_WEIGHT", ROI_WEIGHT_FAST))  # fallback
# Daytime HOLD (accumulate) quality floor: don't park overflow cash in a slow hold unless it
# clears this ROI. Stricter than the 1% active floor — a hold ties capital up for hours, so it
# must earn its keep; below this, leave the cash liquid to recycle through the active slots.
HOLD_MIN_MARGIN = float(os.environ.get("OSRS_FLIPPER_HOLD_MIN_MARGIN", 0.03))

# --- Patient / big-ticket "gear" mode (see cmd_gear) -------------------------
# Low-frequency, high-value items (Barrows, GWD gear) are flipped by posting AT the bid/ask and
# waiting, not by queue-jumping — so they capture close to the FULL spread. PATIENT_BETA models that
# (0 = full spread; the default 0.25 haircut assumes you post inside the spread to fill fast, which
# eats almost all of a tight-% big-ticket margin). Optimistic by design: whether you actually fill at
# the extremes is exactly what live calibration measures. Staleness is relaxed too — a slow item
# legitimately trades less than once an hour, so the 1h ghost gate would wrongly hide it.
PATIENT_BETA = float(os.environ.get("OSRS_FLIPPER_PATIENT_BETA", 0.0))
PATIENT_STALENESS_S = int(os.environ.get("OSRS_FLIPPER_PATIENT_STALENESS_S", 21600))  # 6h
# `gear` lists only items at/above this unit price — the genuinely big-ticket, bought-one-at-a-time
# stuff (Barrows, GWD, weapons). Below it, a sub-500-binding hour is just a normal item having a
# quiet leg, not a gear flip, and belongs in `scan`.
GEAR_MIN_PRICE = int(os.environ.get("OSRS_FLIPPER_GEAR_MIN_PRICE", 50_000))

# --- Account type ------------------------------------------------------------
# Members account (bond redeemed): full market + 8 GE slots. Set OSRS_FLIPPER_MEMBERS=0
# to simulate F2P (non-members items only, 3 slots) — e.g. for testing the F2P path.
MEMBERS = os.environ.get("OSRS_FLIPPER_MEMBERS", "1") == "1"
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
# Runway-to-bed switch: when fewer than this many hours remain before AWAKE_END, a flip placed
# now can't round-trip (buy + sell) before you sleep, so `go` hands you the overnight plan
# (fat-margin holds safe to leave) instead of fast day flips. ~one balanced round-trip (2h) +
# buffer. With AWAKE_END=23 and 3h, `go` flips to night trades at 20:00.
NIGHT_SWITCH_H = float(os.environ.get("OSRS_FLIPPER_NIGHT_SWITCH_H", 3))

# --- Recovery hold (see recovery.py) -----------------------------------------
# For an underwater holding: hold for a bounce (and maybe double down) only if it traded ≥
# RECOVERY_MIN_GREEN above your cost in the past week, is now ≥ RECOVERY_MIN_DIP below its week
# median (or z ≤ RECOVERY_Z below the week mean), and isn't in a steady week-long decline (which
# reads as a re-rating, not a dip). Conservative on purpose — doubling down on a falling knife
# is how a paper loss becomes a real one.
RECOVERY_LOOKBACK_BARS = int(os.environ.get("OSRS_FLIPPER_RECOVERY_LOOKBACK_BARS", 168))  # ~1wk of 1h bars
RECOVERY_MIN_GREEN = float(os.environ.get("OSRS_FLIPPER_RECOVERY_MIN_GREEN", 0.02))
RECOVERY_MIN_DIP = float(os.environ.get("OSRS_FLIPPER_RECOVERY_MIN_DIP", 0.03))
RECOVERY_Z = float(os.environ.get("OSRS_FLIPPER_RECOVERY_Z", -1.0))

# `review` margin-gone guard. The live book is a single noisy last-trade tick; on a thin
# spread it flickers to 0 constantly, so two guards stop false "cancel/re-quote" alarms:
#  - MIN_AGE_H: a real adverse move takes longer than seconds — don't judge a just-placed
#    order off one tick (this is why a 1-minute-old order should never read "margin gone").
#  - FLOOR: skip thin-margin staples (a ≤3gp spread flickering to 0 is tick noise on a 30gp item,
#    not a loss worth a cancel); the alarm only matters where there's a real margin to lose.
REVIEW_MARGIN_MIN_AGE_H = float(os.environ.get("OSRS_FLIPPER_REVIEW_MARGIN_MIN_AGE_H", 0.25))
REVIEW_MARGIN_FLOOR = int(os.environ.get("OSRS_FLIPPER_REVIEW_MARGIN_FLOOR", 3))

# --- Output ------------------------------------------------------------------
DISCORD_WEBHOOK_URL = os.environ.get("OSRS_FLIPPER_DISCORD_WEBHOOK")  # optional
# Background attention monitor: how often to poll RuneLite for offers that need you
# (filled → collect, margin gone, stale). Pushes to Discord only on NEW events (de-duped).
ALERT_POLL_S = int(os.environ.get("OSRS_FLIPPER_ALERT_POLL_S", 60))
