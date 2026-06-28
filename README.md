# osrs-flipper

A flip-finder and strategy backtester for the OSRS Grand Exchange. It ranks live
flips and backtests trading strategies against the [OSRS Wiki real-time prices
API](https://prices.runescape.wiki/api/v1/osrs).

**You execute trades manually.** This is a market-analysis tool, not a bot — it does
not automate the game client. Automating RuneScape violates Jagex's EULA and gets
accounts permanently banned (account + items confiscated), which also makes zero GP.
Everything here reads public price data; you place the buy/sell offers yourself.

## Why it tells the truth

Most GE tools overstate returns 3–10× by assuming every buy fills at the bid and
every sell at the ask. This one models fills conservatively (`osrs_flipper/fills.py`):

- **Spread haircut (β)** — you transact *inside* the spread, not at its extreme. With
  integer-gp rounding this collapses penny-spread "opportunities" to nothing.
- **Partial fills (γ)** — per window you capture only a fraction of the contra-side volume.
- **Adverse-selection gate** — a resting buy fills when price is moving *against* you.
- **Mark-to-market bail-out** — unsold inventory is liquidated at a loss, never hidden.
- **Liquidity/staleness gate** — stale, null-priced "ghost" items are excluded entirely.

Every backtest reports a **β sensitivity band** (β=0 fantasy ceiling → 0.25 honest
default → 0.5 floor) plus completion rate, win rate, ROI, gp/day, and drawdown.

## Setup

```bash
uv venv && . .venv/bin/activate          # or: python3 -m venv .venv && . .venv/bin/activate
uv pip install -e ".[dev]"               # or: pip install -e ".[dev]"
export OSRS_FLIPPER_CONTACT="you@example or @you on Discord"   # required by the API's UA policy
```

Optional environment overrides: `OSRS_FLIPPER_BANKROLL` (default 200000),
`OSRS_FLIPPER_MEMBERS=1` (default F2P-only), `OSRS_FLIPPER_DISCORD_WEBHOOK`.

## Usage

```bash
osrs-flipper trade                         # interactive terminal — scan, quote, log fills, track P&L
osrs-flipper quote "Grapes" --qty 2800     # solve gp/hour-optimal buy/sell prices + frontier
osrs-flipper scan --top 20                 # rank live flips you can afford (F2P, your bankroll)
osrs-flipper scan --members --top 20       # include members items (after you redeem a bond)
osrs-flipper backtest mean_reversion --timestep 24h --top 25
osrs-flipper bootstrap --timestep 24h --top 50   # seed history into DuckDB
osrs-flipper collect                       # one snapshot; put on a 5-min cron to build 5m history
```

### The terminal (`trade`)

A self-contained REPL — no LLM, no token cost. State persists in DuckDB across sessions.

```
osrs> bank 204000                  set your current cash
osrs> port                         recommended allocation: active flips + accumulate
osrs> scan 15                      ranked live flips
osrs> quote Grapes                 optimal buy/sell prices + efficient frontier
osrs> buy Grapes 2800 71           log a buy fill (item qty price)
osrs> sell Grapes 2800 78          log a sell fill (applies GE tax)
osrs> pos                          open positions + unrealised P&L
osrs> pnl                          cash, equity, realised P&L, bond progress
osrs> recent                       recent trades
```

`scan` ranks by `margin_after_tax × affordable_quantity × P(both legs fill)`, so
stale, illiquid, and penny-spread traps sink on their own. It also shows how close
your bankroll is to a **bond** (the F2P → members milestone).

## What actually makes GP

- **Small bankroll (F2P, ~200k):** capital is the binding constraint, not buy limits.
  The edge is high ROI% × turnover × compounding on cheap liquid items — and avoiding
  the penny-spread rune traps that *look* best but fill worst.
- **mean_reversion** backtests as the most robust strategy (ROI stable across the β band).
- **momentum** is a thin, real edge. **margin_flip** on coarse data is mostly the
  free-spread fantasy — trust its live-scanner EV, not a backtest, until you've
  collected weeks of 5m data with `collect`.

## Layout

```
osrs_flipper/   api, http, store(DuckDB), tax, features, fills, scanner, alert, cli
  backtest/     engine, metrics, strategies/{mean_reversion,momentum,margin_flip}
scripts/        collect (cron snapshotter), bootstrap (seed /timeseries)
tests/          tax, fills, features, backtest
```

Tax mechanics, buy limits, and the fill-model design are documented inline. Tax is
dated (1% before 2025-05-29, 2% after) so historical backtests stay correct.
