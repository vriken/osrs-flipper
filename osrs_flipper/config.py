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
# a slow, partially-filled buy pings "bank the partial now" only when the bankable profit is at least
# this fraction of net worth — so it's "a lot" relative to your bankroll, not noise on thin/slow items
BANK_PARTIAL_MIN_FRAC = float(os.environ.get("OSRS_FLIPPER_BANK_PARTIAL_MIN_FRAC", 0.005))  # 0.5%

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

# Items fully exempt from GE tax — the complete GE-tradeable membership of the wiki's
# exempt category, resolved to ids via the prices-API /mapping (exact name match, 2026-07-09).
# Unknown items still default to TAXED (conservative — under-rank a true exempt item rather
# than over-rank a taxed one). Four category members are dosed/charged variants whose bare
# title doesn't pin a specific charge (Energy potion, Games necklace, Ring of dueling, Civitas
# illa fortis teleport) — skipped rather than guess a dose. Re-resolve from:
# https://oldschool.runescape.wiki/w/Category:Items_exempt_from_Grand_Exchange_tax
EXEMPT_ITEM_IDS: frozenset[int] = frozenset({
    13190,                                       # Old school bond
    558,                                         # Mind rune
    882, 884, 886,                               # Bronze / Iron / Steel arrow
    806, 807, 808,                               # Bronze / Iron / Steel dart
    315, 329, 347, 351, 355, 361, 365, 379,      # Shrimps, Salmon, Herring, Pike, Mackerel, Tuna, Bass, Lobster
    1891, 2140, 2142, 2309, 2327,                # Cake, Cooked chicken, Cooked meat, Bread, Meat pie
    233, 952, 1733, 1735, 1755, 1785, 2347,      # Pestle and mortar, Spade, Needle, Shears, Chisel, Glassblowing pipe, Hammer
    5325, 5329, 5331, 5341, 5343, 8794,          # Gardening trowel, Secateurs, Watering can, Rake, Seed dibber, Saw
    8007, 8008, 8009, 8010, 8011, 8013, 28790,   # Varrock/Lumbridge/Falador/Camelot/Ardougne/House/Kourend teleport tablets
})

# --- Fill model parameters (see fills.py) ------------------------------------
BETA = 0.25  # spread haircut PRIOR: buy at low+β·spread, sell at high−β·spread. The live value is
# auto-calibrated from your real fills (shrunk toward this prior) and refreshed every
# CALIBRATE_EVERY_TRADES resolved attempts — this is just the starting point / fallback.
CALIBRATE_EVERY_TRADES = int(os.environ.get("OSRS_FLIPPER_CALIBRATE_EVERY_TRADES", 10))
GAMMA = 0.15  # per-bar fill capture as fraction of contra-volume
ALPHA = 0.10  # capacity capture as fraction of window volume
# Market-impact EV haircut (see fills.market_impact_mult). A resting order claims a share
# p = size / contra-volume of what trades; taking a large share walks the price against you,
# shrinking the realized margin — a cost distinct from fill probability (modeled separately) and
# NOT captured by the α volume-cap alone (which limits size but still assumes you capture that
# α-share at the quoted price). mult = 1/(1 + IMPACT_K·p), floored at IMPACT_FLOOR; penalty-only.
# As the stack grows and more flips become α-share-bound rather than bankroll-bound, this cools the
# ranking on high-participation positions — the exact regime where modeled and live fills diverge.
# Starts as a static model term; IMPACT_K is the knob a future fill-calibration would tune. K=0 off.
IMPACT_K = float(os.environ.get("OSRS_FLIPPER_IMPACT_K", 1.0))
IMPACT_FLOOR = float(os.environ.get("OSRS_FLIPPER_IMPACT_FLOOR", 0.5))
# Hung-leg (trapped-capital) risk (see fills.hung_leg_mult). EV = qty·net·p_round scores a
# non-completed round as zero, but the state that actually hurts is a FILLED buy whose sell then
# hangs — capital stuck in inventory you grind out at an opportunity + markdown cost. This restores
# that expected cost as an EV haircut: mult = 1 − HUNG_LEG_COST_FRAC·(1−p_sell)/p_sell·(1/margin_pct),
# floored at HUNG_LEG_FLOOR. It bites thin-margin flips with a shaky sell leg (a trapped buy wipes
# them) and barely touches fat-margin flips with a reliable sell. p_sell is horizon-aware, so a
# patient/overnight quote is penalised less. Active-flip cost only (holds sell over time). FRAC=0 off.
HUNG_LEG_COST_FRAC = float(os.environ.get("OSRS_FLIPPER_HUNG_LEG_COST_FRAC", 0.01))
HUNG_LEG_FLOOR = float(os.environ.get("OSRS_FLIPPER_HUNG_LEG_FLOOR", 0.5))
# Representative horizon (hours) for the SNAPSHOT fill-probability estimate (fills.completion_probability).
# The snapshot ranks mode-agnostically, so it uses one horizon as the ordering proxy — the balanced (2h)
# day horizon — while the deep re-price uses the exact mode horizon (MODE_HORIZON). The snapshot and the
# quote share ONE leg shape (fills.leg_fill_prob), so snapshot ordering tracks the deep re-price instead
# of a different, harsher curve that could gate a good flip out before it's deep-checked.
SNAPSHOT_HORIZON_H = float(os.environ.get("OSRS_FLIPPER_SNAPSHOT_HORIZON_H", 2.0))
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
# ...and for the live ranking's attention discount (see planner._attention_discount): a flip that
# only reaches its gp/hour by cycling every few minutes demands constant clicking, so its per-slot
# score is discounted by the handling fraction of the window — ACTIONS_PER_CYCLE·(SECONDS_PER_OFFER/
# 3600)/cycle_h (the window cancels). Gently favours fewer-click flips/holds over hyperactive churn at
# equal gp; discounts the SCORE only, not the displayed gp. The cycle clock is floored at
# ATTENTION_MIN_ETA_H so a near-instant fill can't nuke the score. SECONDS_PER_OFFER or
# ACTIONS_PER_CYCLE = 0 disables it.
ACTIONS_PER_CYCLE = int(os.environ.get("OSRS_FLIPPER_ACTIONS_PER_CYCLE", 3))  # place-buy, collect, list-sell
ATTENTION_MIN_ETA_H = float(os.environ.get("OSRS_FLIPPER_ATTENTION_MIN_ETA_H", 0.1))

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

