"""Interactive trading terminal — run it and drive everything without spending tokens.

    osrs-flipper trade

Commands (type `help`):
  go | (press Enter)       ★ THE one command: offers+verdicts, what to sell, what to buy, next action
  ---- drill-downs (go shows all of these together) ----
  sync                    import completed RuneLite fills into the journal
  orders | ge             live GE slots + active offers (from RuneLite)
  review | check          flag active offers to re-price / cancel / collect
  port [free_slots]        recommended allocation (free slots auto-read from RuneLite)
  overnight [item]         plan one big ~8h buy to leave while you sleep
  scan [n] [online|offline|balanced]   ranked live flips (mode sets speed-vs-margin)
  anomaly | manip          price dislocations on abnormal volume (pumps to avoid / dumps to revert-buy)
  why <item>               explain an item's price: live vs recent baselines, volume z, falling-knife check
  quote <item> [qty]       solve optimal buy/sell prices for an item
  sellquote | sq <item> [qty]  sell-price tradeoff for held inventory (fill time vs profit)
  buy <item> <quantity> <price>    log a buy fill
  sell <item> <quantity> <price>   log a sell fill (applies GE tax)
  placed [item buy|sell qty price]  log a PLACED order (or the last quote) for fill calibration
  calibrate | calib        measure empirical β + fill correction from your real attempts
  pos                      open positions + unrealised P&L (vs live bid)
  inv | inventory          holdings split: bank (sellable) vs in-GE (listed / buying)
  pnl                      realised P&L, cash, equity, bond progress
  progress | chart         net-worth chart (realized + live equity) projected to 10M/100M
  recent [n]               recent trades
  preds [n]                logged model predictions (for calibration)
  bank <amount>            set your current cash balance
  alerts [on|off|test]     background Discord push when an offer needs you (auto-on if webhook set)
  update                   git pull latest + reload (OTA, no manual restart)
  reload                   re-exec to pick up code changes (keeps your DB/state)
  help | quit
"""

from __future__ import annotations

import threading
import time

