"""Command-line entrypoints: scan / backtest / collect / bootstrap."""

from __future__ import annotations

import argparse

from . import config


def _limit_used() -> dict[int, int]:
    """Trailing-4h buy-limit usage from the journal; empty if the journal is busy."""
    try:
        from .journal import Journal
        with Journal() as j:
            return j.buy_limit_used()
    except Exception:
        return {}


def _cmd_scan(args: argparse.Namespace) -> None:
    from . import alert, scanner

    df = scanner.scan(
        members=True if args.members else None,
        bankroll=args.bankroll,
        top=args.top,
        include_suspect=args.include_suspect,
        persistence=not args.no_persistence,
        mode=args.mode,
        min_gp=args.min_gp,
        limit_used=_limit_used(),
    )
    print(alert.format_table(df, mode=args.mode))
    summary = alert.format_portfolio_summary(df, args.bankroll)
    if summary:
        print("\n" + summary)
    prog = scanner.bond_progress(bankroll=args.bankroll)
    print()
    print(alert.format_bond_line(prog))
    if args.discord:
        ok = alert.to_discord(alert.format_table(df))
        print(f"discord: {'sent' if ok else 'no webhook configured'}")


def _cmd_trade(args: argparse.Namespace) -> None:
    from .terminal import run

    run()


def _cmd_portfolio(args: argparse.Namespace) -> None:
    from . import alert, scanner

    held: list = []
    cash = args.bankroll or config.BANKROLL
    limit_used: dict = {}
    try:
        from .journal import Journal
        with Journal() as j:
            held = j.positions()
            cash = args.bankroll or int(j.cash()) or config.BANKROLL
            limit_used = j.buy_limit_used()
    except Exception:
        print("(journal busy — it's open in the `trade` terminal. Using --bankroll; "
              "run `port` inside the terminal for held-position- and buy-limit-aware planning.)\n")
    from . import runelite
    rl = runelite.read()
    active_ids = [o.item_id for o in runelite.active_offers(rl)] if rl else []
    if args.slots:
        free, source = args.slots, "specified"
    elif rl is not None:
        free, source = runelite.free_slots(rl, config.GE_SLOTS), "runelite"
    else:
        free, source = max(0, config.GE_SLOTS - len(held)), "assumed"
    picks, idle = scanner.build_portfolio(
        bankroll=cash, held_ids=[h.item_id for h in held] + active_ids, free_slots=free,
        members=True if args.members else None, min_gp=args.min_gp or None, limit_used=limit_used)
    print(alert.format_portfolio(picks, cash, held, idle, free_slots=free, slot_source=source))


def _cmd_quote(args: argparse.Namespace) -> None:
    from . import alert, api
    from .quote import optimal_quote, suggested_qty

    meta = next((r for r in api.mapping()
                 if (args.item.isdigit() and r["id"] == int(args.item))
                 or r["name"].lower() == args.item.lower()), None)
    if not meta:
        print(f"item not found: {args.item}")
        return
    limit_eff = max(0, (meta.get("limit") or 0) - _limit_used().get(meta["id"], 0))
    qty = args.qty or suggested_qty(meta["id"], limit_eff, args.bankroll)
    q = optimal_quote(meta["id"], qty, name=meta["name"], capture=args.capture, timestep=args.timestep)
    print(alert.format_quote(q))


def _cmd_backtest(args: argparse.Namespace) -> None:
    from .backtest.engine import run_backtest

    run_backtest(strategy=args.strategy, timestep=args.timestep, top=args.top, members=args.members)


def _cmd_collect(args: argparse.Namespace) -> None:
    from scripts.collect import collect_once

    collect_once()


def _cmd_bootstrap(args: argparse.Namespace) -> None:
    from scripts.bootstrap import bootstrap

    bootstrap(timestep=args.timestep, top=args.top, members=args.members)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="osrs-flipper", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="rank live flips")
    s.add_argument("--top", type=int, default=20)
    s.add_argument("--bankroll", type=int, default=config.BANKROLL)
    s.add_argument("--members", action="store_true", help="include members items")
    s.add_argument("--include-suspect", action="store_true", help="include manipulation-suspect items")
    s.add_argument("--mode", default="balanced", choices=["online", "balanced", "offline"],
                   help="online=fast fills, offline=fat margins, balanced=both")
    s.add_argument("--min-gp", type=int, default=0, help="hide flips netting less than this gp/cycle")
    s.add_argument("--no-persistence", action="store_true", help="skip the spread-stability deep-check (faster)")
    s.add_argument("--discord", action="store_true", help="also post to the configured Discord webhook")
    s.set_defaults(func=_cmd_scan)

    t = sub.add_parser("trade", help="launch the interactive trading terminal (no tokens)")
    t.set_defaults(func=_cmd_trade)

    pf = sub.add_parser("portfolio", help="recommend a diversified allocation for your free slots")
    pf.add_argument("--bankroll", type=int, default=0, help="override cash (default: journal balance)")
    pf.add_argument("--slots", type=int, default=0, help="free slots (default: GE_SLOTS - held positions)")
    pf.add_argument("--min-gp", type=int, default=0, help="min gp/flip to be worth a slot (default: ~0.2%% of cash)")
    pf.add_argument("--members", action="store_true")
    pf.set_defaults(func=_cmd_portfolio)

    q = sub.add_parser("quote", help="solve for the gp/hour-optimal buy/sell prices for an item")
    q.add_argument("item", help="item name or id")
    q.add_argument("--qty", type=int, default=0, help="units (default: bankroll/price)")
    q.add_argument("--bankroll", type=int, default=config.BANKROLL)
    q.add_argument("--capture", type=float, default=config.ALPHA, help="share of market volume you capture")
    q.add_argument("--timestep", default=config.PERSIST_TIMESTEP, choices=["5m", "1h", "6h", "24h"])
    q.set_defaults(func=_cmd_quote)

    b = sub.add_parser("backtest", help="backtest a strategy on bootstrapped history")
    b.add_argument("strategy", choices=["mean_reversion", "momentum", "margin_flip"])
    b.add_argument("--timestep", default="24h", choices=["5m", "1h", "6h", "24h"])
    b.add_argument("--top", type=int, default=30, help="watchlist size (most liquid F2P items)")
    b.add_argument("--members", action="store_true")
    b.set_defaults(func=_cmd_backtest)

    c = sub.add_parser("collect", help="snapshot current prices into DuckDB")
    c.set_defaults(func=_cmd_collect)

    bs = sub.add_parser("bootstrap", help="seed history from /timeseries")
    bs.add_argument("--timestep", default="24h", choices=["5m", "1h", "6h", "24h"])
    bs.add_argument("--top", type=int, default=50)
    bs.add_argument("--members", action="store_true")
    bs.set_defaults(func=_cmd_bootstrap)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
