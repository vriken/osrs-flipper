# Read live GE state from RuneLite Flipping Utilities (read-only)

We read the RuneLite **Flipping Utilities** plugin's local account JSON
(`~/.runelite/flipping/<account>.json`) to learn true slot occupancy, active offers,
and (once present) completed trades — so the portfolio's free-slot count and the journal
are *observed*, not assumed or hand-logged. It is strictly read-only; execution stays
manual (consistent with ADR 0001 — we observe state, we don't automate actions).

The trade-off: we depend on a third-party plugin's **undocumented** on-disk schema,
decoded by inspecting a real file (`b`=is-buy, `id`=item, `s`=slot, `st`=state,
`tQIT`=qty, `p`=price; slots occupied iff `slotTimers[*].currentOffer` is present). A
plugin format change can break the reader — we accept that for ground-truth state over
manual entry, and isolate all the fragility in `runelite.py`.