# Allocation concentration (see scanner._allocate). Capital is split across ranked picks in proportion
# to capital efficiency (expected return per gp), capped so no single position exceeds this fraction of
# the pile in the primary pass (leftover still tops up over idle cash). Lifts the best flip above the
# old even 1/N share — the compounding edge a small stack lives on — without letting one flip soak the
# whole pile. 1.0 disables the ceiling (pure weight-proportional-with-spillover); lower = more diversified.
MAX_ALLOC_FRAC = float(os.environ.get("OSRS_FLIPPER_MAX_ALLOC_FRAC", 0.5))

# Variance aversion (optional, OFF by default). Multiplies the ranking SCORE (not the EV estimate) by
# p_complete^VARIANCE_AVERSION, favouring reliable-completion flips over high-variance gambles — for the
# F2P bond grind, a steady small gain compounds to the milestone faster than a coin-flip. λ=0 is a no-op
# (default). Deliberately blunt and overlapping the hung-leg / optimizer's-curse-shrink risk terms, which
# price specific risks; this is a global dial on top for a risk-averse grind. Try ~0.5–1.0 to enable.
VARIANCE_AVERSION = float(os.environ.get("OSRS_FLIPPER_VARIANCE_AVERSION", 0.0))

# Adaptive objective (see objective.py). The ranking objective is gp/hour while competition is stable,
# and tilts toward variance-penalised gp/hour as competition ARRIVES — detected from a RISE in our OWN
# realized Sharpe (per active day) above its slow trailing baseline, NOT its absolute level (a high
# Sharpe at a small stack just means we're good; a rise vs baseline isolates the regime change). λ stays
# at the VARIANCE_AVERSION floor until Sharpe climbs above baseline, then ramps to VARIANCE_AVERSION_MAX
# as the rise reaches OBJ_SHARPE_RISE_FULL. OBJ_BASELINE_ALPHA is the EWMA weight for the baseline (small
# = slow = a sustained regime becomes the new normal and λ relaxes). HYPOTHESIS: a Sharpe rise signals a
# more efficient/crowded market — not guaranteed (competition compresses mean AND variance), so it's
# tunable and fully off when VARIANCE_AVERSION_MAX=0. Fill-rate accuracy is NOT a knob here: it's always
# applied upstream via the fill calibration on the EV inputs.
# Tuned from the real ledger (2026-07): cumulative Sharpe naturally swings ~±0.7 with the day-mix, and a
# calibration cycle is 10 resolved trades (~5h). α=0.05 → baseline half-life ~3 days (a regime stays
# flagged for days, not absorbed in hours); RISE_FULL=1.5 → only a climb well beyond the ~0.7 noise band
# reaches full λ, so ordinary Sharpe wobble doesn't false-trigger risk-aversion. Still coarse — no real
# competition transition has been observed yet to calibrate against.
OBJ_BASELINE_ALPHA = float(os.environ.get("OSRS_FLIPPER_OBJ_BASELINE_ALPHA", 0.05))
OBJ_SHARPE_RISE_FULL = float(os.environ.get("OSRS_FLIPPER_OBJ_SHARPE_RISE_FULL", 1.5))
# The competition signal compares a RECENT-window Sharpe (current) against the slow long-run baseline
# (EWMA of the all-history Sharpe). A cumulative "current" is too sluggish to spot a regime change; a
# recent window over OBJ_SHARPE_WINDOW_DAYS active days is responsive. GUARD: a window with fewer than
# OBJ_SHARPE_MIN_BUCKETS active-day return buckets can't measure volatility — fit_growth then substitutes
# a synthetic vol (MC_DEFAULT_CV·rate), which pins Sharpe at 1/MC_DEFAULT_CV (~1.67), a meaningless
# artifact. Below the guard we treat the reading as absent (λ stays at the floor) rather than trust it.
OBJ_SHARPE_WINDOW_DAYS = float(os.environ.get("OSRS_FLIPPER_OBJ_SHARPE_WINDOW_DAYS", 7))
OBJ_SHARPE_MIN_BUCKETS = int(os.environ.get("OSRS_FLIPPER_OBJ_SHARPE_MIN_BUCKETS", 3))
VARIANCE_AVERSION_MAX = float(os.environ.get("OSRS_FLIPPER_VARIANCE_AVERSION_MAX", 1.0))

