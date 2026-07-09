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
# cross-machine learning sync (no server): each install exports sync/<device>.json here; committing it to
# the repo (this dir is NOT gitignored, unlike data/) carries your attempts+blacklist to the other machine.
SYNC_DIR = Path(os.environ.get("OSRS_FLIPPER_SYNC_DIR", str(DATA_DIR.parent / "sync")))
# how long after a pull to wait before judging whether it was a good call (give the market time to move)
PULL_EVAL_DELAY_S = int(os.environ.get("OSRS_FLIPPER_PULL_EVAL_DELAY_S", 1800))  # 30 min
# hysteresis: an OUTRANKED rec must stay out of the plan this long before it's pulled, so 60s-tick rank
# flutter doesn't pull-then-reopen the same opportunity as a churny new episode (margin-gone pulls now)
PULL_GRACE_S = int(os.environ.get("OSRS_FLIPPER_PULL_GRACE_S", 600))  # 10 min

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
# Reject an item when its live book and its recent-bar median disagree by more than this fraction —
# a glitchy tick, a deflating pump, or a price mid-swing where no point estimate is trustworthy. The
# quote anchors to the live book, so this is the "don't flip while it's moving" guard. 0.5 was so
# loose it never fired (a falling item 9% off its norm sailed through and got quoted at stale prices).
PRICE_DIVERGENCE_MAX = float(os.environ.get("OSRS_FLIPPER_PRICE_DIVERGENCE_MAX", 0.15))
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
BETA = 0.25  # spread haircut PRIOR: buy at low+β·spread, sell at high−β·spread. The live value is
# auto-calibrated from your real fills (shrunk toward this prior) and refreshed every
# CALIBRATE_EVERY_TRADES resolved attempts — this is just the starting point / fallback.
CALIBRATE_EVERY_TRADES = int(os.environ.get("OSRS_FLIPPER_CALIBRATE_EVERY_TRADES", 10))
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
# Even a "normally"-priced item (|div| < ANOMALY_DIV_MIN) is not buyable if it's actively falling this
# fast: recent 1-bar slope as a fraction of the baseline. Catches a falling knife before it crosses
# the divergence band — the exact case where 5-min-lagged aggregates mislead. More negative = stricter.
ANOMALY_FALL_SLOPE = float(os.environ.get("OSRS_FLIPPER_ANOMALY_FALL_SLOPE", -0.03))
# Structural manipulability: an item is cheap to corner when it's THIN (few units trade) and
# VALUABLE (each unit moves real gp) — a handful of buy-limit-15 offers can walk the price. Such
# items get extra scrutiny: a slow pump (live elevated vs the 3-MONTH baseline even when the 2-week
# median has already followed it up) is treated as unbuyable, and gear flags them. Liquid items are
# exempt (a real re-rating on volume isn't manipulation), so this never blocks the bread-and-butter.
MANIP_VOL_MAX = int(os.environ.get("OSRS_FLIPPER_MANIP_VOL_MAX", 200))       # ≤ this 1h binding vol = thin
MANIP_PRICE_MIN = int(os.environ.get("OSRS_FLIPPER_MANIP_PRICE_MIN", 50_000))  # ≥ this price = worth cornering

# --- Spread persistence (see persistence.py) ---------------------------------
PERSIST_TIMESTEP = "1h"  # recent history to judge spread stability against
PERSIST_CANDIDATES = 40  # only deep-check the top snapshot candidates (1 API call each)
PERSIST_MIN_BARS = 48  # need ≥48 bars (~2 days at 1h) for an honest stat
PERSIST_MIN_FRAC = 0.6  # the spread must exist in ≥60% of recent bars

