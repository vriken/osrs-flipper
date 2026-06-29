"""Interactive trading terminal — run it and drive everything without spending tokens.

    osrs-flipper trade

Commands (type `help`):
  sync                    import completed RuneLite fills into the journal
  orders | ge             live GE slots + active offers (from RuneLite)
  review | check          flag active offers to re-price / cancel / collect
  port [free_slots]        recommended allocation (free slots auto-read from RuneLite)
  overnight [item]         plan one big ~8h buy to leave while you sleep
  brief | now              schedule-aware: day plan in active hours, overnight plan off-hours
  scan [n] [online|offline|balanced]   ranked live flips (mode sets speed-vs-margin)
  quote <item> [qty]       solve optimal buy/sell prices for an item
  sellquote | sq <item> [qty]  sell-price tradeoff for held inventory (fill time vs profit)
  buy <item> <quantity> <price>    log a buy fill
  sell <item> <quantity> <price>   log a sell fill (applies GE tax)
  placed [item buy|sell qty price]  log a PLACED order (or the last quote) for fill calibration
  calibrate | calib        measure empirical β + fill correction from your real attempts
  pos                      open positions + unrealised P&L (vs live bid)
  pnl                      realised P&L, cash, equity, bond progress
  recent [n]               recent trades
  preds [n]                logged model predictions (for calibration)
  bank <amount>            set your current cash balance
  update                   git pull latest + reload (OTA, no manual restart)
  reload                   re-exec to pick up code changes (keeps your DB/state)
  help | quit
"""

from __future__ import annotations

import time

from . import alert, api, config, runelite, scanner
from .journal import Journal
from .quote import optimal_quote

_BOND = config.BOND_ITEM_ID

_VERDICTS = {
    "collect": ("📦 COLLECT — frees a slot", "yellow"),
    "margin": ("🟠 MARGIN GONE — spread collapsed; cancel/re-quote", "red"),
    "stale": ("🔴 STALE — likely mispriced; cancel & re-quote", "red"),
    "slow": ("🟡 SLOW — consider re-pricing", "yellow"),
    "ontrack": ("🟢 on track", "green"),
    "done": ("done", None),
}


