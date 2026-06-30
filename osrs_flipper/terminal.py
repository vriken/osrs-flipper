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
  reconcile                re-sync positions from RuneLite's full offer history (heals phantoms)
  forget <item>            untrack a holding traded elsewhere (stays gone through reconcile)
  hold <item> <qty> [avg]  track a holding acquired elsewhere (adds it, no cash spent)
  recover | recovery       underwater holdings: bounce-likely (hold/double down) vs re-rating (cut)
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

from . import alert, api, calibration, config, local_export, monitor, runelite, scanner
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
        df = scanner.scan(top=top, bankroll=bankroll, mode=mode, limit_used=self._limit_used(),
                          fill_cal=self._fill_cal())
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
        if fill["global_measured"] is not None:
            gm, g = fill["global_measured"], fill["global"]
            verdict = "too pessimistic" if g > 1.1 else "too optimistic" if g < 0.9 else "well-calibrated"
            print(f"  fill rate: measured ×{gm:.2f} → applied ×{g:.2f}  ({verdict}, n={fill['n']}, shrunk→1.0)")
            for name in ("low", "med", "high"):
                b = fill["buckets"].get(name)
                if b:
                    print(f"    {name:>4} liquidity: measured ×{b['measured']:.2f} → ×{b['shrunk']:.2f}  (n={b['n']})")
        print("  fill correction is AUTO-APPLIED to EV/ranking (go/scan/port). β is report-only — "
              "update BETA in config.py if you trust the measured value.")

    def cmd_port(self, args: list[str]) -> None:
        cash = int(self.j.cash()) or config.BANKROLL
        held = self.j.positions()
        rl = runelite.read()
        offers = self._active_offers()  # relog-proof active offers
        active_ids = [o.item_id for o in offers]
        if args and args[0].isdigit():
            free, source = int(args[0]), "specified"
        elif offers or rl is not None:
            free, source = self._free_slots(offers), "live"
        else:
            free, source = max(0, config.GE_SLOTS - len(held)), "assumed"
        # don't recommend what you already hold OR already have an offer on
        exclude = [h.item_id for h in held] + active_ids
        sell_rows = self._sell_plan(held, set(active_ids))  # skip held items with any live offer
        buy_slots = max(0, free - len(sell_rows))  # reserve a slot per pending sell listing
        if sell_rows:
            print(alert.format_sell_plan(sell_rows))
            self._explain_recovery(sell_rows)
        print(f"  building portfolio for {buy_slots} free slot(s)"
              + (f" ({len(sell_rows)} reserved for sells)…" if sell_rows else "…"))
        picks, idle = scanner.build_portfolio(
            bankroll=cash, held_ids=exclude, free_slots=buy_slots, limit_used=self._limit_used(rl),
            fill_cal=self._fill_cal())
        print(alert.format_portfolio(picks, cash, held, idle, free_slots=buy_slots, slot_source=source))
        nudge = self._attention_nudge()
        if nudge:
            print(nudge)

    def _limit_used(self, rl: dict | None = None) -> dict[int, int]:
        """Prefer RuneLite's exact buy-limit counter; fall back to journal-summed buys."""
        rl = rl if rl is not None else runelite.read()
        return runelite.limit_used(rl) if rl else self.j.buy_limit_used()

    def _fill_cal(self) -> dict:
        """Fill-rate calibration from your resolved attempts, auto-applied to the EV/ranking so the
        model self-corrects from real fills. Stays near 1.0 (shrunk) until enough fills accumulate."""
        return calibration.calibrate_fill(self.j.calibration_rows())

    def _sync_cash(self) -> tuple[int | None, int]:
        """Refresh journal cash from live coins (Local Data Exporter, item 995) and return
        (coins, tied_in_offers). Coins already reflect placed buys (gold leaves the moment you
        place a buy) and collected sells, so this keeps `cash` correct with no manual `bank`.
        No-op when the plugin/inventory snapshot isn't live — cash keeps its last value."""
        le = local_export.read()
        if not le:
            return None, 0
        c = local_export.coins(le)
        if c is not None:
            self.j.set_cash(float(c))
        return c, local_export.tied_gold(le)

    def _active_offers(self) -> list:
        """Authoritative live GE offers, robust across game restarts. The Local Data Exporter is
        primary — it reads the client's real GE offers (true listed prices, and they repopulate on
        login), so active orders no longer vanish after you close and reopen the game (Flipping
        Utilities' slotTimers don't come back cleanly in a new session). Each offer is enriched with
        FU's placement time per slot for age/ETA, and we fall back to FU entirely if the exporter
        isn't running."""
        leo = local_export.active_offers(local_export.read())
        rl = runelite.read()
        fu = runelite.active_offers(rl) if rl else []
        if not leo:
            return fu
        by_slot = {o.slot: o for o in fu}
        for o in leo:  # FU knows when each offer was placed; the exporter doesn't
            f = by_slot.get(o.slot)
            if f and f.item_id == o.item_id:
                o.started_ms = f.started_ms or o.started_ms
                o.uuid = f.uuid or o.uuid
        return leo

    def _free_slots(self, offers: list | None = None) -> int:
        """Observed free GE slots from the relog-proof offer source."""
        offers = self._active_offers() if offers is None else offers
        return max(0, config.GE_SLOTS - len(offers))

    def _autosync(self) -> int:
        """Mirror RuneLite's completed fills into the journal and reconcile them against placed
        attempts. Idempotent → safe to call often."""
        rl = runelite.read()
        if not rl:
            self.j.expire_stale_attempts(int(time.time()))
            return 0
        names = {r["id"]: r["name"] for r in api.mapping()}
        fills = runelite.all_fills(rl, names)            # completed offers + active partial sells
        self.j.migrate_fill_accounting_if_needed(fills)  # one-time baseline to current state; no-op after
        n = 0
        for f in fills:                                  # credit only the NEW units filled per offer
            delta = self.j.account_fill_delta(f.uuid, f.item_id, f.name, f.is_buy, f.qty, f.price)
            if delta > 0:
                n += 1
                self.j.reconcile_fill(f.item_id, f.is_buy, delta, f.price,
                                      int(f.t_ms / 1000) or int(time.time()))
        self._autodetect_placements()
        # authoritative position re-sync from the full offer history — heals phantoms left by
        # out-of-order incremental imports (the silent-cap bug).
        for name, old, new in self.j.reconcile_positions(fills):
            print(f"  reconciled {name}: {old:,} → {new:,} held (matched to RuneLite's offer history)")
        # bag is the final word: drop/trim positions not in your bag or GE (sells that never reached
        # this device's RuneLite). Only when the inventory + GE snapshot is live, so a blank read
        # can't wrongly clear real stock. Bank excluded — you keep stock in your bag.
        held = local_export.holdings(local_export.read())
        if held is not None:
            for name, old, new in self.j.reconcile_to_holdings(held):
                tag = "dropped (not in bag or GE)" if new == 0 else "trimmed to bag + GE"
                print(f"  {name}: {old:,} → {new:,} — {tag}")
        self._sync_cash()  # authoritative cash from live coins, if the exporter is running
        self.j.expire_stale_attempts(int(time.time()))
        return n

    def _autodetect_placements(self) -> int:
        """Record any live pending offer not already tracked as an open attempt — so placing in
        game auto-logs it for calibration without typing `placed`. Only BUYING/SELLING (pending)
        offers: a BOUGHT/SOLD offer is a completed fill, already imported above. Idempotent —
        keyed on (item_id, side), so re-running never double-records."""
        open_keys = {(a["item_id"], a["side"]) for a in self.j.open_attempts()}
        names = None
        n = 0
        for o in self._active_offers():
            # only pending offers with a real price. The Local Data Exporter carries the true listed
            # price even at 0% fill, so a placement is logged the moment you make it (Flipping
            # Utilities reports 0 until it fills); the price>0 guard still skips any FU-only fallback
            # offer whose price isn't known yet, which would poison β calibration.
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
        offers = self._active_offers()
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

    def _sell_plan(self, held: list, busy_ids: set) -> list[dict]:
        """Recommended sell price + expected profit for each held item with NO active offer.
        `busy_ids` = items with any live offer: already-listed sells (don't re-list) AND active
        buys (you're accumulating more — don't sell the partial out from under yourself)."""
        from .tax import breakeven_sell, post_tax_received
        hourly, latest = api.one_hour(), api.latest()
        rows = []
        for p in held:
            if p.item_id in busy_ids:
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
            rows.append({"item_id": p.item_id, "name": p.name, "qty": p.qty, "avg_cost": p.avg_cost,
                         "sell_px": sell_px, "profit": net * p.qty, "eta_h": eta_h,
                         "underwater": underwater})
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
        coins, tied = self._sync_cash()  # live coins on hand + gold tied up in open offers
        cash = int(self.j.cash()) or config.BANKROLL
        held = self.j.positions()
        rl = runelite.read()
        offers = self._active_offers()  # relog-proof: real GE offers, survives a restart
        free = self._free_slots(offers)
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
        cash_str = f"cash {cash:,}" + (f" (+{tied:,} in offers)" if coins is not None and tied else "")
        print(f"  === {hour:02d}:00 · {cash_str} · {len(held)} held · "
              f"{free}/{config.GE_SLOTS} slots free{pct} · {regime} ===")
        if held and offers:  # split holdings into bank (sellable) vs tied up in GE
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

        busy_ids = {o.item_id for o in offers}  # skip held items with ANY live offer (selling / accumulating)
        sell_rows = self._sell_plan(held, busy_ids)
        if sell_rows:
            print(alert.format_sell_plan(sell_rows))
            self._explain_recovery(sell_rows)  # underwater → bounce-likely (hold/double) vs cut

        picks: list[dict] = []  # BUY plan for free slots (the old `port` / `overnight`)
        # reserve a slot for each pending sell listing — a sell occupies a GE slot too, so buys
        # can't claim every free slot or you'd have nowhere to list what you're holding.
        buy_slots = max(0, free - len(sell_rows))
        if buy_slots > 0 and daytime:
            if sell_rows:
                print(f"  ({len(sell_rows)} slot(s) reserved for the sell listing(s) above)")
            exclude = [h.item_id for h in held] + [o.item_id for o in offers]
            fcal = self._fill_cal()
            picks, idle = scanner.build_portfolio(bankroll=cash, held_ids=exclude, free_slots=buy_slots,
                                                  limit_used=self._limit_used(rl), fill_cal=fcal)
            src = "live" if offers else ("runelite" if rl is not None else "assumed")
            print(alert.format_portfolio(picks, cash, held, idle, free_slots=buy_slots, slot_source=src))
            if fcal.get("global_measured") is not None:
                print(f"  (fills auto-calibrated ×{fcal['global']:.2f} from {fcal['n']} attempts — "
                      "applied to gp & ranking)")
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

    def _explain_recovery(self, sell_rows: list) -> None:
        """Inline bounce-vs-cut read for each underwater sell candidate (recovery, baked into go).
        One /timeseries fetch per underwater holding; silent on error."""
        from . import recovery
        from .tax import post_tax_received
        lat = self.latest()
        for r in sell_rows:
            if not r.get("underwater") or not r.get("item_id"):
                continue
            bid = lat.get(r["item_id"], {}).get("low")
            if not bid:
                continue
            try:
                a = recovery.assess_recovery(r["avg_cost"], post_tax_received(int(bid), item_id=r["item_id"]),
                                             recovery.week_mids(api.timeseries(r["item_id"], "1h")))
            except Exception:  # noqa: BLE001 — best-effort
                continue
            if not a:
                continue
            if a["recover"]:
                up = (a["median"] / a["cur"] - 1) * 100 if a["cur"] else 0
                tail = "hold for the bounce" if a["median"] >= r["avg_cost"] else "hold / double down (`recover`)"
                print(alert.color(f"       ↩ {str(r['name'])[:18]}: bounce likely — week median "
                                  f"{a['median']:,.0f} ({up:+.0f}% up), z={a['z']:+.1f} → {tail}", "green"))
            else:
                print(alert.color(f"       ✂ {str(r['name'])[:18]}: no bounce signal "
                                  "(re-rating/downtrend) — sell or break-even hold", "yellow"))

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
        if free > 0:  # a slot IS free — say so, don't claim "all slots working"
            return (f"{free} slot(s) free but nothing cleared the filters — no flip worth a slot "
                    "right now; wait for a better spread or `scan` to widen")
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
        rl = runelite.read()  # for the FU buy-limit counter (limit_used)
        offers = self._active_offers()  # relog-proof active offers
        free = self._free_slots(offers)
        if free <= 0:
            print("  no free slots — collect or cancel an offer first, then `overnight`")
            return
        # reserve a slot for each holding still needing a sell listing — a sell occupies a slot too.
        # holdings with ANY live offer (being sold OR being accumulated) don't need a reserved slot.
        busy_ids = {o.item_id for o in offers}
        pending_sells = sum(1 for h in held if h.item_id not in busy_ids)
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
        offers = self._active_offers()
        if not offers:
            print("  no active offers found — logged in, with Local Data Exporter or Flipping Utilities running?")
            return
        occ, free = len(offers), self._free_slots(offers)
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
        _, tied = self._sync_cash()  # live coins → cash; gold tied in open offers → kept in equity
        lat = self.latest()
        bids = {p.item_id: lat.get(p.item_id, {}).get("low") for p in self.j.positions()}
        equity = self.j.equity(bids) + tied
        bond = lat.get(_BOND, {}).get("high") if not config.MEMBERS else None
        print(f"  cash:        {self.j.cash():>14,.0f}")
        if tied:
            print(f"  in offers:   {tied:>14,.0f}  (reserved in open buys + uncollected sells)")
        print(f"  stock:       {self.j.inventory_value(bids):>14,.0f}  (flip stock you hold — in your bag + sell offers)")
        print(f"  equity:      {equity:>14,.0f}")
        print(f"  realised P&L:{self.j.realized_pnl():>+14,.0f}")
        if bond:
            print(f"  bond:        {bond:>14,.0f}  ({equity / bond * 100:.1f}% — {bond - equity:,.0f} to go)")

    def cmd_recover(self, args: list[str]) -> None:
        """For each underwater holding, read the past week: is the dip likely to bounce (hold /
        double down to lower your average) or a re-rating to cut? Stats-based mean-reversion read —
        a dip can still be a permanent markdown, so verify before doubling down."""
        from . import recovery
        from .tax import post_tax_received
        lat = self.latest()
        mapping = {r["id"]: r for r in api.mapping()}
        limit_used = self._limit_used()
        cash = int(self.j.cash())
        any_uw = False
        for p in self.j.positions():
            bid = lat.get(p.item_id, {}).get("low")
            if not bid:
                continue
            bail = post_tax_received(int(bid), item_id=p.item_id)
            if bail >= p.avg_cost:
                continue  # in the green — nothing to recover
            any_uw = True
            a = recovery.assess_recovery(p.avg_cost, bail, recovery.week_mids(api.timeseries(p.item_id, "1h")))
            if not a:
                print(f"  {p.name}: underwater, not enough week history to judge")
                continue
            up = (a["median"] / a["cur"] - 1) * 100 if a["cur"] else 0
            if not a["recover"]:
                why = ("still trending down (looks like a re-rating)" if a["rerating"]
                       else "didn't trade above your cost this week" if not a["was_green"]
                       else "not statistically low yet")
                print(alert.color(f"  ✂ {p.name}: now {a['cur']:,.0f} vs cost {p.avg_cost:,.0f} — NOT a clear "
                                  f"bounce ({why}); hold at break-even, don't double down", "yellow"))
                continue
            print(alert.color(f"  ↩ {p.name}: HOLD for recovery — week median {a['median']:,.0f} "
                              f"({up:+.0f}% vs now {a['cur']:,.0f}), high {a['high']:,.0f}; cost "
                              f"{p.avg_cost:,.0f}. z={a['z']:+.1f}, no downtrend.", "green"))
            if a["median"] >= p.avg_cost:
                print("       just hold — the week median is at/above your cost; a revert clears you in profit")
            else:
                qty, new_avg = recovery.double_down(p.qty, p.avg_cost, a["cur"], a["median"])
                lim = max(0, (mapping.get(p.item_id, {}).get("limit") or 0) - limit_used.get(p.item_id, 0))
                qty = min(qty, lim, (cash // int(a["cur"])) if a["cur"] else 0)
                if qty > 0:
                    print(f"       double down ~{qty:,} @ {a['cur']:,.0f} → avg {new_avg:,.0f} "
                          "(break-even on a bounce back to the week median)")
                else:
                    print("       (no buy-limit/cash room to double down — just hold)")
        if not any_uw:
            print("  no underwater holdings — nothing to recover")
        else:
            print("  mean-reversion read, not a guarantee — a dip can be a permanent markdown; verify first")

    def cmd_hold(self, args: list[str]) -> None:
        """Tell the journal you hold an item acquired elsewhere (another device) — adds the position
        WITHOUT spending cash, and it sticks through reconcile. Inverse of `forget`.
          hold <item> <qty> [avg_cost]   (avg_cost defaults to the live price)"""
        nums = []
        while args and args[-1].replace(".", "", 1).isdigit():
            nums.insert(0, args.pop())
        if not args or not nums:
            print("  usage: hold <item> <qty> [avg_cost]")
            return
        meta = self.resolve(" ".join(args))
        if not meta:
            print("  item not found")
            return
        iid, name = meta["id"], meta.get("name", str(meta["id"]))
        qty = int(float(nums[0]))
        avg = float(nums[1]) if len(nums) > 1 else float((api.latest().get(iid, {}) or {}).get("high") or 0)
        self.j.hold_position(iid, name, qty, avg)
        print(f"  now holding {qty:,} {name} @ {avg:,.0f} (acquired elsewhere; cash unchanged) — "
              "`pos` shows it and reconcile keeps it")

    def cmd_forget(self, args: list[str]) -> None:
        """Untrack a held position you disposed of elsewhere (e.g. traded on another device) — removes
        it without recording a sale, and it stays gone through reconcile. Use `sell <item> <qty>
        <price>` instead if you want the P&L recorded. Usage: forget <item>"""
        if not args:
            print("  usage: forget <item>")
            return
        meta = self.resolve(" ".join(args))
        if not meta:
            print("  item not found")
            return
        iid, name = meta["id"], meta.get("name", str(meta["id"]))
        pos = self.j.position(iid)
        if not pos or pos.qty <= 0:
            print(f"  not holding {name} — nothing to forget")
            return
        self.j.forget_position(iid, name, pos.qty)
        print(f"  forgot {pos.qty:,} {name} — untracked (recorded as disposed elsewhere; stays gone "
              "through reconcile). Cash unchanged — `bank <gp>` if your gold drifted.")

    def cmd_reconcile(self, args: list[str]) -> None:
        """Recompute every held position from RuneLite's full completed-offer history (authoritative,
        order-independent), correcting phantoms from out-of-order imports. Runs automatically on each
        sync; this shows the result on demand."""
        rl = runelite.read()
        if rl is None:
            print("  no RuneLite data — can't reconcile against the offer history")
            return
        drift = self.j.reconcile_positions(runelite.completed_offers(rl))
        # bag is the final word: clear/trim positions not in your bag or GE (sells RuneLite's window
        # missed, or off-device sells). Reduce-only, only when the live snapshot is present.
        held = local_export.holdings(local_export.read())
        bag_drift = self.j.reconcile_to_holdings(held) if held is not None else []
        if not drift and not bag_drift:
            print("  positions already match your offer history + bag — nothing to correct")
            return
        for name, old, new in drift:
            print(f"  {name}: {old:,} → {new:,} held  (offer history)")
        for name, old, new in bag_drift:
            tag = "dropped (not in bag or GE)" if new == 0 else "trimmed to bag + GE"
            print(f"  {name}: {old:,} → {new:,} held  ({tag})")
        print(f"  reconciled {len(drift) + len(bag_drift)} position(s). Cash/P&L from a historically "
              "mis-recorded sell aren't auto-rewritten — cash is read live from your coins.")

    def cmd_inventory(self, args: list[str]) -> None:
        """What you actually hold, split BANK (sellable now) vs IN-GE (listed for sale / being
        bought), reconciled from your transactions against live RuneLite offers. Alias: inv."""
        split = runelite.holdings_split(self.j.positions(), self._active_offers())
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
        _, tied = self._sync_cash()  # fold gold tied in open offers back into liquid for continuity
        lat = self.latest()
        bids = {p.item_id: lat.get(p.item_id, {}).get("low") for p in self.j.positions()}
        equity_now = self.j.equity(bids) + tied
        initial, times, nw = progress.build_history(rows, self.j.cash() + tied)
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
            "reconcile": lambda a: self.cmd_reconcile(a),
            "forget": lambda a: self.cmd_forget(a), "drop": lambda a: self.cmd_forget(a),
            "hold": lambda a: self.cmd_hold(a), "own": lambda a: self.cmd_hold(a),
            "recover": lambda a: self.cmd_recover(a), "recovery": lambda a: self.cmd_recover(a),
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