# Crowding / competition tilt (ON by default, gentle). The durable edge is items nobody else bothers
# flipping — obscure, modest-volume niches where a real spread persists — NOT the high-turnover staples
# every bot races (there you're exit liquidity). This multiplies the ranking SCORE (never the EV) by a
# bounded factor from an item's both-side gp/hour turnover: > 1 for quiet niches below the pivot, < 1
# for crowded staples above it, and exactly 1 at the pivot. Like the variance dial it deliberately
# overlaps the fill-time term (throughput is already rewarded) — a blunt strategic tilt toward the
# uncrowded edge on top. 0 disables. Keep flipping niches as the stack grows (see the edge memo).
CROWDING_TILT = float(os.environ.get("OSRS_FLIPPER_CROWDING_TILT", 0.25))          # boost/penalty magnitude
CROWDING_PIVOT = int(os.environ.get("OSRS_FLIPPER_CROWDING_PIVOT", 50_000_000))    # gp/h where crowding = 0.5

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
# Overnight SAFETY gate (you're asleep for hours — a flat margin floor isn't enough because a fat-margin
# item can also be wildly volatile). Only leave a buy overnight if its margin cushion DOMINATES the
# item's daily swing: margin_pct ≥ OVERNIGHT_SAFETY_K · σ (σ = daily log-return vol, from the same 24h
# series as the store screen). Also reject items already drifting DOWN (μ below OVERNIGHT_MIN_DRIFT) and
# anything that fails the long-baseline pump gate. σ unmeasurable → rejected (can't verify safety).
OVERNIGHT_SAFETY_K = float(os.environ.get("OSRS_FLIPPER_OVERNIGHT_SAFETY_K", 2.0))
OVERNIGHT_MIN_DRIFT = float(os.environ.get("OSRS_FLIPPER_OVERNIGHT_MIN_DRIFT", -0.005))  # ≈ −0.5%/day tolerance
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
# Manual "I'm online now" override (`active` command): force the day/fast-flip regime for this many
# minutes regardless of the clock, then auto-revert. For when you're flipping outside AWAKE hours and
# want cyclable flips instead of the overnight/patient plan. Interactive `go` only — the Discord board
# stays on the clock.
ACTIVE_OVERRIDE_MIN = float(os.environ.get("OSRS_FLIPPER_ACTIVE_OVERRIDE_MIN", 60))

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

# --- Progress chart / projection (see progress.py) ---------------------------
# Idle days must not dilute the compounding rate: inter-trade gaps longer than this are clamped (you
# weren't trading, nothing was growing), so growth is measured per ACTIVE day, not per wall-clock day.
PROGRESS_IDLE_GAP_MAX_H = float(os.environ.get("OSRS_FLIPPER_PROGRESS_IDLE_GAP_MAX_H", 24))
# Monte-Carlo projection: path count, a fixed seed (so the chart is stable between renders), the daily-
# return volatility used when there's too little history to measure it (as a fraction of the fitted
# rate), the net-worth pivot where drift starts decaying (the S-curve — liquidity/buy-limits bite at
# scale), and the forward horizon in active days.
MC_PATHS = int(os.environ.get("OSRS_FLIPPER_MC_PATHS", 2000))
MC_SEED = int(os.environ.get("OSRS_FLIPPER_MC_SEED", 7))
MC_DEFAULT_CV = float(os.environ.get("OSRS_FLIPPER_MC_DEFAULT_CV", 0.6))
MC_DECAY_PIVOT = int(os.environ.get("OSRS_FLIPPER_MC_DECAY_PIVOT", 20_000_000))
MC_HORIZON_DAYS = int(os.environ.get("OSRS_FLIPPER_MC_HORIZON_DAYS", 180))

