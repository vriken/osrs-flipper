"""Interactive trading terminal — run it and drive everything without spending tokens.

    osrs-flipper trade

Commands (type `help`):
  port [free_slots]        recommended diversified allocation for your free slots
  scan [n] [online|offline|balanced]   ranked live flips (mode sets speed-vs-margin)
  quote <item> [qty]       solve optimal buy/sell prices for an item
  buy <item> <quantity> <price>    log a buy fill
  sell <item> <quantity> <price>   log a sell fill (applies GE tax)
  pos                      open positions + unrealised P&L (vs live bid)
  pnl                      realised P&L, cash, equity, bond progress
  recent [n]               recent trades
  preds [n]                logged model predictions (for calibration)
  bank <amount>            set your current cash balance
  help | quit
"""

from __future__ import annotations

import time

from . import alert, api, config, scanner
from .journal import Journal
from .quote import optimal_quote

_BOND = config.BOND_ITEM_ID


class Terminal:
    def __init__(self, db: str | None = None) -> None:
        self.j = Journal(path=db)
        self._map: dict[str, dict] | None = None
        self._latest: dict[int, dict] = {}
        self._latest_ts = 0.0

    # --- data helpers --------------------------------------------------------
    def mapping(self) -> dict[str, dict]:
        if self._map is None:
            self._map = {r["name"].lower(): r for r in api.mapping()}
        return self._map

    def latest(self, max_age: float = 30) -> dict[int, dict]:
        if time.time() - self._latest_ts > max_age:
            self._latest = api.latest()
            self._latest_ts = time.time()
        return self._latest

    def resolve(self, token: str) -> dict | None:
        m = self.mapping()
        if token.isdigit():
            return next((r for r in m.values() if r["id"] == int(token)), None)
        if token.lower() in m:
            return m[token.lower()]
        hits = [r for k, r in m.items() if token.lower() in k]
        if len(hits) == 1:
            return hits[0]
        if hits:
            print("  ambiguous — matches:", ", ".join(sorted(r["name"] for r in hits[:8])))
        return None

    # --- commands ------------------------------------------------------------
    def cmd_scan(self, args: list[str]) -> None:
        top = next((int(a) for a in args if a.isdigit()), 15)
        mode = next((a for a in args if a in scanner.MODE_WEIGHTS), "balanced")
        bankroll = int(self.j.cash()) or config.BANKROLL
        print(f"  scanning ({mode})…")
        df = scanner.scan(top=top, bankroll=bankroll, mode=mode, limit_used=self.j.buy_limit_used())
        print(alert.format_table(df, mode=mode))
        summary = alert.format_portfolio_summary(df, bankroll)
        if summary:
            print("\n" + summary)

    def cmd_quote(self, args: list[str]) -> None:
        if not args:
            print("  usage: quote <item> [qty]")
            return
        qty = None
        if args[-1].isdigit() and len(args) > 1:
            qty, args = int(args[-1]), args[:-1]
        meta = self.resolve(" ".join(args))
        if not meta:
            print("  item not found")
            return
        from .quote import suggested_qty
        bankroll = int(self.j.cash()) or config.BANKROLL
        # respect the rolling 4h buy limit already used on this item
        limit_eff = max(0, (meta.get("limit") or 0) - self.j.buy_limit_used().get(meta["id"], 0))
        qty = qty or suggested_qty(meta["id"], limit_eff, bankroll)
        if qty <= 0:
            print("  buy limit reached for this item (4h window) or no cash — nothing to quote")
            return
        q = optimal_quote(meta["id"], qty, name=meta["name"])
        print(alert.format_quote(q))
        if q:  # log the prediction so we can calibrate it against your real fills later
            self.j.log_prediction(meta["id"], meta["name"], q.qty, q.buy_px, q.sell_px,
                                  q.p_buy, q.p_sell, q.p_round, q.ev)

    def cmd_preds(self, args: list[str]) -> None:
        n = int(args[0]) if args and args[0].isdigit() else 10
        rows = self.j.recent_predictions(n)
        if not rows:
            print("  (no predictions yet — they're logged each time you `quote` an item)")
            return
        for p in rows:
            print(f"  [{p['source']:5}] {p['name'][:16]:16} qty {p['qty']:>7,}  buy {p['buy_px']:>7,}  "
                  f"sell {p['sell_px']:>7,}  round {p['p_round']:>4.0%}  EV {p['ev']:>8,.0f}")

    def _trade(self, args: list[str], side: str) -> None:
        # item name comes first (may contain spaces) → qty and price are the last two tokens
        if len(args) < 3 or not args[-2].isdigit() or not args[-1].isdigit():
            print(f"  usage: {side} <item> <quantity> <price>")
            return
        qty, price = int(args[-2]), int(args[-1])
        meta = self.resolve(" ".join(args[:-2]))
        if not meta:
            print("  item not found")
            return
        if side == "buy":
            cost = self.j.record_buy(meta["id"], meta["name"], qty, price)
            print(f"  bought {qty:,} {meta['name']} @ {price} = -{cost:,.0f} | cash {self.j.cash():,.0f}")
            try:  # pair this entry with the model's prediction for later calibration
                q = optimal_quote(meta["id"], qty, name=meta["name"])
                if q:
                    self.j.log_prediction(meta["id"], meta["name"], q.qty, q.buy_px, q.sell_px,
                                          q.p_buy, q.p_sell, q.p_round, q.ev, source="buy")
            except Exception:
                pass
        else:
            proceeds, realized = self.j.record_sell(meta["id"], meta["name"], qty, price)
            print(f"  sold {qty:,} {meta['name']} @ {price} = +{proceeds:,.0f} "
                  f"(realised {realized:+,.0f}) | cash {self.j.cash():,.0f}")

    def cmd_port(self, args: list[str]) -> None:
        cash = int(self.j.cash()) or config.BANKROLL
        held = self.j.positions()
        free = int(args[0]) if args and args[0].isdigit() else max(0, config.GE_SLOTS - len(held))
        print(f"  building portfolio for {free} free slot(s)…")
        picks, idle = scanner.build_portfolio(
            bankroll=cash, held_ids=[h.item_id for h in held], free_slots=free,
            limit_used=self.j.buy_limit_used())
        print(alert.format_portfolio(picks, cash, held, idle))

    def cmd_pos(self) -> None:
        pos = self.j.positions()
        if not pos:
            print("  (no open positions)")
            return
        lat = self.latest()
        print(f"  {'item':20} {'qty':>8} {'avg':>9} {'bid':>9} {'unreal':>11}")
        for p in pos:
            bid = lat.get(p.item_id, {}).get("low")
            from .tax import post_tax_received
            unreal = (post_tax_received(bid, item_id=p.item_id) - p.avg_cost) * p.qty if bid else 0
            print(f"  {p.name[:20]:20} {p.qty:>8,} {p.avg_cost:>9,.1f} "
                  f"{(bid or 0):>9,} {unreal:>+11,.0f}")

    def cmd_pnl(self) -> None:
        lat = self.latest()
        bids = {p.item_id: lat.get(p.item_id, {}).get("low") for p in self.j.positions()}
        equity = self.j.equity(bids)
        bond = lat.get(_BOND, {}).get("high")
        print(f"  cash:        {self.j.cash():>14,.0f}")
        print(f"  inventory:   {self.j.inventory_value(bids):>14,.0f}")
        print(f"  equity:      {equity:>14,.0f}")
        print(f"  realised P&L:{self.j.realized_pnl():>+14,.0f}")
        if bond:
            print(f"  bond:        {bond:>14,.0f}  ({equity / bond * 100:.1f}% — {bond - equity:,.0f} to go)")

    def cmd_recent(self, args: list[str]) -> None:
        n = int(args[0]) if args and args[0].isdigit() else 10
        for t in self.j.recent(n):
            tag = f"{t['pnl']:+,.0f}" if t["side"] == "SELL" else ""
            print(f"  {t['side']:4} {t['qty']:>8,} {t['name'][:18]:18} @ {t['price']:>7,} {tag}")

    def cmd_bank(self, args: list[str]) -> None:
        if not args or not args[0].replace("_", "").isdigit():
            print(f"  current cash: {self.j.cash():,.0f}  (set with `bank <amount>`)")
            return
        self.j.set_cash(float(args[0].replace("_", "")))
        print(f"  cash set to {self.j.cash():,.0f}")

    # --- loop ----------------------------------------------------------------
    def run(self) -> None:
        print("osrs-flipper terminal — type `help`, `quit` to exit")
        handlers = {
            "scan": lambda a: self.cmd_scan(a), "quote": lambda a: self.cmd_quote(a),
            "buy": lambda a: self._trade(a, "buy"), "sell": lambda a: self._trade(a, "sell"),
            "port": lambda a: self.cmd_port(a), "portfolio": lambda a: self.cmd_port(a),
            "pos": lambda a: self.cmd_pos(), "positions": lambda a: self.cmd_pos(),
            "pnl": lambda a: self.cmd_pnl(), "recent": lambda a: self.cmd_recent(a),
            "preds": lambda a: self.cmd_preds(a),
            "bank": lambda a: self.cmd_bank(a), "help": lambda a: print(__doc__),
            "?": lambda a: print(__doc__),
        }
        while True:
            try:
                raw = input("osrs> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not raw:
                continue
            cmd, *args = raw.split()
            cmd = cmd.lower()
            if cmd in ("quit", "exit", "q"):
                break
            fn = handlers.get(cmd)
            if not fn:
                print(f"  unknown command: {cmd} (type `help`)")
                continue
            try:
                fn(args)
            except Exception as e:  # keep the REPL alive on any error
                print(f"  error: {e}")


def run() -> None:
    Terminal().run()
