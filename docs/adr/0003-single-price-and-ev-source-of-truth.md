# Single source of truth: 1h-average prices, scanner ranks via the quote optimizer

Both the scanner and the `quote` command price off the **1h-average** bid/ask (live
`/latest` only as a fallback when a side is null), and the scanner ranks each candidate
by re-running the **quote optimizer's EV** rather than a separate cheap heuristic. This
guarantees the scanner and `quote` can never disagree — a bug we hit when the scanner
used 1h-averages while `quote` used live `/latest` (which returned "no quote" whenever a
live side was momentarily null/collapsed).

The cost is more per-candidate computation in the scan. A future reader might be tempted
to "optimize" the scanner back to live prices or a standalone ranking heuristic — that
would reintroduce the divergence, so don't.
