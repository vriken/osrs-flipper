# OSRS GE Flipper

A manual-execution tool for making GP on the Old School RuneScape Grand Exchange by
capturing bid-ask spreads. It surfaces ranked flips, solves order prices, and tracks a
portfolio — you place every trade yourself.

## Language

**Flip**:
A buy→sell round-trip whose only goal is capturing the bid-ask spread (minus GE tax). No view on price direction.
_Avoid_: trade, deal, arbitrage

**Active flip**:
A flip worked in a GE slot right now, for fast turnover.

**Hold** (accumulate):
A *slow flip* — bought into inventory now because it won't fit in the active slots, sold over later buy-limit cycles. Still spread capture.
_Avoid_: investment, position-trade

**Directional strategy**:
A predictive approach that bets on price movement (mean-reversion, momentum). Backtest/research only — NOT a flip.
_Avoid_: calling these "flips"

### Slots & capital

**GE slot**:
One of the concurrent Grand Exchange offer slots (3 F2P / 8 members). Occupied by ANY active offer — a pending buy or an in-progress sell.

**Held** (position):
Inventory bought and not yet fully sold. Occupies a sell-side slot while you sell it.

**Free slots**:
GE slots with no active offer. The tool CANNOT observe true occupancy (it only sees logged fills, not pending offers), so this is a **number you supply**; the default (`GE_SLOTS − held`) is an explicitly-captioned assumption that your only busy slots hold inventory.
_Avoid_: treating the default free-slot count as ground truth

### Profit metrics

**Margin** (shown as `net`):
gp kept per unit after tax = `post_tax(sell) − buy`. The spread you keep.

**Expected flip profit** (shown as `gp/cyc`):
`margin × qty × P(complete)` — expected gp from one buy→sell cycle. The canonical "what this flip makes."

**gp/hour**:
`expected flip profit ÷ fill time` — a *ranking lens* for time-constrained trading, not a distinct kind of profit.

**Realized-when-sold** (the `gp` on a **Hold** row):
`margin × qty` assuming the accumulated inventory eventually fully sells (no time bound). Distinct from per-cycle profit — never sum it with `gp/cyc`.

**SCORE**:
The scanner's *ranking key* — a mode-weighted composite, shrunk for the optimizer's curse. NOT a literal gp figure.
_Avoid_: reading SCORE as "gp"

## Relationships

- A **Flip** is either **Active** or **Hold** — same kind, differing only in turnover speed.
- **Directional strategies** are separate from flipping and live only in the backtest.
- **Active** flip profit is per-cycle (`gp/cyc`); **Hold** profit is realized-when-sold — different time bases, reported separately.

## Example dialogue

> **Dev:** "Is the Apple pie hold a flip or an investment?"
> **Ake:** "A flip — a slow one. We buy it to sell back at the spread, not because we think apple pies go up."

## Flagged ambiguities

- "investment"/"hold" implied a directional bet — resolved: a **hold** is a slow flip (spread capture), not a directional position.
- "free slots" — the journal only knows inventory-holding slots, not pending offers, so it can't compute true occupancy — resolved: **free slots is user-supplied**, with `GE_SLOTS − held` as an explicitly-labelled default assumption.
