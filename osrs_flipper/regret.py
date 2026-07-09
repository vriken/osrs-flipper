"""Pull evaluation: was it a good call for the engine to *drop* a recommendation you never acted on?

Judged in hindsight from the flip's own economics AFTER the pull (we snapshot the item forward even
though it's no longer recommended):

  * good_pull — the spread degraded (gone/negative, or well below where it was when we pulled it): the
                pull dodged a flip that would have disappointed.
  * regret    — the spread HELD (still worth trading, near its pre-pull level): we withdrew a still-good
                flip, so the pull was premature — a signal a gate (reliability/anomaly) or scan churn is
                too trigger-happy.

Pure and network-free (the terminal feeds it the pull-time snapshot and the current price); this is a
heuristic — a pulled flip can legitimately die then recover — so it's a *rate* to watch, not a verdict
on any single pull. `pull_quality` aggregates the rates by reason so a systematic over-pull bias shows.
"""

from __future__ import annotations


def classify_pull(snap_net: float | None, cur_net: float | None, *, min_net: int,
                  hold_frac: float = 0.5) -> str | None:
    """`snap_net` = achievable post-tax margin when we pulled it; `cur_net` = now. Returns good_pull /
    regret / None (can't tell — no current price)."""
    if cur_net is None:
        return None
    if cur_net < min_net or (snap_net is not None and snap_net > 0 and cur_net < hold_frac * snap_net):
        return "good_pull"          # spread died or collapsed vs where it was → pulling dodged it
    return "regret"                 # still a healthy spread → we withdrew a good flip too soon
