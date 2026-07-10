# osrs-flipper

A flip-finder, portfolio manager, and strategy backtester for the Old School RuneScape
Grand Exchange. It ranks live flips, plans how to deploy your whole bankroll across your
GE slots, tracks realised/unrealised P&L, learns from your actual fills, and backtests
trading strategies — all against the free, community-run
[OSRS Wiki real-time prices API](https://prices.runescape.wiki/api/v1/osrs).

> **You execute every trade manually.** This is a market-analysis tool, **not a bot** — it
> never touches or automates the game client. Automating RuneScape violates Jagex's EULA and
> gets accounts permanently banned (account + items confiscated), which also makes zero GP.
> Everything here reads *public* price data and *observes* your account state through an opt-in
> RuneLite plugin export; you place every buy and sell offer yourself. Observe state, execute by
> hand — that boundary (ADR 0001) is deliberate and load-bearing.

---

## Table of contents

- [Why it tells the truth](#why-it-tells-the-truth)
- [Quickstart](#quickstart)
- [The daily driver: `trade` and `go`](#the-daily-driver-trade-and-go)
  - [The `go` dashboard](#the-go-dashboard)
  - [Terminal command reference](#terminal-command-reference)
- [The one-shot CLI](#the-one-shot-cli)
- [How the ranking works (why the numbers are honest)](#how-the-ranking-works)
  - [The conservative fill model](#the-conservative-fill-model)
  - [The ranking formula](#the-ranking-formula)
  - [The deep re-price: `quote` & the efficient frontier](#the-deep-re-price)
  - [Portfolio construction](#portfolio-construction)
  - [GE tax](#ge-tax)
  - [Anomaly & regret](#anomaly--regret)
- [Data, persistence & learning](#data-persistence--learning)
  - [Data source](#data-source)
  - [Persistence (DuckDB)](#persistence-duckdb)
  - [The trade journal](#the-trade-journal)
  - [Self-calibration](#self-calibration)
  - [RuneLite integration](#runelite-integration)
  - [Cross-machine sync](#cross-machine-sync)
- [Wealth, treasury & progress](#wealth-treasury--progress)
- [Backtesting](#backtesting)
- [Building price history](#building-price-history)
- [Discord alerts](#discord-alerts)
- [Configuration reference](#configuration-reference)
- [Project layout](#project-layout)
- [Testing](#testing)
- [Honest caveats](#honest-caveats)

---

## Why it tells the truth

Most GE tools overstate returns 3–10× because they assume every buy fills at the instant-sell
price, every sell fills at the instant-buy price, and both legs complete instantly and in full.
That hands you the entire spread for free — a fantasy. This tool exists to strip that fantasy out
and report a number you could **actually realise**. A flip is two *limit orders*, not a market
trade, and it is priced that way everywhere: the live scanner, the deep quote solver, and the
backtest replay all share one conservative fill model (`osrs_flipper/fills.py`), so they can never
disagree.

The conservative mechanisms, in one place (details in [How the ranking works](#how-the-ranking-works)):

- **Spread haircut (β)** — you transact *inside* the spread, not at its favourable extreme. With
  integer-gp rounding this collapses penny-spread "opportunities" to nothing.
- **Partial-fill capture (α, γ)** — per window you capture only a share of the contra-side volume,
  never 100%.
- **Fill probability** — P(both legs fill) within a horizon, from empirical fill rates.
- **Market impact (K)** — taking a large share of the flow walks the price against you even when it fills.
- **Hung-leg / mark-to-market bailout** — the state that actually hurts is a filled buy with a
  *stuck* sell; unsold inventory is liquidated at a loss and booked, never hidden.
- **Liquidity / staleness gate** — stale, null-priced, wide-spread-on-thin-volume "ghost" items are
  excluded entirely.

Every backtest reports a **β sensitivity band** (β=0 free-spread fantasy ceiling → 0.25 honest
default → 0.5 pessimistic floor) alongside completion rate, win rate, ROI, gp/day, and drawdown.
And crucially, the model's priors **self-calibrate** from your real fills over time (β, fill-rate,
fill-time, market impact), shrinking toward what your account actually experiences.

---

## Quickstart

```bash
uv venv && . .venv/bin/activate          # or: python3 -m venv .venv && . .venv/bin/activate
uv pip install -e ".[dev]"               # or: pip install -e ".[dev]"

# Required by the Wiki API's User-Agent policy (it blocks default UAs):
export OSRS_FLIPPER_CONTACT="you@example.com or @you on Discord"

osrs-flipper trade                       # launch the interactive terminal — the daily driver
```

Requires Python ≥ 3.11. Dependencies: `requests`, `pandas`, `duckdb` (and `matplotlib` is used by
the progress chart / plot script). The only mandatory environment variable is
`OSRS_FLIPPER_CONTACT`; everything else has a sensible default (see the
[Configuration reference](#configuration-reference)).

Common optional overrides:

```bash
export OSRS_FLIPPER_BANKROLL=200000            # starting cash cap for suggested quantities
export OSRS_FLIPPER_MEMBERS=0                  # F2P only (default is members: full market, 8 GE slots)
export OSRS_FLIPPER_DISCORD_BOT_TOKEN=...       # push the dashboard to your phone via Discord
export OSRS_FLIPPER_DISCORD_CHANNEL_ID=...
```

> **Note on account type.** `MEMBERS` defaults to `1` (members: full item market, **8** GE slots).
> Set `OSRS_FLIPPER_MEMBERS=0` for a free-to-play account (F2P-tradeable items only, **3** GE slots).
> In F2P the terminal also tracks how close your bankroll is to a **bond** — the F2P → members milestone.

---

## The daily driver: `trade` and `go`

```bash
osrs-flipper trade
```

launches a self-contained REPL — **no LLM, no token cost**. All state (positions, cash, ledger,
calibration, recommendations, blacklist) persists in DuckDB across sessions. On startup it reports
the active data source, auto-syncs any completed fills from RuneLite, and — if a Discord channel is
configured — turns on the background dashboard push.

**`go` is the one command that does everything.** Press **Enter** on an empty line and it runs `go`.
Everything else is occasional or maintenance. This is the golden rule of the tool: the daily
workflow funnels through `go`, not through a pile of separate commands.

### The `go` dashboard

Each `go` runs, in order:

1. **Sync** — pull live coins and active GE offers from RuneLite; durably remember each offer's
   placement age so a client restart doesn't reset staleness tracking.
2. **Regime detection** — day / winding-down / overnight, from the clock (`AWAKE_START`/`AWAKE_END`/
   `NIGHT_SWITCH_H`) or a manual `active` override. Day → fast flips dominate; near bedtime and
   overnight → patient gear/sets/decants take priority, since each only fills once while you sleep.
3. **Header** — cash, net worth (cash + held stock marked-to-market + gp tied in offers), held count,
   free/total GE slots, % to a bond (F2P), the regime tag, a "🏦 % to cap" note as net worth nears the
   coin cap, and an adaptive-objective note once your realised Sharpe lifts variance-aversion off its floor.
4. **Macro line** — a bond-price gold-inflation gauge (`inflating` / `deflating` / `stable`).
5. **Holdings split** — bank (sellable) vs listed-in-GE vs buying.
6. **ACTIVE OFFERS** — every live slot with a verdict (`collect` / `margin gone` / `stale` / `slow` /
   `on track` / `open`), elapsed time, ETA, and fill %. Verdicts are refined against a fresh quote so a
   genuinely-fine-but-slow offer isn't nagged as cancel-worthy, and a flagged SELL is loss-aware — it
   won't tell you to chase price below your break-even unless a cut is actually justified.
7. **Bank-a-partial** — a 💰 line when a partially-filled, flagged, or buy-limit-capped buy is worth
   banking now (sell the filled units, free the slot).
8. **REBALANCE** — flags a stuck early-stage buy when the plan has something ≥`SWAP_RATIO`× better to
   deploy into, naming the exact swap.
9. **SELL plan / HOLDING for the bounce** — a recommended sell price for every held item without a live
   offer (never below break-even); genuinely-underwater-but-likely-to-bounce holdings are routed to a
   "hold, don't list" bucket instead of sold into a dip.
10. **DECANT** — exit advice for held low-dose potions (members): decant up to (4), then sell.
11. **Unified BUY plan** — fast flips, patient gear, GE set arbitrage, and potion decants are all ranked
    on **one currency** (expected gp over the current time window) and the best use of each free slot is
    printed under "BEST FOR YOUR N FREE SLOT(S)". Optimistic best-case (β=0) gear/set candidates get an EV
    haircut (`PATIENT_EV_CONFIDENCE`) so they can't crowd out an honestly-priced flip on optimism alone.
12. **NEXT** — a single synthesised instruction: collect finished offers first, then re-price flagged ones,
    then list sells / place buys, then decant, else tell you how long to wait.

Every recommendation `go` makes is logged to the journal, so later you can grade whether acting on
(or pulling) it was the right call (`recs`, `analyze`).

### Terminal command reference

`go` funnels the daily loop; the rest are on demand. `help` shows the daily/occasional set; `help all`
adds the maintenance commands.

**Daily**

| Command | What it does |
|---|---|
| `go` *(or Enter)* | The everything dashboard (above) |
| `active [min\|off]` | Force the day/fast-flip regime for a while (default `ACTIVE_OVERRIDE_MIN`), for flipping outside your awake hours |
| `quote <item> [qty]` | Solve the gp/hour-optimal buy/sell prices for an item; logs the prediction for calibration |
| `why <item>` | Explain a price: live vs 1d/2wk/3mo/30d norms, volume z-score, slope, falling-knife / pump phase, 5m margin-durability |
| `overnight [item]` | No arg → diversified ~8h buys across all free slots. With an item → one big cushioned buy sized for ~2 buy-limit windows |
| `gear [n\|all]` | Big-ticket, low-frequency items at their full spread (patient, best-case). `gear all` ignores cash (aspirational) |
| `store [n]` | Stores of value to park capital in near the cash cap — risk/return ranked (μ, σ, Sharpe, utility vs holding cash) |

**Occasional**

| Command | What it does |
|---|---|
| `scan [n] [online\|balanced\|offline]` | Ranked raw, unallocated flips |
| `sets [n] [roi] [all]` | GE set arbitrage: assemble pieces→set or break set→pieces, net of tax |
| `decant [n] [roi] [all]` | Buy (1)/(2)/(3)-dose potions → decant up to (4) at Bob Barter → sell, net of tax (members). `decant log <potion(dose)> <count>` records an executed decant |
| `port [free_slots]` | Recommended allocation across your free slots (the standalone version of `go`'s buy plan) |
| `sellquote <item> [qty]` | Sell-price tradeoff curve (fill time vs profit) for held stock |
| `anomaly` | Market-wide price dislocations on abnormal volume (pumps to avoid, over-dumps to buy) |
| `pnl` | Cash, gp tied in offers, stock value, equity, realised P&L, % of a bond |
| `progress` | Net-worth chart + Monte-Carlo projection to 10M/100M, fit on active (idle-excluded) trading time |
| `pos` | Open positions + unrealised P&L vs the live bid |
| `inv` | Holdings: in your bag vs listed in the GE |
| `recent [n]` | Recent trades |
| `recover` | For each underwater holding: bounce (hold) vs re-rating (cut) |

**Maintenance** (hidden from `help`; shown by `help all`)

| Command | What it does |
|---|---|
| `buy <item> <qty> <price>` | Log a buy fill made off-device |
| `sell <item> <qty> <price>` | Log a sell fill made off-device (applies GE tax) |
| `hold <item> <qty> [avg]` | Track a holding acquired elsewhere, no cash spent |
| `forget <item>` | Untrack a holding traded elsewhere (no sale recorded) |
| `audit` | Full buy/sell + bag reconciliation, per-item P&L, flags any bag/history discrepancy |
| `calibrate [backfill]` | Report empirical β / fill-rate / fill-time calibration from your real attempts. `calibrate backfill` seeds the fill-time learner from RuneLite history |
| `preds [n]` | Logged model predictions (calibration debug) |
| `alerts [on\|off\|test]` | Toggle / status / test the background Discord push |
| `blacklist [<item>\|rm <item>]` | Never recommend an item again; persisted, applies everywhere |
| `sync [export\|import]` | Merge the calibration learner + blacklist across machines via flat files in `sync/` |
| `recs` | Recommendation ledger: logged vs acted vs pulled, plus a pull-quality scorecard |
| `update` | `git pull --ff-only` then reload |
| `reload` | Re-exec the terminal in place (state persists) to pick up new code |
| `help [all]` · `quit`/`exit`/`q` | Help / leave |

> Every command auto-syncs first (import fills, reconcile the bag, detect decants, refresh cash and
> calibration), so manual reconciliation is rarely needed — you can place orders directly in-game and
> the terminal picks them up.

---

## The one-shot CLI

For scripting or a quick look without the REPL:

```bash
osrs-flipper scan --top 20                        # rank live flips you can afford
osrs-flipper scan --members --mode online --top 20
osrs-flipper portfolio                            # diversified allocation for your free slots
osrs-flipper quote "Grapes" --qty 2800            # gp/hour-optimal buy/sell prices + frontier
osrs-flipper backtest mean_reversion --timestep 24h --top 25
osrs-flipper bootstrap --timestep 24h --top 50    # seed history into DuckDB
osrs-flipper collect                              # one snapshot; put on a 5-min cron for 5m history
osrs-flipper trade                                # the interactive terminal
```

Selected flags:

- `scan`: `--top`, `--bankroll`, `--members`, `--include-suspect`, `--mode {online,balanced,offline}`,
  `--min-gp`, `--no-persistence` (skip the deep spread-stability check — faster), `--discord`.
- `portfolio`: `--bankroll` (default: journal balance), `--slots` (default: `GE_SLOTS` − held), `--min-gp`, `--members`.
- `quote`: `item`, `--qty`, `--bankroll`, `--capture` (α — share of volume you capture), `--timestep {5m,1h,6h,24h}`.
- `backtest`: `strategy {mean_reversion,momentum,margin_flip}`, `--timestep`, `--top`, `--members`.
- `bootstrap`: `--timestep`, `--top`, `--members`.

---

## How the ranking works

### The conservative fill model

`osrs_flipper/fills.py` provides the primitives shared by the scanner, the quote solver, and the
backtest, so a flip is priced identically everywhere.

- **Spread haircut (β).** `haircut_prices` posts *inside* the spread: buy at `avg_low + β·spread`, sell
  at `avg_high − β·spread`, rounded to whole gp. `BETA` defaults to **0.25** (a quarter-spread haircut per
  side). On a 1gp spread this rounds the legs together and the margin collapses to ~0 — deliberately
  killing penny-spread traps that look great in ROI% but never fill. A separate `PATIENT_BETA = 0.0`
  (full spread, no haircut) is used for patient gear/sets — explicitly optimistic, best-case pricing.
  β self-calibrates from your real fills (per liquidity bucket) and shrinks toward the 0.25 prior.
- **Partial-fill capture (α, γ).** `capacity_units` sizes a live position as the *minimum* of the legal
  GE buy limit, a passive `α`-share of market volume (`ALPHA = 0.10`), and what your bankroll affords.
  `fill_units` caps per-bar fills at `γ`·contra-volume (`GAMMA = 0.15`) in the backtest. A liquidity
  floor lets big-ticket gear (few trades/hour) still size up — the floor changes *how many* units to
  attempt, not *whether* to; the ETA does that.
- **Fill probability.** `leg_fill_prob` is the fraction of your target that an α-share of the contra-side
  rate clears within the horizon; `completion_probability` multiplies the buy and sell legs → P(both fill).
  The cheap snapshot ranking and the deep quote share this exact function, so the fast path's ordering
  tracks what the authoritative re-price finds.
- **Market impact (K).** `market_impact_mult = 1/(1+K·p)` (participation `p` = your size ÷ contra-volume),
  floored at `IMPACT_FLOOR = 0.5`, `IMPACT_K = 1.0` by default. Near 1.0 for a small bankroll-bound stack;
  bites hard once a position is a meaningful fraction of the flow — the regime a growing bankroll hits. K
  self-calibrates once there are enough high- and low-participation samples.
- **Hung-leg / bailout.** Scoring "doesn't complete" as zero is wrong — what actually hurts is the *buy*
  filling and the *sell* hanging, trapping capital you must grind out at a loss. `hung_leg_mult` penalises a
  shaky sell leg on a thin margin hard (exactly the combination a trapped buy wipes out) and barely touches
  a fat-margin flip with a reliable sell. Horizon-aware, so a patient overnight quote is penalised less than
  a rushed online one. Applied to active flips only.
- **Liquidity / staleness gate** (`features.py`). Requires both live prices, a trade within
  `STALENESS_MAX_S` (1h), and enough liquidity — either raw unit volume (`V_MIN_1H = 500`) *or* enough gp
  turnover (`TURNOVER_MIN_1H = 1M gp/h`) for the "few units, each huge" gear regime. A `suspect` flag
  separately catches wide-spread-on-thin-volume, wide-spread-not-backed-by-volume, and
  wide-spread-across-a-stale-leg — all illiquidity artefacts masquerading as spread.

### The ranking formula

`scanner.scan()` scores every candidate roughly as:

```
score = (margin_after_tax × capacity × P(both legs fill) × persistence) / fill_eta_h^time_weight
```

then layers multiplicative tilts, sorts descending, and gates hard.

- **Modes.** `online` (time-weight 1.0, 0.5h horizon) scores per real-time-hour — a fast flip beats a fat
  slow one, and prices assume you queue-jump (bid+1 / ask−1); penny spreads correctly go ≤0 and sink.
  `offline` (weight 0.0, 8h horizon) treats wall-clock as free overnight — only the per-cycle haul matters.
  `balanced` splits the difference (2h horizon).
- **Auto-applied learning.** EV is multiplied by the measured fill-rate correction (bucketed by turnover),
  a per-item realised-edge EWMA (`edge_mult` — proven losers down-weighted, floored at `EDGE_FLOOR = 0.3`,
  never fully banned), and the calibrated market-impact haircut.
- **Stack-aware ROI tilt.** A small, capital-bound stack compounds fastest chasing margin%
  (`ROI_WEIGHT_SMALL_STACK = 0.6`); a large stack can't push size through shallow high-ROI items and should
  chase raw throughput. The exponent log-interpolates over net worth from `ROI_STACK_LO` (300k) to
  `ROI_STACK_HI` (20M).
- **Risk-aware tilts (on the *ranking*, never the displayed gp).** Variance aversion multiplies by
  `P(complete)^λ`; the crowding tilt rewards uncrowded niches and penalises bot-raced staples around a 50M
  gp/h pivot. This encodes the project's core thesis: **the edge is uncrowded items** — on high-turnover
  staples every bot races, you're the slowest participant in the queue and often someone else's exit liquidity.
- **Optimizer's-curse shrinkage.** Ranking by estimated EV surfaces the *most upward-biased* estimates at
  the top; low-reliability picks get pulled toward the median, reliable ones keep their edge.
- **Hard gates.** Members filter, `tradeable & margin > 0 & capacity > 0`, suspect exclusion, an absolute
  `MIN_NET_MARGIN = 2` gp floor (a 1gp margin on a 2gp item reads as 50% ROI but never fills), and — on the
  deep path — spread persistence (≥60% of recent bars show a positive spread) plus an anomaly buy-gate, so
  the scanner literally cannot recommend a buy its own `why` explanation would warn against.
- **Never-recommend blacklist** applied at the source, and a **fast margin-decay guard** that re-checks the
  last hour at 5m resolution (a popular potion can pass the 1h check while its margin evaporates minutes
  after you list).

### The deep re-price

`quote.optimal_quote()` is the ground-truth solver the scanner's deep path defers to — `quote <item>` shows
the exact numbers `scan` displays. It builds empirical buy/sell rate curves from recent `/timeseries` bars,
grid-searches `(buy, sell)` price pairs, and at each computes `net = post_tax(sell) − buy`,
`p_round = P(buy fills)·P(sell fills)`, `EV = qty·net·p_round`, picking the EV-maximiser. `_frontier()`
returns the full margin-vs-fill-probability tradeoff (one row per achievable margin), not just the optimum.

Safety: it rejects a crossed book (`low > high`) or a live mid diverging from the recent median by more than
`PRICE_DIVERGENCE_MAX` (15%), and clamps the buy floor up to the live bid so it can never quote a price that
would just sit out of market. In **patient/overnight** mode it doesn't take the EV-max price — it takes the
*lowest bid that still fills within the window*, letting the bid drop to where sellers have actually dumped:
bid low, fill by morning, fatter margin.

An **adaptive objective** (`objective.py`) ramps variance aversion from its floor toward `VARIANCE_AVERSION_MAX`
as your realised per-active-day Sharpe climbs from `OBJ_SHARPE_LOW` (1.0) to `OBJ_SHARPE_HIGH` (3.0) — the
hypothesis being that a rising Sharpe signals a more crowded/efficient market, so chase steadier flips as easy
edge dries up. Stated in the code as a hypothesis, not a law, and fully switchable off (`VARIANCE_AVERSION_MAX=0`).

### Portfolio construction

`scanner.build_portfolio()` ranks every free GE slot on the balanced horizon as one diversified flip, in two tiers:

- **ACTIVE** — must clear the queue-jumped spread (an active flip assumes you refuse to wait).
- **HOLD** — soaks idle cash into inventory once active slots are full, at a stricter `HOLD_MIN_MARGIN = 0.03`,
  sized by a *realisable* accumulation cap so the plan never suggests hoarding more than the market will clear.

Diversification: each pick's item is removed from the pool before the next slot is filled, so no doubling up.
Held positions and rolling-4h buy-limit usage feed in so the plan respects limits and doesn't re-recommend what
you hold. A self-calibrating **slot-worth floor** sets the minimum profit worth committing a slot
(`fair-share capital × achievable ROI × λ`) — a bigger account or fatter market raises the bar automatically.
Capital is allocated by efficiency (expected return per gp), concentrated on the best picks but capped at
`MAX_ALLOC_FRAC = 0.5` per position, with a second pass topping up under-filled picks so no gold sits idle.

`planner.py` sits above this and ranks *heterogeneous* candidates — fast flips, patient gear, GE sets, decants —
on one common currency (expected gp per slot over the window), so `go` picks the single best use of a slot across
totally different trade shapes. **GE set combos** (`combinations.py` / `combos.py`) price both directions —
ASSEMBLE (buy parts, sell the set; tax hits the output once) and BREAK (buy the set, sell the parts; tax hits every
part) — and pick whichever wins, with every leg priced through the same gates as a single item.

### GE tax

Seller-only, **2%** of the sale price (was **1%** before **2025-05-29**), floored to whole gp, capped at
5,000,000 gp per item, and waived below 50 gp. A fixed set of items plus bonds are fully exempt. Every function
takes an `on_date`, so backtests replaying pre-2025-05-29 data apply the rate that was actually in force —
historical results stay correct. `breakeven_sell` finds the smallest sell price whose post-tax proceeds cover a
cost basis, used to floor sell recommendations so the tool never suggests dumping under cost.

### Anomaly & regret

`anomaly.py` detects items whose live price has dislocated from its baseline *on abnormal volume* — the
combination that distinguishes a real move from noise. It classifies phase: `PUMP↑` (don't chase; sell into it
if you hold), `FADE↓` (post-pump; don't catch the knife), `RECOVER↑` (dumped and turning back up — the one
low-risk exploit: revert-buy toward baseline), `DUMP↓` (still falling; wait). `is_buyable()` is the single rule
shared by both the human `why` explanation and the scanner's buy gate.

`regret.py` is retrospective: after a recommendation you never acted on gets pulled, was pulling it the right
call? `good_pull` if the spread then died; `regret` if it held up (a signal that some gate is too trigger-happy).
It's a *rate to watch* across many pulls, not a verdict on any single one.

---

## Data, persistence & learning

### Data source

The tool reads the free, unauthenticated **OSRS Wiki real-time prices API**
(`https://prices.runescape.wiki/api/v1/osrs`), wrapping four endpoints:

| Endpoint | Returns |
|---|---|
| `/latest` | Instant-buy/instant-sell price + timestamps for every item |
| `/5m`, `/1h` | Volume-weighted avg high/low + per-side volume; optional historical `?timestamp=` |
| `/mapping` | Static metadata: id, name, GE buy limit, value, high-alch, members flag |
| `/timeseries?id=&timestep=` | Up to 365 historical bars for one item at 5m/1h/6h/24h |

The bulk endpoints return every item in one call, so per-item requests happen only for `/timeseries` (used
sparingly, capped by candidate counts). The Wiki blocks default `requests`/`curl` User-Agents, so a descriptive
UA carrying your `OSRS_FLIPPER_CONTACT` is sent on every request (fast-fail retry, `HTTP_TIMEOUT`-bounded so a
hung API can't freeze the REPL). A small disk **TTL cache** (`data/.cache/`) matches each endpoint's true change
rate (mapping 6h, latest 30s, 5m 60s, 1h 120s, timeseries 5min); it never fakes freshness — staleness gates read
each item's own trade timestamps inside the payload. Disable with `OSRS_FLIPPER_NO_CACHE=1`.

### Persistence (DuckDB)

A single DuckDB file at `data/osrs.duckdb` backs both the market-data store (`store.py`: `mapping`, `prices`,
`latest`, `timeseries`) and the trade journal (`journal.py`). All market writes are idempotent
(`INSERT OR REPLACE` on the natural key), so re-running a collector or bootstrap never duplicates rows. This is a
local warehouse of price history that backtests and deep-checks draw on without re-hitting the API.

### The trade journal

`journal.py` is the persistent portfolio ledger in the same DuckDB file:

- **`positions`** — held item, qty, avg cost; reconcilable to the live bag (authoritative for quantity) or by
  replaying RuneLite's full offer history.
- **`ledger`** — append-only realised history (BUY/SELL/DECANT with tax, cash delta, realised P&L); a DECANT row
  is cash/P&L-neutral, only moving cost basis between doses.
- **`attempts`** — the calibration backbone: every order you placed, its decision-time market snapshot, the
  model's prediction (p_fill, ETA, EV), and its outcome (filled qty, fill price, status).
- **`recommendations`** — the rec ledger: every recommendation episode, whether you acted (linked to its attempt)
  or it was pulled, and a later good-pull-vs-regret evaluation. Hysteresis stops 60s rank flutter from churning.
- **`offer_events`**, **`predictions`**, **`blacklist`**, **`offer_seen`** — lifecycle timeline, logged EV
  estimates, never-recommend list, and durable per-slot offer-age tracking across client restarts.

Buy-limit usage is tracked over a rolling 4h window, preferring RuneLite's own counter when available.

### Self-calibration

Calibration is read-only analysis: nothing is written back to config — the EV code calls the calibrated values at
runtime, refreshed every `CALIBRATE_EVERY_TRADES` (default **10**) resolved attempts and on demand via `calibrate`.
Four things learn from your real fills:

1. **β (spread haircut)** — how far into the spread fills actually land, per liquidity bucket.
2. **Fill-rate correction** — actual vs predicted fill fraction (includes *expired* attempts as 0, so it isn't
   survivorship-biased toward optimism).
3. **Fill-time (ETA)** — realised vs predicted fill hours, bucketed by price × volume band; handles never-filled
   (right-censored) attempts.
4. **Market-impact slope K** — fill quality at high vs low participation; dormant (uses the prior) until enough
   samples exist in both buckets.

Everything **shrinks toward its prior** with weight `n/(n+k)` (k=20) — with few samples you barely move off the
prior; the philosophy is "pessimistic-wrong beats optimistic-wrong," so a handful of lucky fills can't swing the
model. The market-impact floor and the hung-leg cost remain fixed priors with no calibration path yet.

### RuneLite integration

`runelite.py` / `datasource.py` are **read-only** parsers for opt-in RuneLite plugin exports (ADR 0001: observe
state, execute by hand). Two backends behind one interface, selected by availability:

1. **Flip Exporter** (preferred) — one clean source: cash, noted-resolved holdings, real offer prices,
   placement times, and deduped completed/cancelled fill history.
2. **Legacy** — Local Data Exporter (cash/holdings/offers) + Flipping Utilities (fills, buy-limit counter).

Both expose the same shape (`cash()`, `holdings()`, `active_offers()`, `all_fills()`, `limit_used()`, …) so the
rest of the tool never branches on provenance. Design guards: all buy/sell logic keys off the plugin's explicit
`isBuy` boolean (never string-matching state, since `"BUY"` is a substring of neither `"BOUGHT"` nor `"SOLD"`),
and **schema-drift detection** warns loudly rather than failing open (a blind free-slot count would let the tool
over-recommend). In-game **decants** are detected by conservation and re-based via `record_decant`, so cost basis
follows a (3)→(4) decant instead of vanishing with the consumed dose.

### Cross-machine sync

No server. Each install has a stable device id. `sync export` dumps this device's own resolved attempts + blacklist
to `sync/<device>.json` (this directory is *not* gitignored — you commit it to carry learning between machines);
`sync import` merges every other device's file with namespaced, collision-proof, idempotent ids, then recalibrates
over the union. The bare `sync` command does both.

---

## Wealth, treasury & progress

As a stack grows, spread-flipping stops being the whole game — three modules handle the endgame:

- **`macro.py` — bond signal.** Reads the Old School Bond's gp price and its 30-day drift/vol as a gold-inflation
  gauge (rising price ⇒ gp losing value ⇒ favour assets over cash). Surfaced as the one-line macro header in `go`.
- **`treasury.py` — store-of-value screen.** Near the cash cap you *can't* keep growing liquid coins, so capital
  has to sit in held assets. This ranks candidates by a quant risk/return call, not just "low volatility": daily
  drift μ, risk σ, Sharpe μ/σ, and mean-variance utility `U = μ − ½·λ·σ²`. Cash is the baseline (U=0); a store must
  beat that on merit, gated by deep both-side liquidity so you can enter *and* exit size without moving the price.
  (`macro.py` reuses its return-stats helpers.)
- **`wealth.py` — cap glide.** A coin stack caps at 2³¹−1 (~2.147B); platinum tokens hold the overflow. As net
  worth approaches `MAX_LIQUID_GP`, `glide_factor` ramps 0→1 and `go` uses it to progressively tilt deployment from
  flips toward stores of value.

**`progress.py`** renders the two-panel net-worth chart behind the `progress` command: realised net-worth-at-cost
as a step function from the ledger plus a live mark-to-market marker, then your climb on an **active-day** x-axis
(idle gaps over 24h are clamped, so multi-day absences don't dilute the measured growth rate) with a Monte-Carlo
forward fan. The simulation runs `MC_PATHS` (2000) daily-compounding paths under a fixed seed (so the chart doesn't
jitter), with drift decaying past `MC_DECAY_PIVOT` (20M — liquidity and buy limits cap how far a flat %/day
extrapolates), and reports per-milestone (10M/100M) probability and median/p10/p90 first-crossing day within
`MC_HORIZON_DAYS` (180).

---

## Backtesting

```bash
osrs-flipper backtest mean_reversion --timestep 24h --top 25
```

`backtest/engine.py` replays a strategy against per-item history. It builds its universe from the item mapping
(F2P unless `--members`), ranks by 1h **binding** volume (`min(highVol, lowVol)` — whichever side is thinner is
what caps how much you can trade), takes the top N, and pulls `/timeseries` at the chosen `--timestep`
(5m/1h/6h/24h). Coarser timesteps buy a longer look-back but blur intrabar action.

The simulation is honest by construction: a strategy may only read bars *strictly before* the current one, while
fills happen *at* the current bar (no same-bar look-ahead); fills go through the shared conservative model; an
**adverse-selection gate** only fills a resting order when price moved flat-or-against you; positions force-exit at
`max_hold` bars; and any unsold units are marked-to-market and booked as a real bailout loss.

Every run executes three times at **β ∈ {0.0, 0.25, 0.5}** and prints the band side by side — β=0 is the
free-spread fantasy ceiling, 0.25 the honest default, 0.5 the pessimistic floor. Per β it reports: attempted /
filled / completed counts, buy-fill-rate, completion-rate, win-rate, total P&L, ROI%, gp/day, gp/hour,
gp/active-minute, hold-bar stats, and max drawdown. Zero fills short-circuits to an explicit "no executable trades"
note rather than dividing by zero.

**Strategies and their honest verdicts:**

| Strategy | Signal | Verdict |
|---|---|---|
| `mean_reversion` | Buy when `avg_low` is z-score `≤ −Z_ENTRY` (2.0) below its rolling mean; exit on recovery to `−Z_EXIT` (0.5). Signals off the buy-side price, not the mid, so spread width doesn't contaminate the signal | **Most robust.** The closest thing to a real, data-supported edge; degrades gracefully on flat/illiquid history |
| `momentum` | Enter on a price breakout above the prior `range_bars` high *and* a volume-z spike; exit when price falls back through the breakout level | **Thin but real.** The price+volume gate filters noise well, but breakouts are rare so samples are small and the edge is regime-dependent |
| `margin_flip` | Enter whenever the relative spread clears `MIN_MARGIN_PCT` (2%, enough to cover tax); exit next bar | **On coarse data, free-spread fantasy.** Pure spread capture — a capacity game, not alpha. Hourly-or-coarser bars can't see the intrabar flow that decides whether your order actually gets hit. Trust its *live-scanner* EV; only trust a backtest once weeks of self-collected 5m data exist |

### Building price history

Two scripts feed the DuckDB store:

- **`collect`** takes one live snapshot per run (`osrs-flipper collect` or `python -m scripts.collect`). Put it on a
  5-minute cron to accumulate real 5m-granularity history — the only way to get it, since the Wiki API retains only
  ~30h of 5m data. A few weeks of this is the prerequisite for an honest `margin_flip` backtest:

  ```cron
  */5 * * * * cd /path/to/osrs-flipper && .venv/bin/python -m scripts.collect
  ```

- **`bootstrap`** seeds history immediately from `/timeseries` so backtests work on day one:
  `osrs-flipper bootstrap --timestep 24h --top 50`.

- **`scripts/plot.py`** is a standalone diagnostic: `python -m scripts.plot "Oak logs" --timestep 1h` renders a
  two-panel PNG (bid/ask + shaded spread, and a volume chart) to `data/charts/`.

`analysis.py` (surfaced by `analyze` / feeding the ranker) computes realised P&L by weighted-average cost matching,
a per-item realised-edge multiplier (penalty-only — sinks proven losers, never boosts winners above neutral), and
regime-shift flags for items whose recent edge has diverged from baseline.

---

## Discord alerts

Set a Discord channel and the terminal auto-starts a background push so you can step away from the keyboard:

```bash
export OSRS_FLIPPER_DISCORD_BOT_TOKEN=...      # preferred — can edit/delete/repost
export OSRS_FLIPPER_DISCORD_CHANNEL_ID=...
# or, post-only fallback:
export OSRS_FLIPPER_DISCORD_WEBHOOK=...
```

While the REPL is idle it polls every `ALERT_POLL_S` (60s), re-renders `go`, compresses it to a phone-friendly board
(a terse cash/slots header, a "⚠ needs you" block per flagged offer *with its concrete re-quote/re-list target*, then
the sell/buy/decant picks and NEXT — on-track offers, legends, and footnotes dropped), and **reposts it as a new
message at the bottom of the channel only when the actionable signature changes** (volatile cash-tick and bond-price
noise are excluded), deleting the previous board so exactly one exists at a time. All of this runs on the REPL's main
thread, so it never races the DuckDB journal. Control it with `alerts on|off|test`.

---

## Configuration reference

Everything lives in `osrs_flipper/config.py` as module constants; most are overridable via an `OSRS_FLIPPER_*`
environment variable. `OSRS_FLIPPER_CONTACT` is the only one you must set.

<details>
<summary><b>Full environment / tunable reference (click to expand)</b></summary>

| Constant | Env var | Default | Meaning |
|---|---|---|---|
| **Paths / sync** ||||
| `DATA_DIR` | — | `<repo>/data` | DuckDB + cache live here |
| `DB_PATH` | — | `data/osrs.duckdb` | Single DuckDB file (market data + journal) |
| `SYNC_DIR` | `OSRS_FLIPPER_SYNC_DIR` | `<repo>/sync` | Cross-machine learning export/import dir (commit it) |
| `PULL_EVAL_DELAY_S` | `…_PULL_EVAL_DELAY_S` | 1800 | Wait before judging a pulled rec good-pull vs regret |
| `PULL_GRACE_S` | `…_PULL_GRACE_S` | 600 | Hysteresis before an outranked rec is pulled |
| `BANK_PARTIAL_MIN_FRAC` | `…_BANK_PARTIAL_MIN_FRAC` | 0.005 | Min bankable-partial fraction of net worth to ping |
| **API / HTTP** ||||
| `API_BASE` | — | Wiki prices API | Base URL |
| `USER_AGENT` | `OSRS_FLIPPER_CONTACT` | placeholder | **Required** contact baked into the UA |
| `HTTP_TIMEOUT` | `…_HTTP_TIMEOUT` | 15 | Request timeout (s), fail fast |
| `PRICE_DIVERGENCE_MAX` | `…_PRICE_DIVERGENCE_MAX` | 0.15 | Reject if live book vs 1h-median disagree beyond this |
| `ADVERSE_MOVE_MAX_FRAC` | `…_ADVERSE_MOVE_MAX_FRAC` | 0.5 | Reject if downward drift ate this fraction of margin |
| `CACHE_ENABLED` | `OSRS_FLIPPER_NO_CACHE` | on (`=1` disables) | Disk TTL cache toggle |
| **GE tax** ||||
| tax rate | — | 2% (1% before 2025-05-29) | Dated seller-only tax |
| `TAX_CAP` | — | 5,000,000 | Max tax per item |
| `TAX_MIN_PRICE` | — | 50 | Below this, tax = 0 |
| **Fill model / calibration** ||||
| `BETA` | — | 0.25 | Spread-haircut prior (self-calibrates) |
| `CALIBRATE_EVERY_TRADES` | `…_CALIBRATE_EVERY_TRADES` | 10 | Recompute calibration every N resolved attempts |
| `GAMMA` | — | 0.15 | Per-bar fill capture (backtest) |
| `ALPHA` | — | 0.10 | Capacity capture as share of window volume |
| `IMPACT_K` | `…_IMPACT_K` | 1.0 | Market-impact slope prior (self-calibrates); 0 disables |
| `IMPACT_FLOOR` | `…_IMPACT_FLOOR` | 0.5 | Min market-impact multiplier |
| `HUNG_LEG_COST_FRAC` | `…_HUNG_LEG_COST_FRAC` | 0.01 | Trapped-capital EV haircut (fixed prior) |
| `HUNG_LEG_FLOOR` | `…_HUNG_LEG_FLOOR` | 0.5 | Min hung-leg multiplier |
| `SNAPSHOT_HORIZON_H` | `…_SNAPSHOT_HORIZON_H` | 2.0 | Ranking-snapshot fill-probability horizon |
| `BUY_LIMIT_WINDOW_H` | — | 4 | Rolling GE buy-limit window |
| `HOLD_WINDOW_H` | — | 8 | Hours a HOLD accumulates/sells over |
| **Strategies** ||||
| `Z_ENTRY` / `Z_EXIT` | — | 2.0 / 0.5 | Mean-reversion entry/exit z-scores |
| `Z_WINDOW` | — | 168 | Rolling z window (1h bars, 7d) |
| `VOL_Z_BREAKOUT` | — | 2.0 | Momentum volume-z requirement |
| `BREAKOUT_RANGE_DAYS` | — | 14 | Momentum range-break lookback |
| **Liquidity / staleness** ||||
| `STALENESS_MAX_S` | — | 3600 | Exclude items with no trade newer than this |
| `V_MIN_1H` | `…_V_MIN_1H` | 500 | Min 1h binding-side unit volume |
| `TURNOVER_MIN_1H` | `…_TURNOVER_MIN_1H` | 1,000,000 | Alt gp/h liquidity gate for high-price gear |
| `V_FLOOR_1H` | `…_V_FLOOR_1H` | 2 | Min units/h on the turnover branch |
| `REL_SPREAD_SUSPECT` | `…_REL_SPREAD_SUSPECT` | 0.20 | Rel-spread needing volume backing |
| `SPREAD_VOL_K` | `…_SPREAD_VOL_K` | 50,000 | Volume-per-suspect-spread scaling |
| `STALE_LEG_MAX_S` | `…_STALE_LEG_MAX_S` | 1800 | Max one-leg staleness before suspect |
| **Anomaly detector** ||||
| `ANOMALY_DIV_MIN` | `…_ANOMALY_DIV_MIN` | 0.15 | Min live-vs-1h divergence to flag |
| `ANOMALY_MIN_VOL` | `…_ANOMALY_MIN_VOL` | 1000 | Real-volume floor |
| `ANOMALY_VOL_Z_MIN` | `…_ANOMALY_VOL_Z_MIN` | 2.0 | Abnormal-volume z floor |
| `ANOMALY_CANDIDATES` | `…_ANOMALY_CANDIDATES` | 30 | Deep-check cap |
| `ANOMALY_FALL_SLOPE` | `…_ANOMALY_FALL_SLOPE` | −0.03 | Falling-knife slope guard |
| `MANIP_VOL_MAX` | `…_MANIP_VOL_MAX` | 200 | ≤ this 1h volume = thin/manipulable |
| `MANIP_PRICE_MIN` | `…_MANIP_PRICE_MIN` | 50,000 | ≥ this price = worth cornering |
| **Scanner / ranking** ||||
| `MIN_MARGIN_PCT` | — | 0.02 | Must clear tax to list |
| `MIN_NET_MARGIN` | `…_MIN_NET_MARGIN` | 2 | Absolute per-unit gp margin floor |
| `ROI_WEIGHT_FAST` / `_SLOW` | `…_ROI_WEIGHT_FAST`/`_SLOW` | 0.0 / 0.5 | ROI tilt for active-day vs overnight |
| `ROI_WEIGHT_SMALL_STACK` | `…_ROI_WEIGHT_SMALL_STACK` | 0.6 | ROI tilt when net worth is small |
| `ROI_STACK_LO` / `_HI` | `…_ROI_STACK_LO`/`_HI` | 300k / 20M | Net-worth range the tilt interpolates over |
| `BLACKLIST_IDS` | `OSRS_FLIPPER_BLACKLIST` | empty | Seed never-recommend ids (CSV) |
| `HOLD_MIN_MARGIN` | `…_HOLD_MIN_MARGIN` | 0.03 | Min ROI for a daytime HOLD |
| **Patient / gear / combos** ||||
| `PATIENT_BETA` | `…_PATIENT_BETA` | 0.0 | Full-spread assumption for gear/sets |
| `PATIENT_STALENESS_S` | `…_PATIENT_STALENESS_S` | 21600 | Relaxed staleness for low-freq items |
| `GEAR_MIN_PRICE` | `…_GEAR_MIN_PRICE` | 50,000 | Min unit price for `gear` |
| `COMBO_MIN_ROI` | `…_COMBO_MIN_ROI` | 0.0 | Hide sub-noise set combos |
| `COMBO_ANOMALY_CHECK` | `…_COMBO_ANOMALY_CHECK` | on | Pump/knife gate on bought legs |
| `PATIENT_EV_CONFIDENCE` | `…_PATIENT_EV_CONFIDENCE` | 0.55 | Haircut on best-case gear/set EV before ranking vs flips |
| **Account type** ||||
| `MEMBERS` | `OSRS_FLIPPER_MEMBERS` | 1 (members) | Members (8 slots) vs F2P (`=0`, 3 slots) |
| `GE_SLOTS` | — | 8 / 3 | Parallel active-offer cap |
| `BOND_ITEM_ID` | — | 13190 | Old School Bond |
| **Trader context** ||||
| `BANKROLL` | `OSRS_FLIPPER_BANKROLL` | 200,000 | Live capital cap for suggested quantities |
| `BACKTEST_BANKROLL` | `…_BACKTEST_BANKROLL` | 5,000,000 | Notional for backtests |
| `ACTIONS_PER_CYCLE` | `…_ACTIONS_PER_CYCLE` | 3 | Manual clicks per cycle (attention discount) |
| `SLOT_WORTH_LAMBDA` | `…_SLOT_WORTH_LAMBDA` | 0.5 | Opportunity-cost multiplier for a GE slot |
| `MAX_ALLOC_FRAC` | `…_MAX_ALLOC_FRAC` | 0.5 | Cap on capital in one position |
| `VARIANCE_AVERSION` | `…_VARIANCE_AVERSION` | 0.0 | Ranking penalty for low-completion flips (floor) |
| `OBJ_SHARPE_LOW` / `_HIGH` | `…_OBJ_SHARPE_LOW`/`_HIGH` | 1.0 / 3.0 | Sharpe range the adaptive objective ramps over |
| `VARIANCE_AVERSION_MAX` | `…_VARIANCE_AVERSION_MAX` | 1.0 | Ceiling for adaptive variance aversion (0 = off) |
| `CROWDING_TILT` | `…_CROWDING_TILT` | 0.25 | Uncrowded-niche tilt magnitude |
| `CROWDING_PIVOT` | `…_CROWDING_PIVOT` | 50,000,000 | gp/h where crowding = 0.5 |
| `EDGE_HALF_LIFE` | `…_EDGE_HALF_LIFE` | 30 | EWMA half-life for per-item realised edge |
| `EDGE_FLOOR` | `…_EDGE_FLOOR` | 0.3 | Min multiplier for a proven loser |
| `SWAP_RATIO` | `…_SWAP_RATIO` | 2.0 | Alt must beat an open offer's ROI/h by this to suggest a swap |
| `SWAP_MAX_FILL` | `…_SWAP_MAX_FILL` | 0.5 | Only swap offers under this fill fraction |
| `SWAP_MIN_AGE_H` | `…_SWAP_MIN_AGE_H` | 0.5 | Don't cancel a just-placed buy |
| **Schedule** ||||
| `AWAKE_START` / `_END` | `…_AWAKE_START`/`_END` | 9 / 23 | Active-hours window |
| `OVERNIGHT_MIN_MARGIN` | `…_OVERNIGHT_MIN_MARGIN` | 0.04 | Margin cushion required overnight |
| `OVERNIGHT_FILL_TARGET_H` | `…_OVERNIGHT_FILL_TARGET_H` | 8 | Target fill time for overnight bids |
| `NIGHT_SWITCH_H` | `…_NIGHT_SWITCH_H` | 3 | Hours before bed that `go` switches to overnight |
| `ACTIVE_OVERRIDE_MIN` | `…_ACTIVE_OVERRIDE_MIN` | 60 | Duration of a manual `active` override |
| **Recovery / reprice** ||||
| `RECOVERY_LOOKBACK_BARS` | `…_RECOVERY_LOOKBACK_BARS` | 168 | 1h-bar lookback (~1wk) |
| `RECOVERY_MIN_DIP` | `…_RECOVERY_MIN_DIP` | 0.03 | Min dip below week median |
| `REPRICE_DEADBAND` | `…_REPRICE_DEADBAND` | 0.02 | Min deviation before suggesting a reprice |
| `REPRICE_BIG_MOVE` | `…_REPRICE_BIG_MOVE` | 0.08 | Divergence at which the last tick is fully trusted |
| `CUT_ALT_MIN_ROI_H` | `…_CUT_ALT_MIN_ROI_H` | 0.03 | Min alt ROI/h to justify cutting a loss |
| **Progress / Monte-Carlo** ||||
| `PROGRESS_IDLE_GAP_MAX_H` | `…_PROGRESS_IDLE_GAP_MAX_H` | 24 | Idle-gap clamp for active-day compounding |
| `MC_PATHS` | `…_MC_PATHS` | 2000 | Monte-Carlo path count |
| `MC_SEED` | `…_MC_SEED` | 7 | Fixed seed (stable renders) |
| `MC_DECAY_PIVOT` | `…_MC_DECAY_PIVOT` | 20,000,000 | Net worth where drift starts decaying |
| `MC_HORIZON_DAYS` | `…_MC_HORIZON_DAYS` | 180 | Forward projection horizon |
| **Wealth cap / stores** ||||
| `MAX_COINS` | — | 2,147,483,647 | Hard coin-stack cap (2³¹−1) |
| `MAX_LIQUID_GP` | `…_MAX_LIQUID_GP` | = MAX_COINS | Glide ceiling (set higher to use platinum headroom) |
| `CAP_GLIDE_START_FRAC` | `…_CAP_GLIDE_START_FRAC` | 0.70 | Fraction of cap where the glide begins |
| `STORE_MIN_PRICE` | `…_STORE_MIN_PRICE` | 100,000 | Min unit price for a store candidate |
| `STORE_MIN_TURNOVER` | `…_STORE_MIN_TURNOVER` | 5,000,000 | Min both-side gp/h depth |
| `STORE_MAX_VOL` | `…_STORE_MAX_VOL` | 0.10 | Reject stores above this daily σ |
| `STORE_RISK_AVERSION` | `…_STORE_RISK_AVERSION` | 8.0 | λ in U = μ − ½λσ² |
| **Output** ||||
| `DISCORD_WEBHOOK_URL` | `OSRS_FLIPPER_DISCORD_WEBHOOK` | unset | One-way webhook (post-only fallback) |
| `DISCORD_BOT_TOKEN` | `OSRS_FLIPPER_DISCORD_BOT_TOKEN` | unset | Bot push (preferred — edit/delete/repost) |
| `DISCORD_CHANNEL_ID` | `OSRS_FLIPPER_DISCORD_CHANNEL_ID` | unset | Target channel for bot push |
| `ALERT_POLL_S` | `OSRS_FLIPPER_ALERT_POLL_S` | 60 | Idle push / poll interval (s) |

</details>

---

## Project layout

```
osrs_flipper/
  cli.py           argparse entrypoints (scan / portfolio / quote / backtest / collect / bootstrap / trade)
  terminal.py      the interactive REPL — `go` and every command
  config.py        all constants + OSRS_FLIPPER_* env overrides

  api.py http.py cache.py     OSRS Wiki API client, shared session, disk TTL cache
  datasource.py               live-account abstraction (Flip Exporter / legacy)
  runelite.py flip_exporter.py local_export.py   read-only RuneLite plugin parsers
  store.py journal.py         DuckDB: market-data warehouse + trade/portfolio ledger

  features.py fills.py        feature engineering + the conservative fill model
  scanner.py quote.py objective.py   ranking, deep re-price, adaptive objective
  planner.py combinations.py combos.py   heterogeneous ranking + GE set arbitrage
  anomaly.py regret.py recovery.py   manipulation detection, pull grading, bounce-vs-cut
  calibration.py              self-calibration (β / fill / ETA / impact) from real fills
  tax.py                      dated GE tax mechanics
  macro.py treasury.py wealth.py   bond signal, store-of-value screen, cap glide
  progress.py                 net-worth chart + Monte-Carlo projection
  alert.py monitor.py         dashboard formatting + Discord push
  analysis.py                 realised P&L, per-item edge, regime shifts

  backtest/
    engine.py metrics.py      replay + β-band metrics
    strategies/{base,mean_reversion,momentum,margin_flip}.py

scripts/
  collect.py                  one price snapshot (put on a 5-min cron)
  bootstrap.py                seed history from /timeseries
  plot.py                     diagnostic bid/ask + volume PNGs

tests/                        ~28 files: fill model, tax, scanner, planner, journal, calibration, backtest, terminal, …
```

---

## Testing

```bash
pytest                        # whole suite
pytest tests/test_backtest.py -v
```

~28 test files (~4k lines) covering the fill model and calibration, dated tax math, the scanner/ranking
path, portfolio/journal persistence, RuneLite export parsing (including the `isBuy`-not-string-match guard),
the terminal, and the backtest engine (buy-limit and capital capping, mark-to-market bailout losses, and that
a heavier β strictly reduces P&L). Lint with `ruff` (config in `pyproject.toml`).

---

## Honest caveats

- **Cold start — early recommendations are prior-driven, not measured.** β, fill-rate, fill-time, and market
  impact ship as *priors*. Three self-calibrate from your real fills (impact once enough samples exist), shrunk
  toward the prior every `CALIBRATE_EVERY_TRADES` resolved attempts; the hung-leg term stays a fixed prior. Until
  fills accumulate, the EV is only as honest as its priors — educated guesses that get truer as you log trades.
- **You read the same public feed as the bots — so speed is not the edge.** Everything derives from the delayed
  Wiki API that everyone flipping the same items reads. Racing a fast-margin staple often just makes you someone
  else's exit liquidity. The durable edge is *patience* and *uncrowded niches*: buy-limit arbitrage and fat-margin
  holds on items too slow or annoying for bots to bother with — which is why the overnight plan bids low and waits,
  and the crowding tilt steers away from bot-raced staples on purpose.
- **Patient gear/sets are priced best-case (β=0).** That's optimistic by design; the `PATIENT_EV_CONFIDENCE` haircut
  is a stopgap until live calibration exists for big-ticket items (which trade too rarely to calibrate quickly).
- **`margin_flip` backtests are not trustworthy on coarse data** — trust its live-scanner EV, and only trust a
  backtest once weeks of self-collected 5m history exist.
- **The adaptive objective's Sharpe→competition link is a hypothesis**, stated as such in the code, and can be
  switched off entirely.