# --- Fast margin-decay guard (fine-grained, see persistence.reliability_stats) -----
# The 1h/2-day check above can't see a spread that collapses within MINUTES (a 1h bar
# averages the flicker away), so a heavily-flipped tight item (Ultracompost) passes it
# yet the margin is gone 5 min after you place. This 5m short-window check catches that:
# how often, over the last hour, was the achievable post-tax margin at least RELIAB_RATIO
# of the quoted margin. Penalty-only multiplier, applied to fast flips only.
RELIAB_TIMESTEP = "5m"   # fine resolution — see the intraday collapse the 1h check misses
RELIAB_BARS = 12         # judge over the last ~hour (12 × 5m)
RELIAB_MIN_BARS = 6      # fewer valid bars than this → neutral (don't punish on thin data)
RELIAB_RATIO = 0.5       # a bar is "healthy" if its net ≥ this × the quoted net
RELIAB_MIN_NET = int(os.environ.get("OSRS_FLIPPER_RELIAB_MIN_NET", 2))  # abs gp floor for "healthy"
RELIAB_FLOOR = float(os.environ.get("OSRS_FLIPPER_RELIAB_FLOOR", 0.4))  # min multiplier (penalty cap)
RELIAB_HARD_FRAC = float(os.environ.get("OSRS_FLIPPER_RELIAB_HARD_FRAC", 0.0))  # >0 ⇒ DROP below this uptime

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
# Stack-aware ROI tilt: the roi_weight above is the LARGE-stack floor (mode sets it). A small stack is
# capital-bound, so it compounds fastest by tilting HARD to ROI% — we lift roi_weight to
# ROI_WEIGHT_SMALL_STACK when broke and fade it (log-interpolated over net worth) down to the mode floor
# as you scale, because a big stack can't push size through shallow high-ROI items. Always on, no flag.
ROI_WEIGHT_SMALL_STACK = float(os.environ.get("OSRS_FLIPPER_ROI_WEIGHT_SMALL_STACK", 0.6))
ROI_STACK_LO = int(os.environ.get("OSRS_FLIPPER_ROI_STACK_LO", 300_000))       # ≤ this → full small-stack tilt
ROI_STACK_HI = int(os.environ.get("OSRS_FLIPPER_ROI_STACK_HI", 20_000_000))    # ≥ this → mode floor
# Never-recommend list: item ids the scanner/gear/combos drop at the source (build_features). Seed via
# OSRS_FLIPPER_BLACKLIST="123,456"; the terminal `blacklist` command adds/removes at runtime (persisted).
BLACKLIST_IDS: set[int] = {int(x) for x in os.environ.get("OSRS_FLIPPER_BLACKLIST", "").replace(",", " ").split()
                           if x.isdigit()}
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

# --- Combinations: GE sets & (Phase 2) skilling recipes (see combos.py) ------
# Sets are big-ticket / low-frequency like `gear`, so they're priced patiently: full spread (β=0) and
# the relaxed 6h staleness gate (PATIENT_STALENESS_S) so thin set pieces aren't ghost-gated out. β=0 is
# best-case (assumes you fill AT the bid/ask) — captioned as such, same as `gear`.
COMBO_BETA = float(os.environ.get("OSRS_FLIPPER_COMBO_BETA", PATIENT_BETA))
COMBO_MIN_ROI = float(os.environ.get("OSRS_FLIPPER_COMBO_MIN_ROI", 0.0))  # hide sub-noise combos below this ROI
COMBO_ANOMALY_CHECK = os.environ.get("OSRS_FLIPPER_COMBO_ANOMALY_CHECK", "1") == "1"  # pump/knife gate on bought legs
COMBO_ANOMALY_CANDIDATES = int(os.environ.get("OSRS_FLIPPER_COMBO_ANOMALY_CANDIDATES", 30))  # deep-check cap
# `go` ranks fast flips (calibrated, honest) against gear/sets (β=0, best-case) on one per-slot scale.
# Best-case EV is haircut by this confidence before ranking, so an optimistic set can't crowd out an
# honestly-priced flip — a patient play wins a slot only when it beats a flip even after the discount.
PATIENT_EV_CONFIDENCE = float(os.environ.get("OSRS_FLIPPER_PATIENT_EV_CONFIDENCE", 0.55))

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