# --- Wealth-cap glide + store-of-value screen (see wealth.py, store.py) -------
# OSRS caps a coin stack at 2^31-1. Platinum tokens (1 = 1,000 gp) hold the overflow, so the ABSOLUTE
# "can't hold more liquid value" ceiling is max coins + max platinum ≈ 2.15T. But that's unreachable —
# measuring the glide against it leaves the feature dormant forever. So MAX_LIQUID_GP defaults to plain
# max coins (2.147B): a reachable milestone where sitting on a maxed coin stack (rather than juggling
# platinum) is the natural cue to start banking value in assets. The glide then begins at 70% (~1.5B)
# and hits full at the cap. To instead treat platinum as usable headroom and pivot only near the true
# ceiling, set OSRS_FLIPPER_MAX_LIQUID_GP=2149631130647 (= MAX_COINS * 1001).
MAX_COINS = 2**31 - 1  # 2,147,483,647 — hard coin-stack cap
MAX_LIQUID_GP = int(os.environ.get("OSRS_FLIPPER_MAX_LIQUID_GP", MAX_COINS))
# Glide: fraction of the cap at which we START tilting new capital from flips into stores-of-value,
# ramping linearly to full tilt AT the cap. 0.70 = begin at 70% of MAX_LIQUID_GP.
CAP_GLIDE_START_FRAC = float(os.environ.get("OSRS_FLIPPER_CAP_GLIDE_START_FRAC", 0.70))
# How hard a full glide (net worth at the cap) boosts store candidates over flips in the `go` plan.
# store window-gp is multiplied by (1 + STORE_GLIDE_GAIN * glide); at glide=1 stores dominate.
STORE_GLIDE_GAIN = float(os.environ.get("OSRS_FLIPPER_STORE_GLIDE_GAIN", 4.0))

# Store-of-value screen: rank stable, deep, appreciating assets to park capital in near the cap. Not
# spread capture — a quant risk/return view. Universe is bounded (high price + liquid) to cap the
# per-item timeseries calls; drift μ and vol σ come from log-returns over STORE_LOOKBACK 24h bars.
STORE_MIN_PRICE = int(os.environ.get("OSRS_FLIPPER_STORE_MIN_PRICE", 100_000))   # park value in few units
STORE_MIN_TURNOVER = int(os.environ.get("OSRS_FLIPPER_STORE_MIN_TURNOVER", 5_000_000))  # gp/h depth to enter/exit size
STORE_TIMESTEP = os.environ.get("OSRS_FLIPPER_STORE_TIMESTEP", "24h")            # bar size for the return series
STORE_LOOKBACK = int(os.environ.get("OSRS_FLIPPER_STORE_LOOKBACK", 60))          # bars of history (~8wk of 24h)
STORE_CANDIDATES = int(os.environ.get("OSRS_FLIPPER_STORE_CANDIDATES", 40))      # max items to pull timeseries for
STORE_MAX_VOL = float(os.environ.get("OSRS_FLIPPER_STORE_MAX_VOL", 0.10))        # reject σ (daily) above this
# Mean-variance risk aversion λ in the utility U = μ − 0.5·λ·σ² (daily). Cash is the baseline (U=0, no
# risk, no nominal growth); a store must beat it — U>0 — to be worth holding, UNLESS the glide forces
# conversion because excess cash above the cap simply can't be held. Higher λ = more risk-averse.
STORE_RISK_AVERSION = float(os.environ.get("OSRS_FLIPPER_STORE_RISK_AVERSION", 8.0))
# Hold horizon (days) used to turn a store's daily drift μ into an expected-appreciation gp figure the
# `go` plan can rank against flips. A store's per-slot value ≈ capital · μ · this, then glide-boosted.
STORE_HOLD_DAYS = int(os.environ.get("OSRS_FLIPPER_STORE_HOLD_DAYS", 30))

# --- Output ------------------------------------------------------------------
DISCORD_WEBHOOK_URL = os.environ.get("OSRS_FLIPPER_DISCORD_WEBHOOK")  # optional (one-way, one channel)
# Bot push (posts AS your bot, can edit a live status message): set both. Preferred over the webhook.
DISCORD_BOT_TOKEN = os.environ.get("OSRS_FLIPPER_DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = os.environ.get("OSRS_FLIPPER_DISCORD_CHANNEL_ID")
# Background attention monitor: how often to poll RuneLite for offers that need you
# (filled → collect, margin gone, stale). Pushes to Discord only on NEW events (de-duped).
ALERT_POLL_S = int(os.environ.get("OSRS_FLIPPER_ALERT_POLL_S", 60))