class Terminal:
    def __init__(self, db: str | None = None) -> None:
        self.j = Journal(path=db)
        self._map: dict[str, dict] | None = None
        self._latest: dict[int, dict] = {}
        self._latest_ts = 0.0
        self._last_quote: tuple | None = None  # (meta, Quote, qty) — used by `placed`

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
        df = scanner.scan(top=top, bankroll=bankroll, mode=mode, limit_used=self._limit_used())
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
        # respect the rolling 4h buy limit already used (RuneLite counter preferred)
        limit_eff = max(0, (meta.get("limit") or 0) - self._limit_used().get(meta["id"], 0))
        qty = qty or suggested_qty(meta["id"], limit_eff, bankroll)
        if qty <= 0:
            print("  buy limit reached for this item (4h window) or no cash — nothing to quote")
            return
        q = optimal_quote(meta["id"], qty, name=meta["name"])
        print(alert.format_quote(q))
        if q:  # log the prediction so we can calibrate it against your real fills later
            self.j.log_prediction(meta["id"], meta["name"], q.qty, q.buy_px, q.sell_px,
                                  q.p_buy, q.p_sell, q.p_round, q.ev)
            self._last_quote = (meta, q, qty)  # `placed` records this without re-typing
            print("  → placed it? type `placed` to log it for fill calibration")

    def cmd_preds(self, args: list[str]) -> None:
        n = int(args[0]) if args and args[0].isdigit() else 10
        rows = self.j.recent_predictions(n)
        if not rows:
            print("  (no predictions yet — they're logged each time you `quote` an item)")
            return
        for p in rows:
            print(f"  [{p['source']:5}] {p['name'][:22]:22} qty {p['qty']:>7,}  buy {p['buy_px']:>7,}  "
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
            except Exception as e:  # don't lose the buy, but don't silently corrupt calibration data
                print(f"  ⚠ prediction not logged for calibration ({type(e).__name__}: {e})")
        else:
            proceeds, realized = self.j.record_sell(meta["id"], meta["name"], qty, price)
            print(f"  sold {qty:,} {meta['name']} @ {price} = +{proceeds:,.0f} "
                  f"(realised {realized:+,.0f}) | cash {self.j.cash():,.0f}")

    def _snapshot(self, item_id: int) -> dict:
        """Decision-time market snapshot for an attempt (1h averages + binding volume)."""
        hr = api.one_hour().get(item_id, {})
        low_vol, high_vol = hr.get("lowPriceVolume") or 0, hr.get("highPriceVolume") or 0
        return {"avg_low": hr.get("avgLowPrice"), "avg_high": hr.get("avgHighPrice"),
                "vol_1h_binding": min(low_vol, high_vol)}

    def cmd_placed(self, args: list[str]) -> None:
        """Record an order you just placed in-game, so its fill calibrates the model.
          placed                              log the last `quote` (its BUY leg)
          placed <item> <buy|sell> <qty> <price>"""
        if not args:
            if not self._last_quote:
                print("  nothing to record — run `quote <item>` first, or: "
                      "placed <item> <buy|sell> <qty> <price>")
                return
            meta, q, _ = self._last_quote
            snap = self._snapshot(meta["id"])
            aid = self.j.record_attempt(
                meta["id"], meta["name"], "BUY", q.qty, q.buy_px, horizon_h=q.horizon_h,
                avg_low=snap["avg_low"], avg_high=snap["avg_high"],
                vol_1h_binding=snap["vol_1h_binding"], pred_p_fill=q.p_buy,
                pred_eta_h=q.t_buy_h, pred_ev=q.ev)
            print(f"  recorded BUY {q.qty:,} {meta['name']} @ {q.buy_px:,}  [attempt {aid}] — "
                  f"auto-reconciled when it fills")
            return
        if len(args) < 4 or args[-3].lower() not in ("buy", "sell") \
                or not args[-2].isdigit() or not args[-1].isdigit():
            print("  usage: placed <item> <buy|sell> <qty> <price>")
            return
        side, qty, px = args[-3].lower(), int(args[-2]), int(args[-1])
        meta = self.resolve(" ".join(args[:-3]))
        if not meta:
            print("  item not found")
            return
        snap = self._snapshot(meta["id"])
        aid = self.j.record_attempt(
            meta["id"], meta["name"], side, qty, px, horizon_h=1.0, avg_low=snap["avg_low"],
            avg_high=snap["avg_high"], vol_1h_binding=snap["vol_1h_binding"])
        print(f"  recorded {side.upper()} {qty:,} {meta['name']} @ {px:,}  [attempt {aid}]")

    def cmd_calibrate(self, args: list[str]) -> None:
        """Measure empirical β + fill correction from your real order attempts (report only)."""
        from . import calibration
        rows = self.j.calibration_rows()
        n = len(rows)
        if n < 10:
            print(f"  only {n} resolved attempt(s) — need ~10+ for a meaningful read.")
            print("  type `placed` after you place a recommended order; fills reconcile automatically.")
            return
        beta = calibration.calibrate_beta(rows, prior=config.BETA)
        fill = calibration.calibrate_fill(rows)
        print(f"  === calibration ({n} resolved attempts) ===")
        gm = beta["global_measured"]
        if gm is None:
            print(f"  β (spread haircut): no in-spread fills yet (prior {config.BETA:.2f})")
        else:
            print(f"  β (spread haircut): prior {config.BETA:.2f} · measured {gm:.2f} "
                  f"→ use {beta['global']:.2f}  (shrunk toward prior)")
        for name in ("low", "med", "high"):
            b = beta["buckets"].get(name)
            if b:
                print(f"    {name:>4} liquidity: measured {b['measured']:.2f} "
                      f"→ {b['shrunk']:.2f}  (n={b['n']})")
        if fill["correction"] is not None:
            c = fill["correction"]
            verdict = "too pessimistic" if c > 1.1 else "too optimistic" if c < 0.9 else "well-calibrated"
            print(f"  fill rate: ×{c:.2f} ({verdict}, n={fill['n']})")
        print("  report only — nothing applied. Update BETA in config.py if you trust the measured value.")

    def cmd_port(self, args: list[str]) -> None:
        cash = int(self.j.cash()) or config.BANKROLL
        held = self.j.positions()
        rl = runelite.read()
        offers = runelite.active_offers(rl) if rl else []
        active_ids = [o.item_id for o in offers]
        if args and args[0].isdigit():
            free, source = int(args[0]), "specified"
        elif rl is not None:
            free, source = runelite.free_slots(rl, config.GE_SLOTS), "runelite"
        else:
            free, source = max(0, config.GE_SLOTS - len(held)), "assumed"
        # don't recommend what you already hold OR already have an offer on
        exclude = [h.item_id for h in held] + active_ids
        print(f"  building portfolio for {free} free slot(s)…")
        picks, idle = scanner.build_portfolio(
            bankroll=cash, held_ids=exclude, free_slots=free, limit_used=self._limit_used(rl))
        print(alert.format_portfolio(picks, cash, held, idle, free_slots=free, slot_source=source))
        active_sell_ids = {o.item_id for o in offers if not o.is_buy}
        sell = alert.format_sell_plan(self._sell_plan(held, active_sell_ids))
        if sell:
            print(sell)
        nudge = self._attention_nudge()
        if nudge:
            print(nudge)

    def _limit_used(self, rl: dict | None = None) -> dict[int, int]:
        """Prefer RuneLite's exact buy-limit counter; fall back to journal-summed buys."""
        rl = rl if rl is not None else runelite.read()
        return runelite.limit_used(rl) if rl else self.j.buy_limit_used()

    def _autosync(self) -> int:
        """Mirror RuneLite's completed fills into the journal and reconcile them against placed
        attempts. Idempotent → safe to call often."""
        rl = runelite.read()
        if not rl:
            self.j.expire_stale_attempts(int(time.time()))
            return 0
        n = 0
        for f in runelite.completed_offers(rl):
            if self.j.import_offer(f.uuid, f.item_id, f.name, f.is_buy, f.qty, f.price):
                n += 1
                self.j.reconcile_fill(f.item_id, f.is_buy, f.qty, f.price,
                                      int(f.t_ms / 1000) or int(time.time()))
        self.j.expire_stale_attempts(int(time.time()))
        return n

    def cmd_sync(self, args: list[str]) -> None:
        n = self._autosync()
        print(f"  synced {n} new fill(s) from RuneLite · cash {self.j.cash():,.0f} · "
              f"realised {self.j.realized_pnl():+,.0f}")

    def _review_offers(self) -> list[tuple]:
        """For each live offer: (offer, verdict, elapsed_h, eta_h, progress)."""
        from .tax import post_tax_received
        rl = runelite.read()
        offers = runelite.active_offers(rl) if rl else []
        if not offers:
            return []
        hourly, latest = api.one_hour(), api.latest()
        now_ms = int(time.time() * 1000)
        out = []
        for o in offers:
            v = hourly.get(o.item_id, {})
            vol = (v.get("lowPriceVolume") if o.is_buy else v.get("highPriceVolume")) or 0
            rate = config.ALPHA * vol
            eta_h = o.qty / rate if rate > 0 else float("inf")
            elapsed_h = (now_ms - o.started_ms) / 3_600_000 if o.started_ms else 0.0
            prog = o.filled / o.qty if o.qty else 0.0
            verdict = runelite.review_verdict(o.state, prog, elapsed_h, eta_h)
            # market-moved check (buys): is the round-trip margin still there at live prices?
            if verdict != "collect" and o.is_buy:
                lo = latest.get(o.item_id, {})
                lbid, lask = lo.get("low"), lo.get("high")
                if lbid and lask:
                    live_net = post_tax_received(lask, item_id=o.item_id) - lbid
                    abid, aask = v.get("avgLowPrice"), v.get("avgHighPrice")
                    avg_net = post_tax_received(aask, item_id=o.item_id) - abid if (abid and aask) else None
                    if runelite.margin_collapsed(live_net, avg_net):
                        verdict = "margin"
            out.append((o, verdict, elapsed_h, eta_h, prog))
        return out

    def cmd_review(self, args: list[str]) -> None:
        rows = self._review_offers()
        if not rows:
            print("  no active offers (or no RuneLite data)")
            return
        names = {r["id"]: r["name"] for r in api.mapping()}
        print(f"  {'slot':4} {'item':22} {'side':4} {'prog':>5} {'elapsed':>8} {'expETA':>7}  verdict")
        for o, v, elapsed_h, eta_h, prog in sorted(rows, key=lambda x: x[0].slot):
            text, c = _VERDICTS[v]
            eta_s = f"{eta_h:.1f}h" if eta_h < 100 else "—"
            print(f"  {o.slot:<4} {str(names.get(o.item_id, o.item_id))[:22]:22} {'BUY' if o.is_buy else 'SELL':4} "
                  f"{prog:>4.0%} {elapsed_h:>7.1f}h {eta_s:>7}  {alert.color(text, c) if c else text}")
        print("  (advice is time/progress-based — RuneLite doesn't expose your pending offer price)")

    def _sell_plan(self, held: list, active_sell_ids: set) -> list[dict]:
        """Recommended sell price + expected profit for each held item not already listed."""
        from .tax import post_tax_received
        hourly, latest = api.one_hour(), api.latest()
        rows = []
        for p in held:
            if p.item_id in active_sell_ids:
                continue
            h = hourly.get(p.item_id, {})
            ask = h.get("avgHighPrice") or (latest.get(p.item_id, {}) or {}).get("high")
            if not ask:
                continue
            sell_px = int(round(ask))
            net = post_tax_received(sell_px, item_id=p.item_id) - p.avg_cost
            hv = h.get("highPriceVolume") or 0
            eta_h = p.qty / (config.ALPHA * hv) if hv > 0 else float("inf")
            rows.append({"name": p.name, "qty": p.qty, "avg_cost": p.avg_cost,
                         "sell_px": sell_px, "profit": net * p.qty, "eta_h": eta_h})
        return rows

    def _attention_nudge(self) -> str:
        """A coloured one-liner if any active offer needs re-pricing/collecting."""
        verds = [v for (_o, v, *_rest) in self._review_offers()]
        parts = []
        if (n := verds.count("margin")):
            parts.append(alert.color(f"{n} margin-gone", "red"))
        if (n := verds.count("stale")):
            parts.append(alert.color(f"{n} to re-price", "red"))
        if (n := verds.count("slow")):
            parts.append(alert.color(f"{n} slow", "yellow"))
        if (n := verds.count("collect")):
            parts.append(alert.color(f"{n} to collect", "yellow"))
        return "  active orders: " + ", ".join(parts) + " — run `review`" if parts else ""

    def cmd_brief(self, args: list[str]) -> None:
        """Schedule-aware: active hours → day plan (port); off-hours → overnight plan."""
        from datetime import datetime
        hour = datetime.now().hour
        if config.AWAKE_START <= hour < config.AWAKE_END:
            print(f"  [{hour:02d}:00] active hours — day plan:")
            self.cmd_port([])
        else:
            print(f"  [{hour:02d}:00] off-hours — overnight plan:")
            self.cmd_overnight([])

    def cmd_overnight(self, args: list[str]) -> None:
        """Overnight plan. No arg → diversified buys across all free slots; <item> → one big buy."""
        cash = int(self.j.cash()) or config.BANKROLL
        if cash <= 0:
            print("  set your cash first:  bank <gp>")
            return
        if args:
            self._overnight_single(" ".join(args), cash)
        else:
            self._overnight_plan(cash)

    def _overnight_plan(self, cash: int) -> None:
        """One big buy per FREE slot (you can't cycle slots while asleep), diversified,
        cushioned, sized to fill over ~8h. Splits the whole pile across the free slots."""
        from .quote import optimal_quote
        held = self.j.positions()
        rl = runelite.read()
        offers = runelite.active_offers(rl) if rl else []
        free = runelite.free_slots(rl, config.GE_SLOTS) if rl is not None else max(0, config.GE_SLOTS - len(held))
        if free <= 0:
            print("  no free slots — collect or cancel an offer first, then `overnight`")
            return
        exclude = {h.item_id for h in held} | {o.item_id for o in offers}
        limit_used = self._limit_used(rl)
        mapping = {r["id"]: r for r in api.mapping()}
        df = scanner.scan(mode="offline", bankroll=cash, top=40, limit_used=limit_used)
        rows, remaining, slots_left = [], float(cash), free
        for _, r in df.iterrows():
            if slots_left <= 0:
                break
            iid = int(r["item_id"])
            if iid in exclude or r.get("margin_fast", 1) <= 0 or r.get("margin_pct", 0) < config.OVERNIGHT_MIN_MARGIN:
                continue
            meta = mapping.get(iid, {})
            bid = api.one_hour().get(iid, {}).get("avgLowPrice") or api.latest().get(iid, {}).get("low")
            if not bid:
                continue
            budget = remaining / slots_left  # fair share of the pile, with spillover
            cap = max(0, (meta.get("limit") or 0) * 2 - limit_used.get(iid, 0))  # ~2 buy-limit windows
            qty = min(cap or 10**9, int(budget // bid))
            q = optimal_quote(iid, qty, name=r["name"], horizon_h=8.0) if qty > 0 else None
            if not q:
                continue
            filled = int(q.qty * q.p_buy)
            rows.append({"name": q.name, "buy": q.buy_px, "sell": q.sell_px, "qty": q.qty,
                         "deploy": q.qty * q.buy_px, "fill8h": q.p_buy, "profit": q.net_unit * filled})
            exclude.add(iid)
            remaining -= q.qty * q.buy_px
            slots_left -= 1
        print(f"  OVERNIGHT plan — ≥{config.OVERNIGHT_MIN_MARGIN:.0%} cushion, sized to fill over ~8h:")
        print(alert.format_overnight(rows, cash, free))

    def _overnight_single(self, name_or_id: str, cash: int) -> None:
        """One big buy of a named item over an ~8h horizon (~2 buy-limit windows)."""
        from .quote import optimal_quote
        meta = self.resolve(name_or_id)
        if not meta:
            print("  item not found")
            return
        iid, name = meta["id"], meta.get("name", str(meta["id"]))
        h1v, latv = api.one_hour().get(iid, {}), api.latest().get(iid, {})
        bid = h1v.get("avgLowPrice") or latv.get("low")
        if not bid:
            print(f"  no live price for {name}")
            return
        cap = max(0, (meta.get("limit") or 0) * 2 - self._limit_used().get(iid, 0))  # ~2 windows overnight
        qty = min(cap or 10**9, cash // int(bid))
        if qty <= 0:
            print(f"  {name}: buy limit reached or not enough cash")
            return
        q = optimal_quote(iid, qty, name=name, horizon_h=8.0)
        if not q:
            print(f"  {name}: no profitable overnight quote")
            return
        filled = int(q.qty * q.p_buy)
        margin_pct = q.net_unit / q.buy_px if q.buy_px else 0
        print(f"  OVERNIGHT (~8h) — {name}")
        print(f"    BUY  {q.qty:,} @ {q.buy_px:,}   (~{q.p_buy:.0%} ≈ {filled:,} fill overnight, ties up ~{q.qty * q.buy_px:,} gp)")
        print(f"    AM   collect + SELL @ {q.sell_px:,}   → ~{q.net_unit * filled:,} gp profit (net {q.net_unit}/unit, {margin_pct:.1%})")
        if margin_pct < config.OVERNIGHT_MIN_MARGIN:
            print(alert.color(f"    ⚠ thin margin ({margin_pct:.1%}) — risky to leave overnight; a small dip could go red", "red"))

    def cmd_orders(self, args: list[str]) -> None:
        rl = runelite.read()
        if not rl:
            print("  no RuneLite data found (~/.runelite/flipping/) — is Flipping Utilities tracking?")
            return
        offers = runelite.active_offers(rl)
        occ, free = runelite.occupied_slots(rl), runelite.free_slots(rl, config.GE_SLOTS)
        names = {r["id"]: r["name"] for r in api.mapping()}
        print(f"  GE slots: {occ} occupied, {free} free (of {config.GE_SLOTS})")
        for o in sorted(offers, key=lambda x: x.slot):
            side = "BUY " if o.is_buy else "SELL"
            print(f"  slot {o.slot}  {side} {str(names.get(o.item_id, o.item_id))[:22]:22} x{o.qty:<6} {o.state}")

    def cmd_sellquote(self, args: list[str]) -> None:
        if not args:
            print("  usage: sellquote <item> [qty]")
            return
        qty_override = None
        if len(args) > 1 and args[-1].isdigit():  # trailing number = quantity to quote
            qty_override, args = int(args[-1]), args[:-1]
        meta = self.resolve(" ".join(args))
        if not meta:
            print("  item not found")
            return
        pos = self.j.position(meta["id"])
        if not pos or pos.qty <= 0:
            print(f"  you don't hold {meta.get('name', '?')} — sellquote is for inventory you own")
            return
        qty = qty_override or pos.qty
        from .quote import sell_frontier
        rows = sell_frontier(meta["id"], qty, pos.avg_cost)
        print(alert.format_sell_quote(meta["name"], qty, pos.avg_cost, rows))

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
            print(f"  {t['side']:4} {t['qty']:>8,} {t['name'][:22]:22} @ {t['price']:>7,} {tag}")

    def cmd_reload(self, args: list[str]) -> None:
        """Re-exec the terminal in place to pick up new code (DB/state persists)."""
        import os
        import sys
        print("  reloading…")
        try:
            self.j.con.close()  # release the DB before the new process opens it
        except Exception:
            pass
        os.execv(sys.executable, [sys.executable, "-m", "osrs_flipper.cli", "trade"])

    def cmd_update(self, args: list[str]) -> None:
        """git pull the latest, then reload — over-the-air update without quitting."""
        import subprocess
        from pathlib import Path
        repo = Path(__file__).resolve().parent.parent
        print("  pulling latest…")
        try:
            r = subprocess.run(["git", "-C", str(repo), "pull", "--ff-only"],
                               capture_output=True, text=True, timeout=60)
        except Exception as e:
            print(f"  pull failed: {e}")
            return
        lines = (r.stdout or r.stderr).strip().splitlines()
        print("  " + (lines[-1] if lines else "done"))
        if r.returncode == 0:
            self.cmd_reload(args)
        else:
            print("  pull failed — not reloading")

    def cmd_bank(self, args: list[str]) -> None:
        if not args or not args[0].replace("_", "").isdigit():
            print(f"  current cash: {self.j.cash():,.0f}  (set with `bank <amount>`)")
            return
        self.j.set_cash(float(args[0].replace("_", "")))
        print(f"  cash set to {self.j.cash():,.0f}")

    # --- loop ----------------------------------------------------------------
    def run(self) -> None:
        print("osrs-flipper terminal — type `help`, `quit` to exit")
        rl0 = runelite.read()
        for w in runelite.schema_health(rl0):
            print(alert.color(f"  ⚠ RuneLite data looks wrong: {w}. Slot/limit advice may be unsafe — "
                              f"pass free slots explicitly (`port <n>`).", "red"))
        n0 = self._autosync()
        if n0:
            print(f"  (auto-synced {n0} fill(s) from RuneLite)")
        handlers = {
            "scan": lambda a: self.cmd_scan(a), "quote": lambda a: self.cmd_quote(a),
            "sellquote": lambda a: self.cmd_sellquote(a), "sq": lambda a: self.cmd_sellquote(a),
            "buy": lambda a: self._trade(a, "buy"), "sell": lambda a: self._trade(a, "sell"),
            "placed": lambda a: self.cmd_placed(a),
            "calibrate": lambda a: self.cmd_calibrate(a), "calib": lambda a: self.cmd_calibrate(a),
            "port": lambda a: self.cmd_port(a), "portfolio": lambda a: self.cmd_port(a),
            "orders": lambda a: self.cmd_orders(a), "ge": lambda a: self.cmd_orders(a),
            "review": lambda a: self.cmd_review(a), "check": lambda a: self.cmd_review(a),
            "sync": lambda a: self.cmd_sync(a),
            "overnight": lambda a: self.cmd_overnight(a), "night": lambda a: self.cmd_overnight(a),
            "brief": lambda a: self.cmd_brief(a), "now": lambda a: self.cmd_brief(a),
            "pos": lambda a: self.cmd_pos(), "positions": lambda a: self.cmd_pos(),
            "pnl": lambda a: self.cmd_pnl(), "recent": lambda a: self.cmd_recent(a),
            "preds": lambda a: self.cmd_preds(a),
            "bank": lambda a: self.cmd_bank(a),
            "update": lambda a: self.cmd_update(a), "reload": lambda a: self.cmd_reload(a),
            "help": lambda a: print(__doc__),
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
            if cmd != "sync":  # mirror RuneLite fills before any command (sync reports its own count)
                synced = self._autosync()
                if synced:
                    print(f"  (auto-synced {synced} new fill(s) from RuneLite)")
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