# A GE slot is a scarce, reusable resource: committing it to a flip has an opportunity cost —
# that slot could hold a far bigger position once your open offers return cash. So a new flip
# must clear a minimum profit to be WORTH a slot. The floor is DYNAMIC (not a hard-coded %): it's
# the opportunity cost of a slot = fair-share capital per slot (net_worth / slots) × the ROI the
# market is currently paying (median of the top candidates) × λ. Self-calibrating — bigger account
# or fatter market raises the bar, more slots lowers the per-slot bar. λ is how much of a fair-share
# slot's earning a flip must match to be worth taking. The "unless gains are huge" escape is
# automatic: profit = deploy × ROI, so a small high-ROI flip still clears it. Below it, cash stays
# liquid to consolidate rather than fragment into slot-unworthy flips.
SLOT_WORTH_LAMBDA = float(os.environ.get("OSRS_FLIPPER_SLOT_WORTH_LAMBDA", 0.5))

# Per-item edge tracker: a rolling, recency-weighted (EWMA) score of each item's REALIZED profit,
# fed into the ranking as a multiplier — so items that are actually losing you money right now get
# down-weighted, WITHOUT a permanent blocklist. Regimes shift, so it's adaptive:
#  - HALF_LIFE trades: an EWMA half-life (old trades fade), so the score tracks the current regime.
#  - FLOOR: a losing item is never banned, only floored to this multiplier — it keeps getting the
#    occasional shot, which is the only way to discover it turned good again (explore vs exploit).
#  - Penalty-only (cap 1.0): proven losers sink; everything else stays neutral (no over-concentration).
#  - SHRINK_K: shrink small samples toward neutral so a couple of trades barely move an item.
#  - FAST_HALF_LIFE: a shorter EWMA used only to flag REGIME SHIFTS (recent edge vs the baseline).
EDGE_HALF_LIFE = float(os.environ.get("OSRS_FLIPPER_EDGE_HALF_LIFE", 30))
EDGE_FLOOR = float(os.environ.get("OSRS_FLIPPER_EDGE_FLOOR", 0.3))
EDGE_GAIN = float(os.environ.get("OSRS_FLIPPER_EDGE_GAIN", 10.0))  # −7% realized ROI → the 0.3 floor
EDGE_SHRINK_K = float(os.environ.get("OSRS_FLIPPER_EDGE_SHRINK_K", 5))
EDGE_FAST_HALF_LIFE = float(os.environ.get("OSRS_FLIPPER_EDGE_FAST_HALF_LIFE", 8))

# Rebalancing: an active BUY holds a slot + capital that a better flip could use. Suggest
# cancelling it only when a candidate's ROI-per-hour (margin% ÷ fill-time — folds in margin,
# speed and capital efficiency) beats the offer's by SWAP_RATIO, AND the offer is still early
# (< SWAP_MAX_FILL filled) so we're not throwing away a nearly-done buy. Conservative on purpose:
# cancelling costs your queue position + the re-place clicks, so only a wide edge is worth it.
SWAP_RATIO = float(os.environ.get("OSRS_FLIPPER_SWAP_RATIO", 2.0))       # alt must be ≥2× the offer's ROI/h
SWAP_MAX_FILL = float(os.environ.get("OSRS_FLIPPER_SWAP_MAX_FILL", 0.5))  # only swap buys under 50% filled
SWAP_MIN_AGE_H = float(os.environ.get("OSRS_FLIPPER_SWAP_MIN_AGE_H", 0.5))  # don't cancel a just-placed buy
# Floor on any fill-time used in a ROI-per-hour rate. A high-volume flip's estimated fill can round
# toward 0, which makes margin% ÷ time explode to a meaningless "1000× faster" — no flip actually
# round-trips in zero time (place → fill → collect → list → fill → collect). Clamp the denominator so
# a near-instant flip can't dominate a fatter-margin one on a rounding artifact.
MIN_FILL_ETA_H = float(os.environ.get("OSRS_FLIPPER_MIN_FILL_ETA_H", 0.25))