from . import alert, api, config, monitor, runelite, scanner
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
        self._alert_stop = threading.Event()
        self._alert_thread: threading.Thread | None = None

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
        active_sell_ids = {o.item_id for o in offers if not o.is_buy}
        sell_rows = self._sell_plan(held, active_sell_ids)
        buy_slots = max(0, free - len(sell_rows))  # reserve a slot per pending sell listing
        if sell_rows:
            print(alert.format_sell_plan(sell_rows))
        print(f"  building portfolio for {buy_slots} free slot(s)"
              + (f" ({len(sell_rows)} reserved for sells)…" if sell_rows else "…"))
        picks, idle = scanner.build_portfolio(
            bankroll=cash, held_ids=exclude, free_slots=buy_slots, limit_used=self._limit_used(rl))
        print(alert.format_portfolio(picks, cash, held, idle, free_slots=buy_slots, slot_source=source))
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
        self._autodetect_placements(rl)
        self.j.expire_stale_attempts(int(time.time()))
        return n

    def _autodetect_placements(self, rl: dict) -> int:
        """Record any live pending offer not already tracked as an open attempt — so placing in
        game auto-logs it for calibration without typing `placed`. Only BUYING/SELLING (pending)
        offers: a BOUGHT/SOLD offer is a completed fill, already imported above. Idempotent —
        keyed on (item_id, side), so re-running never double-records."""
        open_keys = {(a["item_id"], a["side"]) for a in self.j.open_attempts()}
        names = None
        n = 0
        for o in runelite.active_offers(rl):
            # only pending offers with a real price: RuneLite reports price 0 until an offer
            # starts filling, so we log it (at its true price) once it does — a price-0 attempt
            # would poison β calibration. BOUGHT/SOLD are completed fills, imported above.
            if o.state not in ("BUYING", "SELLING") or o.price <= 0:
                continue
            side = "BUY" if o.is_buy else "SELL"
            if (o.item_id, side) in open_keys:
                continue
            if names is None:
                names = {r["id"]: r["name"] for r in api.mapping()}
            snap = self._snapshot(o.item_id)
            self.j.record_attempt(o.item_id, names.get(o.item_id, str(o.item_id)), side, o.qty,
                                  o.price, horizon_h=1.0, avg_low=snap["avg_low"],
                                  avg_high=snap["avg_high"], vol_1h_binding=snap["vol_1h_binding"])
            open_keys.add((o.item_id, side))
            n += 1
        if n:
            print(f"  (auto-logged {n} placed order(s) from RuneLite — no need to type `placed`)")
        return n

    def cmd_sync(self, args: list[str]) -> None:
        n = self._autosync()
        print(f"  synced {n} new fill(s) from RuneLite · cash {self.j.cash():,.0f} · "
              f"realised {self.j.realized_pnl():+,.0f}")

    def _review_offers(self) -> list[tuple]:
        """For each live offer: (offer, verdict, elapsed_h, eta_h, progress). Shares the pure
        verdict logic with the background alert watcher (monitor.review_offers)."""
        rl = runelite.read()
        offers = runelite.active_offers(rl) if rl else []
        if not offers:
            return []
        return monitor.review_offers(offers, api.one_hour(), api.latest(), int(time.time() * 1000))

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
        from .tax import breakeven_sell, post_tax_received
        hourly, latest = api.one_hour(), api.latest()
        rows = []
        for p in held:
            if p.item_id in active_sell_ids:
                continue
            h = hourly.get(p.item_id, {})
            ask = h.get("avgHighPrice") or (latest.get(p.item_id, {}) or {}).get("high")
            if not ask:
                continue
            # never list under cost: when the market drops below your break-even, hold the listing
            # at break-even rather than dump at a loss just because the average ticked down.
            be = breakeven_sell(p.avg_cost, item_id=p.item_id)
            market_px = int(round(ask))
            sell_px = max(market_px, be)
            underwater = market_px < be
            net = post_tax_received(sell_px, item_id=p.item_id) - p.avg_cost
            hv = h.get("highPriceVolume") or 0
            # a break-even listing sits ABOVE market, so it won't fill at market volume — show
            # "won't fill (yet)" rather than an ETA computed as if it were priced at the market.
            eta_h = float("inf") if underwater else (p.qty / (config.ALPHA * hv) if hv > 0 else float("inf"))
            rows.append({"name": p.name, "qty": p.qty, "avg_cost": p.avg_cost, "sell_px": sell_px,
                         "profit": net * p.qty, "eta_h": eta_h, "underwater": underwater})
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

    def cmd_go(self, args: list[str]) -> None:
        """THE one command — everything on one screen so you don't juggle review/port/pos/brief:
        active offers + verdicts, what to sell, what to buy with free slots, and a single NEXT
        action. Runway-aware: by day → patient cyclable flips; near bedtime/overnight → big
        cushioned buys safe to leave (switches NIGHT_SWITCH_H before AWAKE_END). Alias: Enter."""
        from datetime import datetime
        hour = datetime.now().hour
        cash = int(self.j.cash()) or config.BANKROLL
        held = self.j.positions()
        rl = runelite.read()
        offers = runelite.active_offers(rl) if rl else []
        free = (runelite.free_slots(rl, config.GE_SLOTS) if rl is not None
                else max(0, config.GE_SLOTS - len(held)))
        bp = scanner.bond_progress(cash) if not config.MEMBERS else {}
        pct = f" · {bp['pct']:.0f}% to bond" if bp.get("pct") else ""
        # runway to bed picks the plan: a flip placed now must round-trip (buy + sell) before
        # you sleep to be a "day" flip; within NIGHT_SWITCH_H of bedtime it'd be left overnight,
        # so hand over the overnight plan (fat-margin holds safe to leave) instead.
        awake = config.AWAKE_START <= hour < config.AWAKE_END
        hours_until_sleep = config.AWAKE_END - hour if awake else 0
        daytime = awake and hours_until_sleep > config.NIGHT_SWITCH_H
        if daytime:
            regime = "☀️ day"
        elif awake:
            regime = f"\U0001f319 winding down ({hours_until_sleep:.0f}h to sleep)"
        else:
            regime = "\U0001f4a4 overnight"
        print(f"  === {hour:02d}:00 · cash {cash:,} · {len(held)} held · "
              f"{free}/{config.GE_SLOTS} slots free{pct} · {regime} ===")
        if held and rl is not None:  # split holdings into bank (sellable) vs tied up in GE
            sp = runelite.holdings_split(held, offers)
            nb = sum(1 for h in sp.values() if h["bank"] > 0)
            nl = sum(1 for h in sp.values() if h["listed"] > 0)
            ni = sum(1 for h in sp.values() if h["incoming"] > 0)
            print(f"  holdings: {nb} in bank · {nl} listed in GE · {ni} buying  (`inv` for detail)")

        rows = self._review_offers()  # ACTIVE offers + verdicts (the old `review`)
        refined = []  # verdicts re-checked against a fresh quote so we never churn a priced-right offer
        if rows:
            names = {r["id"]: r["name"] for r in api.mapping()}
            print("  ACTIVE OFFERS:")
            for o, v, elapsed_h, eta_h, prog in sorted(rows, key=lambda x: x[0].slot):
                hint = ""
                if v in ("margin", "stale", "slow"):
                    v, hint = self._refine_verdict(o, v)
                text, c = _VERDICTS[v]
                eta_s = f"{eta_h:.1f}h" if eta_h < 100 else "—"
                print(f"    {o.slot:<2} {str(names.get(o.item_id, o.item_id))[:18]:18} "
                      f"{'BUY' if o.is_buy else 'SELL':4} {prog:>4.0%} {elapsed_h:>5.1f}h "
                      f"{eta_s:>6}  {alert.color(text, c) if c else text}")
                if hint:
                    print(hint)
                refined.append((o, v, elapsed_h, eta_h, prog))

        active_sell_ids = {o.item_id for o in offers if not o.is_buy}  # SELL holdings (the old port tail)
        sell_rows = self._sell_plan(held, active_sell_ids)
        if sell_rows:
            print(alert.format_sell_plan(sell_rows))

        picks: list[dict] = []  # BUY plan for free slots (the old `port` / `overnight`)
        # reserve a slot for each pending sell listing — a sell occupies a GE slot too, so buys
        # can't claim every free slot or you'd have nowhere to list what you're holding.
        buy_slots = max(0, free - len(sell_rows))
        if buy_slots > 0 and daytime:
            if sell_rows:
                print(f"  ({len(sell_rows)} slot(s) reserved for the sell listing(s) above)")
            exclude = [h.item_id for h in held] + [o.item_id for o in offers]
            picks, idle = scanner.build_portfolio(bankroll=cash, held_ids=exclude,
                                                  free_slots=buy_slots, limit_used=self._limit_used(rl))
            src = "runelite" if rl is not None else "assumed"
            print(alert.format_portfolio(picks, cash, held, idle, free_slots=buy_slots, slot_source=src))
            self._explain_picks(picks)  # one-line "why" for each buy you're about to place
        elif buy_slots > 0:
            self._overnight_plan(cash)

        print("  " + alert.color("NEXT: " + self._next_action(refined, sell_rows, free, picks), "bold"))

    def _explain_picks(self, picks: list, limit: int = 5) -> None:
        """Print a one-line price-position 'why' for each buy being placed now (bounded — one
        /timeseries fetch each). Silent on any error so it never breaks the dashboard."""
        from . import anomaly
        lat, hr = api.latest(), api.one_hour()
        shown = 0
        for i, p in enumerate(picks, 1):
            if p.get("place_at_h", 0) != 0 or shown >= limit:
                continue
            try:
                a = anomaly.assess(p["item_id"], lat, hr, api.timeseries)
                print(f"     why #{i} {str(p['name'])[:18]:18} {anomaly.summary_line(a)}")
                shown += 1
            except Exception:  # noqa: BLE001 — explanation is best-effort, never fatal
                pass

    @staticmethod
    def _next_action(review_rows: list, sell_rows: list, free: int, picks: list) -> str:
        """The single most important thing to do right now, synthesized from current state."""
        verds = [v for (_o, v, *_rest) in review_rows]
        if (n := verds.count("collect")):
            return f"collect {n} finished offer(s) — frees a slot, then press Enter again"
        if "margin" in verds or "stale" in verds:
            return "re-price the flagged offer(s): cancel & re-quote (see ACTIVE OFFERS)"
        if free > 0 and (picks or sell_rows):
            actions = []
            if sell_rows:
                actions.append(f"list {len(sell_rows)} sell(s)")
            n_now = sum(1 for p in picks if p.get("place_at_h", 0) == 0)
            if n_now:
                actions.append(f"place buy #1–#{n_now}")
            if actions:
                return " + ".join(actions) + " now (auto-logged from RuneLite — no `placed` needed)"
        if sell_rows:
            return "list your holdings for sale (see SELL above)"
        if review_rows:
            etas = [eta for (_o, _v, _e, eta, _p) in review_rows if eta < 100]
            wait = f"~{min(etas) * 60:.0f}m" if etas else "a while"
            return f"all slots working — nothing to do; check back in {wait}"
        return "idle — set cash with `bank <gp>`, or `scan` for ideas"

    @staticmethod
    def _refine_verdict(o, verdict: str) -> tuple[str, str]:
        """Consult fresh prices on a flagged offer so the advice is actionable, not churn:
          - priced right, just slow   → downgrade to on-track (no pointless cancel/re-list)
          - genuinely mispriced        → keep the flag, show the price to move to
          - no profitable spread (buy) → keep the flag, say cancel & redeploy
        Returns (possibly-downgraded verdict, indented hint line)."""
        # RuneLite reports the offer price as 0 until it starts filling — for an unfilled offer
        # we genuinely don't know your price, so we can't say it's mispriced, only show the market.
        known = o.price > 0
        if o.is_buy:
            from .quote import optimal_quote
            from .tax import post_tax_received
            q = optimal_quote(o.item_id, max(1, o.qty - o.filled), horizon_h=1.0)
            if not q:
                return verdict, alert.color("         → no profitable spread now — cancel & redeploy that cash", "yellow")
            if not known:  # market spread exists → most likely just slow; show it to compare against
                return "slow", alert.color(f"         → market now: buy {q.buy_px:,} / sell {q.sell_px:,} (net {q.net_unit}/ea) — fine if your bid ≥ {q.buy_px:,}", "yellow")
            # known price: fine if at/above the competitive buy AND still profitable to sell into;
            # only under-bidding (won't fill) or over-paying (no margin) needs a re-quote.
            net_at_mine = post_tax_received(q.sell_px, item_id=o.item_id) - o.price
            if o.price >= q.buy_px and net_at_mine > 0:
                return "ontrack", alert.color(f"         → your bid {o.price:,} still clears (sell ~{q.sell_px:,}, net {net_at_mine}/ea) — just slow; hold", "green")
            return verdict, alert.color(f"         → re-quote: buy {q.buy_px:,} / sell {q.sell_px:,}  (net {q.net_unit}/ea)", "bold")
        # SELL: compare against the current market ask.
        ask = api.latest().get(o.item_id, {}).get("high") or api.one_hour().get(o.item_id, {}).get("avgHighPrice")
        if not ask:
            return verdict, ""
        ask = int(round(ask))
        if not known:
            return "slow", alert.color(f"         → market ask {ask:,} — fine if you're listed ≤ {ask:,}", "yellow")
        if o.price <= ask:
            return "ontrack", alert.color(f"         → listed {o.price:,} ≤ market {ask:,} — priced to sell, just slow; hold", "green")
        return verdict, alert.color(f"         → re-list nearer {ask:,} — you're above market", "bold")

    # --- background Discord alerts -------------------------------------------
    def _alerts_running(self) -> bool:
        return self._alert_thread is not None and self._alert_thread.is_alive()

    def _start_alerts(self) -> bool:
        """Spawn the daemon watcher (read-only: RuneLite + API, never the journal → no DB lock)."""
        if not config.DISCORD_WEBHOOK_URL or self._alerts_running():
            return self._alerts_running()
        self._alert_stop.clear()
        self._alert_thread = threading.Thread(target=monitor.watch_loop, args=(self._alert_stop,), daemon=True)
        self._alert_thread.start()
        return True

    def _stop_alerts(self) -> None:
        self._alert_stop.set()
        self._alert_thread = None

    def cmd_alerts(self, args: list[str]) -> None:
        """Background Discord alerts when an offer needs you (filled/margin-gone/stale).
          alerts           status      alerts on|off    toggle      alerts test    send a test ping"""
        sub = args[0].lower() if args else "status"
        if sub == "on":
            if not config.DISCORD_WEBHOOK_URL:
                print("  no webhook — set OSRS_FLIPPER_DISCORD_WEBHOOK, then `reload`")
            else:
                self._start_alerts()
                print(f"  alerts ON — polling RuneLite every {config.ALERT_POLL_S}s, pushing to Discord")
        elif sub == "off":
            self._stop_alerts()
            print("  alerts OFF")
        elif sub == "test":
            ok, detail = alert.post_discord("\U0001f514 osrs-flipper test alert — you're wired up.")
            print(f"  discord test: {detail}")
        else:
            state = "ON" if self._alerts_running() else "OFF"
            hook = "configured" if config.DISCORD_WEBHOOK_URL else "NOT set (OSRS_FLIPPER_DISCORD_WEBHOOK)"
            print(f"  alerts {state} · webhook {hook} · poll {config.ALERT_POLL_S}s")
            print("  `alerts on|off` to toggle · `alerts test` to verify the webhook")

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
        # reserve a slot for each holding still needing a sell listing — a sell occupies a slot too
        active_sell_ids = {o.item_id for o in offers if not o.is_buy}
        pending_sells = sum(1 for h in held if h.item_id not in active_sell_ids)
        free = max(0, free - pending_sells)
        if free <= 0:
            print(f"  all free slot(s) reserved for {pending_sells} sell listing(s) — list those first")
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
        bond = lat.get(_BOND, {}).get("high") if not config.MEMBERS else None
        print(f"  cash:        {self.j.cash():>14,.0f}")
        print(f"  inventory:   {self.j.inventory_value(bids):>14,.0f}")
        print(f"  equity:      {equity:>14,.0f}")
        print(f"  realised P&L:{self.j.realized_pnl():>+14,.0f}")
        if bond:
            print(f"  bond:        {bond:>14,.0f}  ({equity / bond * 100:.1f}% — {bond - equity:,.0f} to go)")

    def cmd_inventory(self, args: list[str]) -> None:
        """What you actually hold, split BANK (sellable now) vs IN-GE (listed for sale / being
        bought), reconciled from your transactions against live RuneLite offers. Alias: inv."""
        rl = runelite.read()
        if rl is None:
            print("  no RuneLite data — `pos` shows total holdings; can't split bank vs GE without it")
            return
        split = runelite.holdings_split(self.j.positions(), runelite.active_offers(rl))
        if not split:
            print("  nothing held or in GE")
            return
        names = {r["id"]: r["name"] for r in api.mapping()}
        print(f"  {'item':22} {'bank':>9} {'listed(GE)':>11} {'buying(GE)':>11} {'avg':>9}")
        print("  " + "-" * 66)
        tb = tl = ti = 0
        for iid, h in sorted(split.items(), key=lambda kv: -kv[1]["total"]):
            name = h["name"] or names.get(iid, str(iid))
            drift = alert.color("  ⚠ drift", "red") if h["bank"] < 0 else ""
            print(f"  {str(name)[:22]:22} {h['bank']:>9,} {h['listed']:>11,} {h['incoming']:>11,} "
                  f"{h['avg_cost']:>9,.0f}{drift}")
            tb += max(0, h["bank"])
            tl += h["listed"]
            ti += h["incoming"]
        print("  " + "-" * 66)
        print(f"  {'TOTAL units':22} {tb:>9,} {tl:>11,} {ti:>11,}")
        print("  bank = sellable now · listed = tied up in active sell offers · buying = bought, awaiting collection")

    def cmd_why(self, args: list[str]) -> None:
        """Explain an item's price position: live vs its recent baselines (1d/2wk/3mo/30d), volume
        z-score, slope, and phase verdict — is it normal, a real dip to buy, or a falling knife?
          why <item>"""
        if not args:
            print("  usage: why <item>")
            return
        from . import anomaly
        meta = self.resolve(" ".join(args))
        if not meta:
            print("  item not found")
            return
        name = meta.get("name", str(meta["id"]))
        a = anomaly.assess(meta["id"], api.latest(), api.one_hour(), api.timeseries, deep=True)
        if a["live_mid"] is None or not a["baselines"]:
            print(f"  {name}: no live price / history to assess")
            return
        print(f"  === {name} — why ===")
        print(f"  live bid {a['live_bid']:,} / ask {a['live_ask']:,}  ·  1h avg {a['avg_low']}/{a['avg_high']}")
        print(f"  {'window':6} {'baseline':>9} {'vs live':>8}")
        for lbl in ("1d", "2wk", "3mo", "30d"):
            if lbl in a["baselines"]:
                b = a["baselines"][lbl]
                print(f"  {lbl:6} {b:>9,.0f} {(a['live_mid'] - b) / b * 100:>+7.0f}%")
        print(f"  vol_z {a['vol_z']:+.1f} (abnormal ≥{config.ANOMALY_VOL_Z_MIN:.0f})  ·  "
              f"slope {a['slope']:+.1f}  ·  phase {a['phase'] or '—'}")
        print("  → " + alert.color(anomaly.summary_line(a), "bold"))

    def cmd_anomaly(self, args: list[str]) -> None:
        """Detect price dislocations on abnormal volume — pumps (avoid / sell into) and over-dumps
        (mean-revert buy). Only the RECOVER/DUMP side is exploitable in OSRS (no shorting)."""
        from . import anomaly
        names = {r["id"]: r["name"] for r in api.mapping()}
        print("  scanning for dislocations (deep-checks top candidates — a few API calls)…")
        hits = anomaly.detect(api.latest(), api.one_hour(), names, api.timeseries)
        if not hits:
            print("  no anomalies — nothing liquid is dislocated from baseline on abnormal volume right now")
            return
        print(f"  {'phase':8} {'item':22} {'live':>8} {'baseline':>9} {'div':>7} {'vol_z':>6} {'revertEV':>9}  verdict")
        print("  " + "-" * 110)
        for h in hits[:15]:
            ev = f"{h['revert_ev_unit']:+,}" if h["div_now"] < 0 else "—"
            c = "green" if h["div_now"] < 0 and h["revert_ev_unit"] > 0 else ("red" if h["div_now"] > 0 else None)
            line = (f"  {h['phase']:8} {h['name'][:22]:22} {h['live_mid']:>8,.0f} {h['baseline']:>9,.0f} "
                    f"{h['div_now'] * 100:>+6.1f}% {h['vol_z']:>6.1f} {ev:>9}  {h['verdict']}")
            print(alert.color(line, c) if c else line)
        print("  KEY  div = live vs recent baseline · vol_z = how abnormal current volume is · "
              "revertEV = gp/unit buying live, selling back at baseline")
        print("  ⚡ only RECOVER↑/DUMP↓ (over-dumped) are exploitable here — you can't short a pump; "
              "and the buy limit caps size. Treat as opportunistic, verify before committing.")

    def cmd_progress(self, args: list[str]) -> None:
        """Net-worth progress chart: realized history + live (marked-to-market) equity, projected
        to 10M/100M at a growth rate re-fit from your own trade history. Saves + opens a PNG."""
        from . import progress
        rows = self.j.con.execute("SELECT ts, cash_delta, realized_pnl FROM ledger ORDER BY ts").fetchall()
        if len(rows) < 2:
            print("  not enough trade history yet — flip a bit, then `progress`")
            return
        lat = self.latest()
        bids = {p.item_id: lat.get(p.item_id, {}).get("low") for p in self.j.positions()}
        equity_now = self.j.equity(bids)
        initial, times, nw = progress.build_history(rows, self.j.cash())
        rate, span_days = progress.fit_daily_rate(rows, initial)
        out = "/tmp/osrs_progress.png"
        if not progress.render(out, initial=initial, times=times, networth=nw, equity_now=equity_now,
                               daily_rate=rate, span_days=span_days):
            print("  matplotlib not installed — run `pip install matplotlib`, then `reload`")
            return
        print(f"  realized +{nw[-1] - initial:,.0f} over {span_days * 24:.1f}h · "
              f"live equity {equity_now:,.0f} (unrealised {equity_now - nw[-1]:+,.0f})")
        if rate:
            for tgt, lbl in ((10e6, "10M"), (100e6, "100M")):
                d = progress.eta_days(equity_now, tgt, rate)
                if d:
                    print(f"  {lbl}: ~{d:.0f} days at the fitted {rate:.1%}/day (optimistic — decays at scale)")
        print(f"  saved {out}")
        self._open(out)

    @staticmethod
    def _open(path: str) -> None:
        """Open a file in the OS default viewer (best-effort, never raises)."""
        import subprocess
        import sys
        cmd = {"darwin": "open"}.get(sys.platform, "xdg-open")
        try:
            subprocess.Popen([cmd, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

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
        if self._start_alerts():  # auto-on when a webhook is configured
            print(f"  (Discord alerts ON — every {config.ALERT_POLL_S}s; `alerts off` to stop)")
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
            "go": lambda a: self.cmd_go(a), "g": lambda a: self.cmd_go(a),
            "brief": lambda a: self.cmd_go(a), "now": lambda a: self.cmd_go(a),
            "pos": lambda a: self.cmd_pos(), "positions": lambda a: self.cmd_pos(),
            "pnl": lambda a: self.cmd_pnl(), "recent": lambda a: self.cmd_recent(a),
            "progress": lambda a: self.cmd_progress(a), "chart": lambda a: self.cmd_progress(a),
            "anomaly": lambda a: self.cmd_anomaly(a), "anomalies": lambda a: self.cmd_anomaly(a),
            "manip": lambda a: self.cmd_anomaly(a), "why": lambda a: self.cmd_why(a),
            "inv": lambda a: self.cmd_inventory(a), "inventory": lambda a: self.cmd_inventory(a),
            "preds": lambda a: self.cmd_preds(a),
            "bank": lambda a: self.cmd_bank(a),
            "alerts": lambda a: self.cmd_alerts(a),
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
                raw = "go"  # bare Enter → the everything dashboard
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
