# Conservative fill model is the core

Profit is computed through a deliberately pessimistic fill model — β spread-haircut,
γ partial fills capped by contra-side volume, an adverse-selection gate, and
mark-to-market bail-out on unsold inventory — instead of the industry-standard "every
buy fills at the bid and every sell at the ask." Most GE tools overstate returns 3–10×
by assuming full-spread capture; we trade rosy numbers for honest ones, and every
downstream figure (scan SCORE, quote EV, portfolio profit) depends on this choice.

The parameters (β, γ, α, mode horizons) are provisional estimates, not measured truth —
they are meant to be calibrated against the user's real logged fills over time.