# --- Schedule (drives the time-aware `brief`) --------------------------------
# Active hours = fast online flips; outside them = overnight/patient plan.
AWAKE_START = int(os.environ.get("OSRS_FLIPPER_AWAKE_START", 9))   # hour you wake
AWAKE_END = int(os.environ.get("OSRS_FLIPPER_AWAKE_END", 23))     # hour you sleep
# Overnight buys need a fat margin cushion so a small overnight price drift can't go red.
OVERNIGHT_MIN_MARGIN = float(os.environ.get("OSRS_FLIPPER_OVERNIGHT_MIN_MARGIN", 0.04))
# Target fill time for an overnight buy: you're asleep ~8h and can't cycle a slot, so bid LOW and
# aim to fill near morning rather than in 1-2h — a slower fill means a better buy price (fatter
# margin) with the slot working the whole night. The bid is chosen as the lowest that still fills
# within this window (the α-capture estimate is conservative, so real fills tend to beat it).
OVERNIGHT_FILL_TARGET_H = float(os.environ.get("OSRS_FLIPPER_OVERNIGHT_FILL_TARGET_H", 8))
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

# Reprice deadband for the review hints ("re-list nearer X", "re-quote buy/sell"). The market
# reference is the 5-MINUTE average (short-term context), not the instantaneous last tick, so a
# 1gp dip over a minute doesn't read as a move. On top of that, only advise a reprice when your
# price is off that reference by more than this fraction — a sub-deadband gap is tick jitter, and
# chasing it costs a re-list + your queue position for nothing. Relative so it's fair across prices
# (1gp on a 30gp item is 3% = real; 1gp on a 3000gp item is 0.03% = noise).
REPRICE_DEADBAND = float(os.environ.get("OSRS_FLIPPER_REPRICE_DEADBAND", 0.02))  # 2%
# The market reference blends the 5m average with the last tick, weighting the tick MORE the
# further it has diverged from the average — so noise is smoothed but a genuine sharp move (a
# crash or spike) still pulls the reference fast, instead of lagging 5 min behind it. This is the
# divergence at which we fully trust the last tick (a real move this big isn't noise).
REPRICE_BIG_MOVE = float(os.environ.get("OSRS_FLIPPER_REPRICE_BIG_MOVE", 0.08))  # 8%

# When a held SELL has fallen below your break-even, we hold at break-even rather than chase the
# market down into a loss — UNLESS the loss is worth taking: no near-term bounce AND a better home
# for the freed capital exists right now. "Better home" = a scanned flip that fills within
# CUT_ALT_MAX_ETA_H and clears CUT_ALT_MIN_ROI_H ROI/hour. Conservative so a cut is the exception.
CUT_ALT_MAX_ETA_H = float(os.environ.get("OSRS_FLIPPER_CUT_ALT_MAX_ETA_H", 1.0))    # "asap" = fills ≤ 1h
CUT_ALT_MIN_ROI_H = float(os.environ.get("OSRS_FLIPPER_CUT_ALT_MIN_ROI_H", 0.03))   # ≥3% ROI per hour

# --- Output ------------------------------------------------------------------
DISCORD_WEBHOOK_URL = os.environ.get("OSRS_FLIPPER_DISCORD_WEBHOOK")  # optional (one-way, one channel)
# Bot push (posts AS your bot, can edit a live status message): set both. Preferred over the webhook.
DISCORD_BOT_TOKEN = os.environ.get("OSRS_FLIPPER_DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = os.environ.get("OSRS_FLIPPER_DISCORD_CHANNEL_ID")
# Background attention monitor: how often to poll RuneLite for offers that need you
# (filled → collect, margin gone, stale). Pushes to Discord only on NEW events (de-duped).
ALERT_POLL_S = int(os.environ.get("OSRS_FLIPPER_ALERT_POLL_S", 60))
