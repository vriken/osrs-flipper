"""Interactive trading terminal — run it and drive everything without spending tokens.

    osrs-flipper trade

★ go  (or just press Enter)  does everything: imports fills, reconciles to your bag, recalibrates,
      then shows your offers + verdicts, what to sell, and the single best use of your free slots —
      ranking fast flips, patient gear, and GE sets on one scale (best-case gear/set EV haircut so it
      stays honest), tuned to the time you have. Daily driver.

Daily
  go                       the one command (above)
  quote <item> [qty]       solve optimal buy/sell prices for an item
  why <item>               explain a price: live vs recent norm, volume z, falling-knife check
  overnight [item]         plan one big ~8h buy to leave while you sleep
  gear [n]                 big-ticket / low-frequency items at their full spread (patient)

Occasional
  scan [n] [online|offline|balanced]   ranked live flips (raw, unallocated)
  sets [n] [roi] [all]     GE set arbitrage: buy pieces→sell set (or reverse), net of tax
  decant [n] [roi] [all]   buy (1)/(2)/(3)-dose potions → decant up to (4) → sell, net of tax (members)
  port [free_slots]        recommended allocation across your free slots
  sellquote <item> [qty]   sell-price tradeoff for held stock (fill time vs profit)
  anomaly                  market-wide price dislocations on abnormal volume
  pnl                      realised P&L, cash, equity, bond progress
  progress                 net-worth chart projected to 10M/100M
  pos                      open positions + unrealised P&L (vs live bid)
  inv                      holdings: in your bag vs listed in GE
  recent [n]               recent trades
  recover                  underwater holdings: bounce (hold) vs re-rating (cut)

  type `help all` for maintenance commands · `quit` to exit
"""

from __future__ import annotations

import contextlib
import io
import queue
import re
import sys
import threading
import time

from . import alert, analysis, api, calibration, config, datasource, monitor, runelite, scanner
from .journal import Journal
from .quote import optimal_quote

_BOND = config.BOND_ITEM_ID


_STATUS_DROP_STARTS = ("→", "KEY", "ACTIVE OFFERS", "REBALANCE", "holdings:", "#", "BEST FOR",
                       "(flips", "(", "*")
_STATUS_DROP_CONTAINS = ("on track", "auto-calibrated", "best-case", "scores shrunk",
                         "reserved for the sell", "priced to sell", "check back", "sell@")
_VERDICT_WORD = {"📦": "collect", "🟠": "margin gone", "🔴": "stale", "🟡": "re-price"}
_OFFER_RE = re.compile(r"^(\d+)\s+(.+?)\s+(BUY|SELL)\b")


def _compact_status(dash: str) -> str:
    """Trim the full console dashboard to a phone-friendly Discord status: a terse cash/slots header, a
    'needs you' block (each flagged offer with its SLOT and the concrete re-quote/re-list target pulled
    from the offer's own hint line), then the sell/decant/buy picks and NEXT. On-track offers, the
    holdings line, table legends and calibration footnotes are all dropped."""
    header, attention, body = "", [], []
    pending = None  # index of the attention entry awaiting its "→ …" hint line (the one right below it)
    for ln in dash.splitlines():
        s = alert.plain(ln).strip()  # strip ANSI FIRST — in the REPL stdout is a tty, so hint lines are
        if not s:                    # colour-wrapped and a raw `startswith("→")` would miss them
            continue
        if "===" in s:                                        # header → one terse line
            header = (s.strip("= ").replace("cash ", "").replace(" slots free", " free")
                      .removesuffix(" day").removesuffix(" — flips cycle").strip())
            pending = None
            continue
        m = _OFFER_RE.match(s)
        if m:                                                 # an active-offer line
            emoji = next((e for e in _VERDICT_WORD if e in s), None)
            if emoji:                                         # needs action → [slot, name, verdict, target]
                attention.append([m.group(1), m.group(2).strip(), _VERDICT_WORD[emoji], ""])
                pending = len(attention) - 1
            else:
                pending = None                                # on-track/open → drop its following hint
            continue
        if s.startswith("→"):                                 # the flagged offer's hint → attach the target
            if pending is not None:
                attention[pending][3] = s[1:].split("  (")[0].split(";")[0].strip()[:80]  # actionable core
                pending = None
            continue                                          # always drop the raw hint line
        if s.startswith(_STATUS_DROP_STARTS) or any(t in s for t in _STATUS_DROP_CONTAINS):
            continue
        body.append(s)
        pending = None
    lines = [header] if header else []
    if attention:
        lines.append("⚠ needs you:")
        lines += [f"  slot {slot} {name} — {tgt or verdict}" for slot, name, verdict, tgt in attention]
    lines += body
    return "\n".join(lines)


class _Tee:
    """A stdout proxy that writes to several streams at once — used to capture the `go` dashboard into a
    buffer while still printing it to the console (interactive `go`)."""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, s: str) -> int:
        for st in self._streams:
            st.write(s)
        return len(s)

    def flush(self) -> None:
        for st in self._streams:
            with contextlib.suppress(Exception):
                st.flush()

_VERDICTS = {
    "collect": ("📦 COLLECT — frees a slot", "yellow"),
    "margin": ("🟠 MARGIN GONE — spread collapsed; cancel/re-quote", "red"),
    "stale": ("🔴 STALE — likely mispriced; cancel & re-quote", "red"),
    "slow": ("🟡 SLOW — consider re-pricing", "yellow"),
    "ontrack": ("🟢 on track", "green"),
    "open": ("⏳ open · true age unconfirmed (placed before tracking)", None),
    "done": ("done", None),
}

_HELP_RARE = """
Maintenance / rare  (all the commands above still work too)
  buy  <item> <qty> <price>   log a buy fill made off-device
  sell <item> <qty> <price>   log a sell fill made off-device (applies GE tax)
  hold <item> <qty> [avg]     track a holding acquired elsewhere (no cash spent)
  forget <item>               untrack a holding traded elsewhere
  audit                       full buy/sell + bag reconciliation, per-item P&L (inspect only)
  calibrate                   measure empirical β + fill correction from your attempts
  preds [n]                   logged model predictions (calibration debug)
  alerts [on|off|test]        background Discord push when an offer needs you
  update                      git pull latest + reload · reload   re-exec, keep DB/state

These run automatically on every command, so you rarely need them by hand: fills import, positions
reconcile to your bag, β + fill correction recalibrate, and cash reads live from your coins.
"""


class Terminal:
    def __init__(self, db: str | None = None) -> None:
        self.j = Journal(path=db)
        self._map: dict[str, dict] | None = None
        self._latest: dict[int, dict] = {}
        self._latest_ts = 0.0
        # auto-calibration cache: β + fill correction, recomputed every CALIBRATE_EVERY_TRADES
        # resolved attempts (not every command) so recommendations don't wiggle on each fill.
        self._cal_at = -1             # resolved-attempt count at last calibration (-1 = never)
        self._cal_beta = config.BETA   # live spread haircut, auto-applied
        self._cal_fill: dict = {}      # live fill-rate correction, auto-applied
        self._cal_eta: dict = {}       # live fill-time (ETA) correction by price×volume, auto-applied
        self._cal_edges: dict = {}     # live per-item realized-edge multipliers, auto-applied
        # auto-push: repost the compact `go` board to Discord on an idle tick (main thread — no DB race).
        # The board is the single message; it reposts at the bottom only when its actionable content changes.
        self._auto_push = False        # enabled flag (`alerts on/off`); auto-on at startup if a channel is set
        self._status_msg_id: str | None = None   # the current board message (deleted + reposted on change)
        self._last_dash = ""           # last board's actionable signature (repost only when it changes)
        # per-`go` caches for the below-cost sell policy (bounce read per item, one opportunity scan)
        self._cut_bounce: dict[int, bool | None] = {}
        self._cut_better: bool | None = None

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
                          fill_cal=self._fill_cal(), edges=self._edges(), beta=self._beta(), cal_eta=self._eta_cal())
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

    def _backfill_fill_time(self) -> None:
        """Seed the fill-time learner from RuneLite's completed history (placedAt→completedAt = real
        live-time). Approximate: the predicted ETA it grades against is reconstructed from CURRENT volume
        (we didn't record the model's past prediction), so it's a warm start that forward data refines.
        Idempotent — already-imported trades are skipped."""
        from . import flip_exporter
        trades = (flip_exporter.read_history() or {}).get("trades") or []
        if not trades:
            print("  no Flip Exporter history to backfill from (is the plugin installed / logged in?)")
            return
        hr = api.one_hour()
        names = {r["id"]: r["name"] for r in api.mapping()}
        added = skipped = 0
        for t in trades:
            placed, done = int(t.get("placedAt", 0) or 0), int(t.get("completedAt", 0) or 0)
            state = t.get("state", "")
            status = ("filled" if state in ("BOUGHT", "SOLD")
                      else "cancelled" if state in ("CANCELLED_BUY", "CANCELLED_SELL") else None)
            iid, qty = int(t.get("id", 0)), int(t.get("qty", 0))
            if status is None or placed <= 0 or done <= placed or qty <= 0:
                skipped += 1
                continue
            h = hr.get(iid, {})
            ah, al = h.get("avgHighPrice"), h.get("avgLowPrice")
            is_buy = bool(t.get("isBuy"))
            legvol = (h.get("lowPriceVolume") if is_buy else h.get("highPriceVolume")) or 0
            if legvol <= 0 or not ah or not al:      # can't reconstruct a prediction without live vol/price
                skipped += 1
                continue
            pred = qty / (config.ALPHA * legvol)     # model's ETA at CURRENT volume (approx warm-start)
            ok = self.j.backfill_attempt(
                t.get("uuid") or f"{iid}:{done}", iid, names.get(iid, str(iid)),
                "BUY" if is_buy else "SELL", qty, int(t.get("avgPrice") or al), placed // 1000,
                done // 1000, status, pred, int(al), int(ah),
                min(h.get("highPriceVolume") or 0, h.get("lowPriceVolume") or 0))
            added += ok
            skipped += not ok
        self._cal_at = -1                            # force a recalibration that includes the backfill
        self._ensure_calibration()
        print(f"  backfilled {added} historical trade(s) into the fill-time learner "
              f"({skipped} skipped — no current vol/price, or already imported).")
        print("  ⚠ approximate: predictions were reconstructed from CURRENT volume, not the volume at the "
              "time you traded. It's a warm start; your live fills refine it.")

    def cmd_calibrate(self, args: list[str]) -> None:
        """Measure empirical β + fill correction from your real order attempts (report only).
        `calibrate backfill` seeds the fill-time learner from your RuneLite trade history."""
        if args and args[0] == "backfill":
            return self._backfill_fill_time()
        from . import calibration
        rows = self.j.calibration_rows()
        n = len(rows)
        if n < 10:
            print(f"  only {n} resolved attempt(s) — need ~10+ for a meaningful read.")
            print("  place the orders `go` recommends; fills auto-log and calibrate as they resolve.")
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
        # fill-TIME (ETA) calibration + why offers resolve as they do
        eta = calibration.calibrate_eta(rows)
        if eta["global_measured"] is not None:
            gm, g = eta["global_measured"], eta["global"]
            verdict = "fills SLOWER than modelled" if g > 1.1 else "fills faster" if g < 0.9 else "well-calibrated"
            print(f"  fill time: measured ×{gm:.2f} → applied ×{g:.2f}  ({verdict}, n={eta['n']}, shrunk→1.0)")
            for name, b in sorted(eta["buckets"].items()):
                print(f"    {name:>10}: measured ×{b['measured']:.2f} → ×{b['shrunk']:.2f}  (n={b['n']})")
        reasons = calibration.eta_attribution(rows)
        if reasons:
            tot = sum(reasons.values())
            parts = " · ".join(f"{k} {v} ({v / tot * 100:.0f}%)"
                               for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]))
            print(f"  why offers resolved: {parts}")
        print(f"  β, fill-rate AND fill-time are AUTO-APPLIED to EV/ETA/ranking (go/scan/port), refreshed "
              f"every {config.CALIBRATE_EVERY_TRADES} resolved trades. config.BETA is just the prior.")

    def cmd_port(self, args: list[str]) -> None:
        cash = int(self.j.cash()) or config.BANKROLL
        held = self.j.positions()
        src = datasource.active()
        offers = src.active_offers()
        active_ids = [o.item_id for o in offers]
        if args and args[0].isdigit():
            free, source = int(args[0]), "specified"
        elif offers or src.cash() is not None:  # logged in with a live snapshot
            free, source = self._free_slots(offers), "live"
        else:
            free, source = max(0, config.GE_SLOTS - len(held)), "assumed"
        # don't recommend what you already hold OR already have an offer on
        exclude = [h.item_id for h in held] + active_ids
        sell_rows = self._sell_plan(held, set(active_ids))  # skip held items with any live offer
        rec = self._recovery_reads(sell_rows)  # underwater → bounce-likely (hold) vs re-rating (cut)
        to_sell, holds = self._split_sells(sell_rows, rec)  # bounce-holds don't list / don't take a slot
        buy_slots = max(0, free - len(to_sell))  # reserve a slot per pending sell (not bounce-holds)
        if to_sell:
            print(alert.format_sell_plan(to_sell))
            for r in to_sell:
                if r["item_id"] in rec:
                    print(self._recovery_note(r, rec[r["item_id"]]))
        if holds:
            print("  HOLDING for the bounce (not listing — no slot used):")
            for r in holds:
                print(self._recovery_note(r, rec[r["item_id"]]))
        self._print_decant_exits(self._decant_exits(held, set(active_ids)))
        print(f"  building portfolio for {buy_slots} free slot(s)"
              + (f" ({len(to_sell)} reserved for sells)…" if to_sell else "…"))
        bids = {p.item_id: (self.latest().get(p.item_id) or {}).get("low") for p in held}
        net_worth = int(self.j.equity(bids) + src.tied_gold())
        picks, idle, _ = scanner.build_portfolio(
            bankroll=cash, held_ids=exclude, free_slots=buy_slots, limit_used=self._limit_used(),
            net_worth=net_worth, fill_cal=self._fill_cal(), edges=self._edges(), beta=self._beta(), cal_eta=self._eta_cal())
        print(alert.format_portfolio(picks, cash, held, idle, free_slots=buy_slots, slot_source=source))
        nudge = self._attention_nudge()
        if nudge:
            print(nudge)

    def _limit_used(self, rl: dict | None = None) -> dict[int, int]:
        """Units bought in the rolling 4h window, per item. Uses the data source's own counter (FU's
        exact one on the legacy path) when it has one, else the journal's ledger sum."""
        lu = datasource.active().limit_used()
        return lu if lu is not None else self.j.buy_limit_used()

    def _ensure_calibration(self) -> None:
        """Recompute β + fill correction every CALIBRATE_EVERY_TRADES resolved attempts (and on first
        use), caching them so recommendations stay stable between refreshes. Both are AUTO-APPLIED to
        EV/ranking/prices; the spread haircut β and the fill rate are measured from your real fills and
        shrunk toward the config priors. Announces each refresh after the initial (startup) one."""
        rows = self.j.calibration_rows()
        n = len(rows)
        if self._cal_at >= 0 and n - self._cal_at < config.CALIBRATE_EVERY_TRADES:
            return
        new_beta = calibration.effective_beta(calibration.calibrate_beta(rows, prior=config.BETA), config.BETA)
        self._cal_fill = calibration.calibrate_fill(rows)
        self._cal_eta = calibration.calibrate_eta(rows)  # realized fill-time vs predicted, by price×volume
        self._cal_edges = analysis.item_edges(analysis.collect_fills())  # per-item realized-edge (JSON audit)
        if self._cal_at >= 0:  # not the first (silent) computation → announce the refresh
            fg = self._cal_fill.get("global") or 1.0
            eg = self._cal_eta.get("global") or 1.0
            print(f"  🔧 recalibrated ({n} resolved trades): β {self._cal_beta:.2f}→{new_beta:.2f} · "
                  f"fill ×{fg:.2f} · eta ×{eg:.2f} (auto-applied)")
        self._cal_beta = new_beta
        self._cal_at = n

    def _eta_cal(self) -> dict:
        """Live (cached) fill-time calibration (realized vs predicted ETA), auto-applied to the ETA model."""
        self._ensure_calibration()
        return self._cal_eta

    def _fill_cal(self) -> dict:
        """Live (cached) fill-rate calibration, auto-applied to EV/ranking."""
        self._ensure_calibration()
        return self._cal_fill

    def _beta(self) -> float:
        """Live (cached) spread-haircut β, auto-calibrated from your fills and auto-applied."""
        self._ensure_calibration()
        return self._cal_beta

    def _edges(self) -> dict:
        """Live (cached) per-item realized-edge multipliers, auto-applied to the ranking."""
        self._ensure_calibration()
        return self._cal_edges

    def _sync_cash(self) -> tuple[int | None, int]:
        """Refresh journal cash from live coins and return (coins, tied_in_offers). Coins already
        reflect placed buys and collected sells, so `cash` stays right with no manual `bank`. No-op
        when there's no live snapshot — cash keeps its last value. Source-agnostic (see datasource)."""
        src = datasource.active()
        c = src.cash()
        if c is not None:
            self.j.set_cash(float(c))
        return c, src.tied_gold()

    def _active_offers(self) -> list:
        """Authoritative live GE offers (real prices, placement time, uuid), from whatever data
        source is active — see datasource. Placement age is made durable through the journal
        (remember_offer_ages) so a RuneLite restart doesn't reset every offer to 'age unconfirmed'."""
        offers = datasource.active().active_offers()
        self.j.remember_offer_ages(offers, int(time.time() * 1000))
        return offers

    def _free_slots(self, offers: list | None = None) -> int:
        """Observed free GE slots from the relog-proof offer source."""
        offers = self._active_offers() if offers is None else offers
        return max(0, config.GE_SLOTS - len(offers))

    def _autosync(self) -> int:
        """Mirror completed fills into the journal, reconcile positions to your live bag, refresh
        cash + calibration. Idempotent → safe to call often. Prefers the Flip Exporter plugin (one
        source: fills from history + active offers, holdings noted-resolved); falls back to the legacy
        Flipping Utilities + Local Data Exporter path when the plugin isn't installed."""
        src = datasource.active()
        fills = src.all_fills()
        held = src.holdings()
        self.j.migrate_fill_accounting_if_needed(fills)  # one-time baseline to current state; no-op after
        n = 0
        bought_since: dict[int, int] = {}                # this sync's NEW filled units per item, by side —
        sold_since: dict[int, int] = {}                  # lets _autodecant tell a decant from a GE sale
        for f in fills:                                  # credit only the NEW units filled per offer
            delta = self.j.account_fill_delta(f.uuid, f.item_id, f.name, f.is_buy, f.qty, f.price)
            if delta > 0:
                n += 1
                side = bought_since if f.is_buy else sold_since
                side[f.item_id] = side.get(f.item_id, 0) + delta
                self.j.reconcile_fill(f.item_id, f.is_buy, delta, f.price,
                                      int(f.t_ms / 1000) or int(time.time()))
        self._autodetect_placements()
        self._detect_cancels(self._active_offers())  # terminal-state vanished offers seen cancelled in history
        # POSITIONS: the live bag is ground truth for held quantity (you keep flip stock in your
        # bag), with cost from your buy history — one idempotent, self-healing pass. Falls back to
        # the offer-history net only when the bag snapshot isn't live (exporter off / logged out).
        if held is not None:
            # move cost basis for any in-game decant BEFORE the bag-sync drops the vanished low dose
            self._autodecant(held, bought_since, sold_since)
            for name, old, new in self.j.sync_positions_to_bag(held, fills):
                tag = "dropped (not in bag/GE)" if new == 0 else "matched to bag"
                print(f"  {name}: {old:,} → {new:,} — {tag}")
        else:
            for name, old, new in self.j.reconcile_positions(fills):
                print(f"  reconciled {name}: {old:,} → {new:,} held (offer history)")
        self._sync_cash()  # authoritative cash from live coins
        self._ensure_calibration()  # refresh β + fill correction every N resolved trades (+ announce)
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
            # record what the MODEL predicted for this offer so it can later be graded (ETA + fill-rate
            # calibration). Leg-specific: a BUY grades the buy leg, a SELL the sell leg. Best-effort.
            pe = pp = pv = None
            try:
                from .quote import optimal_quote
                q = optimal_quote(o.item_id, o.qty, name=names.get(o.item_id), horizon_h=1.0)
                if q:
                    pe = q.t_buy_h if o.is_buy else q.t_sell_h
                    pp = q.p_buy if o.is_buy else q.p_sell
                    pv = q.ev
            except Exception:  # noqa: BLE001 — a prediction is best-effort; never block logging the attempt
                pass
            aid = self.j.record_attempt(o.item_id, names.get(o.item_id, str(o.item_id)), side, o.qty,
                                        o.price, horizon_h=1.0, avg_low=snap["avg_low"],
                                        avg_high=snap["avg_high"], vol_1h_binding=snap["vol_1h_binding"],
                                        pred_eta_h=pe, pred_p_fill=pp, pred_ev=pv)
            self.j.record_event(aid, "placed", qty=o.qty, price=o.price)
            self.j.mark_rec_acted(o.item_id, side, int(time.time()), aid)  # link the placement to its rec
            open_keys.add((o.item_id, side))
            n += 1
        if n:
            print(f"  (auto-logged {n} new order(s) from RuneLite)")
        return n

    def _autodecant(self, bag: dict[int, int], bought_since: dict[int, int],
                    sold_since: dict[int, int]) -> None:
        """Detect an in-game decant and move the low-dose cost basis onto the (4)s BEFORE the bag-sync
        would drop the vanished low dose as 'not in bag' (losing its basis). Decanting is free and leaves
        no GE trade, so it's invisible to fill accounting — but it's recoverable by conservation:

            decanted = (last_tracked_qty − bag_qty) + bought_this_sync − sold_this_sync

        i.e. every low-dose unit that left the bag and wasn't a GE buy/sell was decanted up. Gated on
        membership (Bob Barter) and on the (4) actually existing now (in the bag, on an offer, or sold this
        sync) so a transient/glitchy snapshot can't fabricate a transfer. Announces every move it makes."""
        if not config.MEMBERS:
            return
        from . import combinations
        by_input = {rc.inputs[0][0]: rc for rc in combinations.decant_recipes(api.mapping())}
        if not by_input:
            return
        names = {it["id"]: it["name"] for it in api.mapping()}
        offer_ids = {o.item_id for o in self._active_offers()}
        for p in self.j.positions():
            rc = by_input.get(p.item_id)
            if rc is None:
                continue
            # p.qty is read AFTER this sync's GE fills were applied (account_fill_delta → record_buy/
            # record_sell, above), so it already reflects buys/sells. The low-dose units missing from the
            # live bag beyond what we still track were removed by a non-GE route = decanted. Adding
            # bought_since/sold_since here would double-count deltas already baked into p.qty and fabricate
            # a decant on an ordinary buy (they cancel out algebraically — see the audit).
            decanted = p.qty - bag.get(p.item_id, 0)
            if decanted <= 0:
                continue
            oid = rc.output_id
            four_seen = bag.get(oid, 0) > 0 or oid in offer_ids or sold_since.get(oid, 0) > 0
            if not four_seen:  # no evidence the (4) exists → don't attribute the drop to a decant
                continue
            out_qty = decanted * rc.in_dose // 4
            if out_qty <= 0:
                continue
            out_name = names.get(oid, str(oid))
            moved, navg, in_avg = self.j.record_decant(p.item_id, p.name, decanted, oid, out_name, out_qty)
            if in_avg > 0:
                print(f"  decant detected: {decanted:,} {p.name} → {out_qty:,} {out_name} "
                      f"(basis {moved:,.0f} gp moved onto the (4)s, avg {navg:,.0f}/ea)")

    def _detect_cancels(self, offers: list) -> None:
        """An open/partial attempt whose live offer has VANISHED and which shows a CANCELLED_* in the
        trade history is a cancel — terminal-state it (status + resolved_ts + event) so its live-time is
        learnable. Conservative: fires only on a positive cancel signal, so a just-filled offer awaiting
        import (or a transient snapshot) is never mislabelled a cancel."""
        unresolved = self.j.unresolved_attempts()
        if not unresolved:
            return
        live = {(o.item_id, "BUY" if o.is_buy else "SELL") for o in offers}
        try:
            completed = datasource.active().completed_offers()
        except Exception:  # noqa: BLE001 — no history available → skip (an unfilled offer still expires)
            return
        cancelled = {(f.item_id, "BUY" if f.is_buy else "SELL") for f in completed
                     if getattr(f, "state", "") in ("CANCELLED_BUY", "CANCELLED_SELL")}
        now = int(time.time())
        for a in unresolved:
            key = (a["item_id"], a["side"])
            if key not in live and key in cancelled:
                self.j.resolve_attempt(a["attempt_id"], "cancelled", now, event="cancelled")

    def _rebalance(self, offers: list, cash: int, held: list, net_worth: int,
                   daytime: bool, hours: float) -> list[str]:
        """Flag an active BUY whose slot + capital `go`'s OWN buy plan would deploy something materially
        better into (≥ SWAP_RATIO the expected gp/slot of just finishing the stuck buy), while the buy is
        still EARLY and not just-placed. Sells are excluded — they're waiting for a buyer.

        The alternative is the top pick of the SAME unified candidate pool the deploy plan ranks (flips +
        gear + sets + decant, `_deploy_candidates`), so the named swap is exactly what you'll be told to
        buy for the freed slot — never a flips-only pick the plan then overrides. The freed capital is
        added back for sizing so affordability matches the post-cancel plan."""
        from . import planner
        from .tax import post_tax_received
        now_ms = int(time.time() * 1000)
        # eligible = early fill AND old enough that suggesting a cancel isn't churning a fresh order
        buys = [o for o in offers if o.is_buy and o.qty and (o.filled / o.qty) < config.SWAP_MAX_FILL
                and o.started_ms and (now_ms - o.started_ms) / 3_600_000 >= config.SWAP_MIN_AGE_H]
        if not buys:
            return []
        lat, hr = self.latest(), api.one_hour()
        names = {r["id"]: r["name"] for r in api.mapping()}
        # value of KEEPING each stuck buy = expected gp over the window from finishing it (buy→sell),
        # prorated by how much of that completion fits the window — a slow buy earns only a sliver.
        stuck, freed_capital = [], 0
        for o in buys:
            ask = (lat.get(o.item_id) or {}).get("high")
            if not ask or not o.price or ask >= config.GEAR_MIN_PRICE:
                continue  # patient big-ticket buy is meant to sit and fill slowly — don't nag to cancel it
            remaining = max(0, o.qty - o.filled)
            margin = post_tax_received(int(ask), item_id=o.item_id) - o.price
            v = hr.get(o.item_id, {})
            lowv, highv = v.get("lowPriceVolume") or 0, v.get("highPriceVolume") or 0
            eta = ((remaining / (config.ALPHA * lowv)) if lowv else float("inf")) \
                + ((o.qty / (config.ALPHA * highv)) if highv else float("inf"))
            keep_gp = (max(0.0, margin) * remaining * min(1.0, hours / max(config.MIN_FILL_ETA_H, eta))
                       if eta < float("inf") else 0.0)
            freed_capital += o.price * remaining  # unfilled reserved gold a cancel would return
            stuck.append({"slot": o.slot, "name": str(names.get(o.item_id, o.item_id)),
                          "keep_gp": keep_gp, "roi": margin / o.price, "eta": eta})
        if not stuck:
            return []
        # rank the SAME unified pool the deploy plan uses, sized to the post-cancel cash for one freed slot
        mp = api.mapping()
        n = len(stuck)
        cands, verify, _ = self._deploy_candidates(cash + freed_capital, held, offers, n, net_worth,
                                                   daytime, hours, lat, hr, mp)
        exclude_ids = {h.item_id for h in held} | {o.item_id for o in offers}
        ranked = planner.rank(cands, free_slots=n, patient_confidence=config.PATIENT_EV_CONFIDENCE,
                              exclude_ids=exclude_ids, budget=float(cash + freed_capital), verify=verify)
        if not ranked:
            return []
        # the nudge is 1 cancel → 1 deploy, so only single-slot candidates qualify: a set that ties up N
        # slots can't be deployed by freeing ONE, and pairing it 1:1 would mislabel it as a one-slot swap.
        cand_val = [(c, planner.per_slot_score(c, patient_confidence=config.PATIENT_EV_CONFIDENCE))
                    for c in ranked if c.slots == 1]
        if not cand_val:
            return []
        out, used = [], 0
        for s in sorted(stuck, key=lambda x: x["keep_gp"]):   # worst-kept buy first
            if used >= len(cand_val):
                break
            c, cval = cand_val[used]
            if cval < max(s["keep_gp"], 1.0) * config.SWAP_RATIO:
                continue  # nothing available clearly beats finishing this buy — leave it
            used += 1
            eta_s = f"{s['eta']:.1f}h" if s["eta"] < 100 else "stuck"
            edge = "stuck/underwater" if s["keep_gp"] <= 0 else f"~{cval / max(s['keep_gp'], 1):.0f}× the gp/slot"
            out.append(f"  slot {s['slot']}: cancel {s['name'][:18]} ({s['roi']:+.1%} in {eta_s}) → "
                       f"{str(c.key)[:26]} · {edge} (what `go` would deploy here)")
        return out

    def _review_offers(self) -> list[tuple]:
        """For each live offer: (offer, verdict, elapsed_h, eta_h, progress). Shares the pure
        verdict logic with the background alert watcher (monitor.review_offers)."""
        offers = self._active_offers()
        if not offers:
            return []
        return monitor.review_offers(offers, api.one_hour(), api.latest(), int(time.time() * 1000))

    def _sell_plan(self, held: list, busy_ids: set) -> list[dict]:
        """Recommended sell price + expected profit for each held item with NO active offer.
        `busy_ids` = items with any live offer: already-listed sells (don't re-list) AND active
        buys (you're accumulating more — don't sell the partial out from under yourself)."""
        from .tax import breakeven_sell, post_tax_received
        hourly, latest = api.one_hour(), api.latest()
        skip = busy_ids | self._decant_input_ids()  # a held low dose exits via decant, NOT a flip-sell
        rows = []
        for p in held:
            if p.item_id in skip:
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

    def _decant_input_ids(self) -> set[int]:
        """Item ids of low-dose potions that are decant INPUTS (members only) — held to decant UP to (4),
        never flipped back — so the sell plan must not treat them as flip inventory. Empty in F2P."""
        if not config.MEMBERS:
            return set()
        from . import combinations
        return {rc.inputs[0][0] for rc in combinations.decant_recipes(api.mapping())}

    def _decant_exits(self, held: list, busy_ids: set) -> list[dict]:
        """Exit advice for held low-dose potions: decant UP to (4) at Bob Barter, then sell the (4) — the
        low dose is a decant input, not a flip. `ready` = you hold enough for ≥1 whole (4); otherwise
        accumulate. Skips items with a live offer (still buying / already listed). Members-only."""
        if not config.MEMBERS:
            return []
        from . import combinations
        from .tax import post_tax_received
        mp = api.mapping()
        hourly, latest = api.one_hour(), api.latest()
        by_input = {rc.inputs[0][0]: rc for rc in combinations.decant_recipes(mp)}
        names = {it["id"]: it["name"] for it in mp}
        rows = []
        for p in held:
            if p.item_id in busy_ids:
                continue
            rc = by_input.get(p.item_id)
            if rc is None:
                continue
            out_qty = p.qty * rc.in_dose // 4                 # whole (4)s you can make from the held low dose
            out_name = names.get(rc.output_id, f"item {rc.output_id}")
            if out_qty < 1:                                   # e.g. 1×(3) = 3 doses — not enough for a (4)
                rows.append({"item_id": p.item_id, "name": p.name, "qty": p.qty, "ready": False,
                             "need": -(-4 // rc.in_dose), "out_name": out_name})
                continue
            h4 = hourly.get(rc.output_id, {})
            ask4 = h4.get("avgHighPrice") or (latest.get(rc.output_id, {}) or {}).get("high")
            sell4 = int(round(ask4)) if ask4 else 0
            net = (post_tax_received(sell4, item_id=rc.output_id) * out_qty - p.avg_cost * p.qty) if sell4 else 0
            rows.append({"item_id": p.item_id, "name": p.name, "qty": p.qty, "ready": True,
                         "out_name": out_name, "out_qty": out_qty, "sell4": sell4, "net": net,
                         "leftover": p.qty * rc.in_dose - out_qty * 4, "underwater": bool(sell4) and net < 0})
        return rows

    @staticmethod
    def _print_decant_exits(rows: list) -> None:
        """Render the DECANT exit section — the counterpart to the sell plan for low-dose potion holdings."""
        if not rows:
            return
        print("  DECANT (low-dose potions — decant UP to (4); don't flip the low dose back):")
        for d in rows:
            if not d["ready"]:
                print(f"    {d['name']} ×{d['qty']:,} — decant input; need ≥{d['need']} for one {d['out_name']}. "
                      f"Accumulate or decant with a spare dose — not a flip, holding.")
                continue
            lo = f" (+{d['leftover']} dose leftover)" if d["leftover"] else ""
            warn = "  ⚠ (4) below your cost — decant & hold, don't dump" if d["underwater"] else ""
            print(f"    {d['name']} ×{d['qty']:,} → decant → sell {d['out_qty']:,} {d['out_name']} @{d['sell4']:,} "
                  f"(net {d['net']:+,.0f}){lo} · then log it: `decant log {d['name']} {d['qty']}`{warn}")

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
        return "  active orders: " + ", ".join(parts) if parts else ""

    def cmd_go(self, args: list[str]) -> None:
        """THE one command — everything on one screen so you don't juggle review/port/pos/brief/gear/sets:
        active offers + verdicts, what to sell, and the best use of your free slots, and a single NEXT
        action. The buy plan ranks fast flips + gear + sets on one per-slot currency (best-case gear/set
        EV haircut by PATIENT_EV_CONFIDENCE so it can't crowd out an honest flip). Runway-aware: by day
        flips cycle and dominate; near bedtime/overnight everything fills once so patient gear/sets rise
        (switches NIGHT_SWITCH_H before AWAKE_END). Alias: Enter."""
        from datetime import datetime
        self._cut_bounce, self._cut_better = {}, None  # fresh per-invocation cut-policy caches
        hour = datetime.now().hour
        coins, tied = self._sync_cash()  # live coins on hand + gold tied up in open offers
        cash = int(self.j.cash()) or config.BANKROLL
        held = self.j.positions()
        offers = self._active_offers()  # live GE offers from the active data source
        free = self._free_slots(offers)
        # net worth (cash + stock + gold in offers) sets the slot-worth floor — a slot is worth its
        # opportunity cost against your whole pile, not just the loose cash on hand.
        bids = {p.item_id: (self.latest().get(p.item_id) or {}).get("low") for p in held}
        net_worth = int(self.j.equity(bids) + (tied or 0))
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
        # cash (liquid) + net worth (everything: cash + held stock marked-to-market, listed-to-sell
        # included, + gold reserved in buy offers). The old "(+X in offers)" only counted buy-reserved
        # gold, so it hid the value of what you're currently selling — net worth doesn't.
        cash_str = f"cash {cash:,} · net worth {net_worth:,}"
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
            mp = {r["id"]: r for r in api.mapping()}
            names = {i: r["name"] for i, r in mp.items()}
            print("  ACTIVE OFFERS:")
            for o, v, elapsed_h, eta_h, prog in sorted(rows, key=lambda x: x[0].slot):
                hint = ""
                if v in ("margin", "stale", "slow"):
                    # for a SELL, make the re-price advice cost-aware so we never tell you to chase
                    # the market below your break-even (a loss) unless a cut is actually justified.
                    kw = self._sell_cut_context(o, cash) if not o.is_buy else {}
                    v, hint = self._refine_verdict(o, v, **kw)
                text, c = _VERDICTS[v]
                eta_s = f"{eta_h:.1f}h" if eta_h < 100 else "—"
                if elapsed_h is None:
                    elapsed_s = "?"                       # no timestamp at all
                elif o.placement_observed:
                    elapsed_s = f"{elapsed_h:.1f}h"       # real age
                else:
                    elapsed_s = f"≥{elapsed_h:.1f}h"      # first-seen only → lower bound on true age
                print(f"    {o.slot:<2} {str(names.get(o.item_id, o.item_id))[:18]:18} "
                      f"{'BUY' if o.is_buy else 'SELL':4} {prog:>4.0%} {elapsed_s:>6} "
                      f"{eta_s:>6}  {alert.color(text, c) if c else text}")
                if hint:
                    print(hint)
                refined.append((o, v, elapsed_h, eta_h, prog))
            # bank-a-partial: a partial buy where waiting is pointless — it's flagged (slow/stale/margin)
            # or it HIT the 4h buy limit (rest blocked ~4h). Bank the filled units now (same margin,
            # sooner) & free the slot, when it's worth a material sum. One line, shows on the board too.
            bank_min = net_worth * config.BANK_PARTIAL_MIN_FRAC
            for o, v, *_ in refined:
                if (bpq := self._bank_partial(o)) is None or bpq[2] < bank_min:
                    continue
                lim = int((mp.get(o.item_id) or {}).get("limit") or 0)
                limit_hit = bool(lim and lim <= o.filled < o.qty)
                if v not in ("slow", "stale", "margin") and not limit_hit:
                    continue
                sell_px, net_now, gp = bpq
                note = " · buy limit hit, rest blocked ~4h" if limit_hit else ""
                print(alert.color(f"  💰 bank slot {o.slot} {names.get(o.item_id, o.item_id)}: {o.filled:,} "
                                  f"filled @ {sell_px:,} (net {net_now:,}/ea ≈ {gp:,}) & free the slot{note}", "yellow"))
            swaps = self._rebalance(offers, cash, held, net_worth, daytime, hours_until_sleep)  # a better slot use → swap
            if swaps:
                print("  REBALANCE (a better use of these slots — what `go` would deploy):")
                for s in swaps:
                    print(alert.color(s, "yellow"))

        busy_ids = {o.item_id for o in offers}  # skip held items with ANY live offer (selling / accumulating)
        sell_rows = self._sell_plan(held, busy_ids)
        rec = self._recovery_reads(sell_rows)  # underwater → bounce-likely (hold) vs re-rating (cut)
        to_sell, holds = self._split_sells(sell_rows, rec)  # bounce-holds don't list / don't take a slot
        if to_sell:
            print(alert.format_sell_plan(to_sell))
            for r in to_sell:
                if r["item_id"] in rec:
                    print(self._recovery_note(r, rec[r["item_id"]]))
        if holds:
            print("  HOLDING for the bounce (not listing — no slot used):")
            for r in holds:
                print(self._recovery_note(r, rec[r["item_id"]]))
        decant_rows = self._decant_exits(held, busy_ids)
        self._print_decant_exits(decant_rows)

        picks: list[dict] = []  # unified BUY plan for free slots: fast flips + gear + sets
        # reserve a slot for each pending sell listing — a sell occupies a GE slot too, so buys
        # can't claim every free slot or you'd have nowhere to list what you're holding. Bounce-holds
        # don't count: they stay in your bag, not the GE.
        buy_slots = max(0, free - len(to_sell))
        if buy_slots > 0:
            if to_sell:
                print(f"  ({len(to_sell)} slot(s) reserved for the sell listing(s) above)")
            lat, hr, mp = api.latest(), api.one_hour(), api.mapping()  # one fetch, shared by all sources
            picks = self._plan_buys(cash, held, offers, buy_slots, net_worth, daytime,
                                    hours_until_sleep, lat, hr, mp)

        print("  " + alert.color("NEXT: " + self._next_action(refined, to_sell, free, picks, decant_rows), "bold"))

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

    # --- unified buy plan: fast flips + gear + sets, ranked on one per-slot currency --------------
    @staticmethod
    def _flip_window_gp(pick: dict, hours: float) -> float:
        """Expected gp a fast flip earns over the available window `hours`. Active flips CYCLE
        (throughput = per-cycle gp × cycles that fit the window); an accumulation `hold` fills once."""
        gp = float(pick.get("gp", 0.0))
        if pick.get("tier") == "hold":
            return gp
        eta = pick.get("buy_eta_h")
        cycle_h = max(config.MIN_FILL_ETA_H, 2.0 * eta) if (eta and eta < 100) else max(config.MIN_FILL_ETA_H, hours)
        return gp * max(1.0, hours / cycle_h)

    def _gear_rows(self, lat: dict, hr: dict, mp: list, cash: int, limit_used: dict):
        """Big-ticket / low-frequency 'gear' rows at the full patient spread (β=0). The shared filter
        for `gear` and `go` — build_features with the patient beta/staleness, then the gear gate."""
        from .features import build_features
        df = build_features(lat, hr, mp, bankroll=cash, limit_used=limit_used,
                            beta=config.PATIENT_BETA, staleness_max=config.PATIENT_STALENESS_S)
        if df.empty:
            return df
        if not config.MEMBERS:
            df = df[~df["members"]]
        return df[(df["buy_px"] >= config.GEAR_MIN_PRICE) & (df["vol_1h_binding"] < config.V_MIN_1H)
                  & (df["turnover_1h"] >= config.TURNOVER_MIN_1H) & df["tradeable"]
                  & (df["hold_units"] > 0) & (df["margin_abs"] > 0) & ~df["suspect"]].copy()

    def _deploy_candidates(self, cash: int, held: list, offers: list, buy_slots: int, net_worth: int,
                           daytime: bool, hours: float, lat: dict, hr: dict, mp: list):
        """Gather every deployable candidate — fast flips + patient gear + GE sets + potion decants — as
        planner.Candidate on ONE per-slot currency (expected gp over the window; best-case gear/set/decant
        EV is haircut at ranking). Shared by the free-slot buy plan (`_plan_buys`) AND the rebalance nudge
        (`_rebalance`), so both rank the SAME universe — the nudge's named swap is exactly what the plan
        would deploy. Returns (candidates, verify_fn, fill_cal); verify_fn is the lazy pump/knife gate."""
        from . import anomaly, combinations, combos, planner
        limit_used = self._limit_used()
        exclude_ids = {h.item_id for h in held} | {o.item_id for o in offers}
        cands: list[planner.Candidate] = []
        fcal: dict = {}
        # fair-share capital per slot — every candidate is sized to ONE slot's share (a set to N shares),
        # so a gear/set buy can't win by monopolising the whole pile against fair-shared flips.
        fair = max(1, cash // max(1, buy_slots))

        # fast flips: daytime = cyclable balanced flips (throughput); overnight = one-shot cushioned buys
        if daytime:
            fcal = self._fill_cal()
            picks, _idle, _floor = scanner.build_portfolio(
                bankroll=cash, held_ids=list(exclude_ids), free_slots=buy_slots, limit_used=limit_used,
                net_worth=net_worth, fill_cal=fcal, edges=self._edges(), beta=self._beta())
            for p in picks:
                cands.append(planner.Candidate(kind="flip", key=str(p["name"]), slots=1,
                    window_gp=self._flip_window_gp(p, hours), patient=False,
                    item_ids=(int(p["item_id"]),), fill_eta_h=p.get("buy_eta_h"),
                    cost=float((p.get("buy_px") or 0) * (p.get("qty") or 0)),
                    payload={"buy": p.get("buy_px"), "sell": p.get("sell_px"), "qty": p.get("qty"),
                             "gp": p.get("gp"), "item_id": int(p["item_id"]),
                             "gone_frac": p.get("reliab_gone_frac")}))
        else:
            for r in self._overnight_rows(cash, buy_slots, exclude_ids):
                cands.append(planner.Candidate(kind="flip", key=str(r["name"]), slots=1,
                    window_gp=float(r["profit"]), patient=False, item_ids=(int(r["item_id"]),),
                    cost=float(r["buy"] * r["qty"]),
                    payload={"buy": r["buy"], "sell": r["sell"], "qty": r["qty"], "gp": r["profit"],
                             "item_id": int(r["item_id"]), "windows": r.get("windows", 1)}))

        # gear (patient, best-case): one-shot expected profit
        g = self._gear_rows(lat, hr, mp, cash, limit_used)
        if not getattr(g, "empty", True):
            for r in g.to_dict("records"):
                bpx = int(r["buy_px"]) or 0
                qty = int(min(fair // bpx, r["hold_units"])) if bpx else 0  # one slot's share of capital
                if qty <= 0:
                    continue
                cands.append(planner.Candidate(kind="gear", key=str(r["name"]), slots=1,
                    window_gp=float(r["margin_abs"]) * qty, patient=True, item_ids=(int(r["item_id"]),),
                    fill_eta_h=r.get("fill_eta_h"), cost=float(bpx * qty),
                    payload={"buy": bpx, "sell": int(r["sell_px"]), "qty": qty,
                             "gp": int(r["margin_abs"]) * qty, "item_id": int(r["item_id"])}))

        # sets (patient, best-case): one-shot total gp; an ASSEMBLE ties up N buy slots at once
        for r in combos.scan_combinations(combinations.load("set"), lat, hr, mp, cash=cash,
                                          limit_used=limit_used, beta=config.COMBO_BETA,
                                          staleness_max=config.PATIENT_STALENESS_S, members=config.MEMBERS):
            if r["conversions"] <= 0 or not r["cost_per_conv"]:
                continue
            legs = tuple(int(i) for i in r["bought_ids"])
            budget = fair * len(legs)  # a set occupies N slots → N shares of capital
            conv = min(int(r["conversions"]), int(budget // r["cost_per_conv"]))
            if conv <= 0:
                continue
            wg = float(r["profit_per_conv"]) * conv
            cands.append(planner.Candidate(kind="set", key=f"{r['name']} · {r['direction']}",
                slots=len(legs), window_gp=wg, patient=True, item_ids=legs,
                fill_eta_h=r.get("fill_eta_h"), cost=float(r["cost_per_conv"] * conv),
                payload={"cost": r["cost_per_conv"], "proceeds": r["proceeds_per_conv"],
                         "conv": conv, "gp": wg}))

        # decant (patient, members-only): buy ONE low-dose potion → decant up → sell (4). One buy slot, like
        # a flip. Keep only the best dose per output potion so 3 near-identical rows don't crowd the plan.
        if config.MEMBERS:
            best: dict[int, dict] = {}
            for r in combos.scan_combinations(combinations.decant_recipes(mp), lat, hr, mp, cash=cash,
                                              limit_used=limit_used, beta=config.COMBO_BETA,
                                              staleness_max=config.PATIENT_STALENESS_S, members=True):
                if r["conversions"] <= 0 or not r["cost_per_conv"]:
                    continue
                conv = min(int(r["conversions"]), int(fair // r["cost_per_conv"]))  # one slot's share of capital
                if conv <= 0:
                    continue
                wg = float(r["profit_per_conv"]) * conv
                oid = int(r["output_id"])
                if oid not in best or wg > best[oid]["wg"]:
                    best[oid] = {"r": r, "conv": conv, "wg": wg}
            for b in best.values():
                r = b["r"]
                cands.append(planner.Candidate(kind="decant", key=str(r["name"]), slots=1,
                    window_gp=b["wg"], patient=True, item_ids=(int(r["bought_ids"][0]),),
                    fill_eta_h=r.get("fill_eta_h"),
                    cost=float(r["in_unit_px"] * r["in_qty"] * b["conv"]),
                    payload={"in_qty": r["in_qty"], "in_px": r["in_unit_px"], "out_qty": r["out_qty"],
                             "out_px": r["out_unit_px"], "in_dose": r["in_dose"], "out_dose": r["out_dose"],
                             "conv": b["conv"], "gp": b["wg"]}))

        def verify(c: planner.Candidate) -> bool:  # pump/knife gate on the legs you'd BUY (lazy, top picks)
            if not config.COMBO_ANOMALY_CHECK:
                return True
            try:
                return all(anomaly.is_buyable(anomaly.assess(int(i), lat, hr, api.timeseries, long=True))
                           for i in c.item_ids)
            except Exception:  # noqa: BLE001 — API hiccup shouldn't silently drop a pick
                return True

        return cands, verify, fcal

    def _plan_buys(self, cash: int, held: list, offers: list, buy_slots: int, net_worth: int,
                   daytime: bool, hours: float, lat: dict, hr: dict, mp: list) -> list[dict]:
        """THE unified buy recommendation: gather flip/gear/set/decant candidates (`_deploy_candidates`),
        rank them on one per-slot currency, print the merged plan, and return the chosen picks. Fast flips
        cycle (throughput); gear/sets/decant fill once — so daytime favours flips, overnight lets patient
        plays win the slots. `_next_action` reads the returned picks."""
        from . import planner
        exclude_ids = {h.item_id for h in held} | {o.item_id for o in offers}
        cands, verify, fcal = self._deploy_candidates(cash, held, offers, buy_slots, net_worth,
                                                      daytime, hours, lat, hr, mp)
        chosen = planner.rank(cands, free_slots=buy_slots,
                              patient_confidence=config.PATIENT_EV_CONFIDENCE,
                              exclude_ids=exclude_ids, budget=float(cash), verify=verify)
        self._log_recommendations(chosen, net_worth, buy_slots, daytime, lat, hr)
        self._print_plan(chosen, buy_slots, daytime, hours, fcal)
        return [{"name": c.key, "kind": c.kind, "place_at_h": 0} for c in chosen]

    def _log_recommendations(self, chosen: list, net_worth: int, free_slots: int, daytime: bool,
                             lat: dict, hr: dict) -> None:
        """Ledger every buy the plan recommends (episode upsert + market snapshot), then mark PULLS —
        open recs no longer chosen and not acted on — with a reason. So we can later audit what you
        acted on and whether pulling a rec was a good call (see the `recs` view / pull-evaluation)."""
        from .tax import post_tax_received
        now = int(time.time())
        mode = "online" if daytime else "offline"
        active: set = set()
        for c in chosen:
            if not c.item_ids:
                continue
            iid = int(c.item_ids[0])
            d = c.payload
            active.add((c.kind, iid, "BUY"))
            h = hr.get(iid, {})
            self.j.upsert_recommendation({
                "kind": c.kind, "item_id": iid, "side": "BUY", "name": str(c.key),
                "leg_ids": ",".join(str(int(i)) for i in c.item_ids),  # all legs → placing any one links it
                "buy_px": int(d.get("buy") or d.get("cost") or d.get("in_px") or 0),
                "sell_px": int(d.get("sell") or d.get("proceeds") or d.get("out_px") or 0),
                "qty": int(d.get("qty") or d.get("conv") or 0),
                "pred_eta_h": c.fill_eta_h, "pred_gp": float(c.window_gp), "score": float(c.window_gp),
                "net_worth": int(net_worth), "free_slots": int(free_slots), "mode": mode,
                "snap_low": h.get("avgLowPrice"), "snap_high": h.get("avgHighPrice"),
                "snap_vol": min(h.get("highPriceVolume") or 0, h.get("lowPriceVolume") or 0),
            }, now)
        # pull the open recs that dropped out this run. margin_gone is only sound for single-item flip/gear,
        # where the rec's item IS the traded spread — for a set/decant the stored item_id is just the first
        # leg you buy, so its own avgHigh/avgLow says nothing about the combo's margin; default those to
        # "outranked" rather than asserting margin_gone on the wrong instrument.
        reasons: dict = {}
        for r in self.j.open_recommendations():
            key = (r["kind"], r["item_id"], r["side"])
            if key in active:
                continue
            if r["kind"] in ("flip", "gear"):
                h = hr.get(r["item_id"], {})
                ah, al = h.get("avgHighPrice"), h.get("avgLowPrice")
                net = post_tax_received(int(ah), item_id=r["item_id"]) - int(al) if (ah and al) else None
                reasons[key] = "margin_gone" if (net is not None and net <= 0) else "outranked"
            else:
                reasons[key] = "outranked"
        self.j.pull_recommendations(active, now, reasons, grace_s=config.PULL_GRACE_S)
        self._evaluate_pulls(hr)

    def _evaluate_pulls(self, hr: dict) -> None:
        """Grade matured pulls: did the flip we dropped degrade (good_pull) or hold (regret)? Compares the
        item's CURRENT spread to its pull-time snapshot. Heuristic — a rate to watch, not a per-pull verdict."""
        from . import regret
        from .tax import post_tax_received
        now = int(time.time())
        for r in self.j.pulls_awaiting_eval(now, config.PULL_EVAL_DELAY_S):
            if r["kind"] not in ("flip", "gear"):
                # a set/decant rec's margin is the COMBO's, not the stored first-leg item's spread — we
                # can't cheaply recompute it here, so grade it "unrated" (recorded, excluded from the
                # scorecard) rather than fabricating a good_pull/regret verdict off the wrong instrument.
                self.j.set_rec_eval(r["rec_id"], "unrated", now)
                continue
            h = hr.get(r["item_id"], {})
            ah, al = h.get("avgHighPrice"), h.get("avgLowPrice")
            cur = post_tax_received(int(ah), item_id=r["item_id"]) - int(al) if (ah and al) else None
            snap = (post_tax_received(int(r["snap_high"]), item_id=r["item_id"]) - int(r["snap_low"])
                    if r["snap_high"] and r["snap_low"] else None)
            verdict = regret.classify_pull(snap, cur, min_net=config.MIN_NET_MARGIN)
            if verdict:
                self.j.set_rec_eval(r["rec_id"], verdict, now)

    @staticmethod
    def _print_plan(chosen: list, buy_slots: int, daytime: bool, hours: float, fcal: dict) -> None:
        """Render the merged buy plan — one ranked line per pick, tagged by type."""
        regime = "☀️ day — flips cycle" if daytime else "🌙 patient — one fill; gear/sets favoured"
        if not chosen:
            print(f"  BEST FOR YOUR {buy_slots} FREE SLOT(S) · {regime}: nothing cleared the filters "
                  "(no flip/gear/set worth a slot right now)")
            return
        tags = {"flip": "⚡flip", "gear": "🕰gear", "set": "🧩set", "decant": "🧪decant"}
        used = sum(c.slots for c in chosen)
        print(f"  BEST FOR YOUR {buy_slots} FREE SLOT(S) · {regime}  (using {used})")
        print(f"    {'#':<2}{'type':7}{'trade':32}{'~gp':>12}{'slots':>6}  detail")
        for i, c in enumerate(chosen, 1):
            d = c.payload
            if c.kind == "decant":  # TOTALS + the decant step, so direction & counts can't be misread
                bt, st = d['in_qty'] * d['conv'], d['out_qty'] * d['conv']
                detail = (f"buy {bt:,} ({d['in_dose']})@{int(d['in_px']):,} → decant → "
                          f"sell {st:,} ({d['out_dose']})@{int(d['out_px']):,}")
            elif c.kind == "set":
                detail = f"buy {int(d['cost']):,} → {int(d['proceeds']):,} × {d['conv']}"
            else:
                detail = f"buy {int(d['buy']):,} → sell {int(d['sell']):,} × {d['qty']:,}"
                if d.get("windows", 1) > 1:  # overnight qty spans >1 buy-limit window (fills across resets)
                    detail += f"  · {d['windows']}× buy-limit windows (overnight)"
                gf = d.get("gone_frac")
                if gf is not None and gf >= 0.3:  # margin was gone ≥30% of the last hour at 5m resolution
                    detail += f"  ⚠ fleeting ({gf * 100:.0f}% gone)"
            gp = int(c.window_gp)
            star = "*" if c.patient else " "  # best-case (β=0) marker
            print(f"    {i:<2}{tags.get(c.kind, c.kind):7}{str(c.key)[:32]:32}{gp:>11,}{star}{c.slots:>6}  {detail}")
        if any(c.patient for c in chosen):
            print(f"    * best-case (β=0, fill AT the bid/ask); EV haircut ×{config.PATIENT_EV_CONFIDENCE:g} for ranking")
        if fcal.get("global_measured") is not None:
            print(f"    (flips auto-calibrated from {fcal['n']} attempts: β {fcal.get('global', 1):.2f})")

    def _recovery_reads(self, sell_rows: list) -> dict:
        """{item_id: recovery assessment} for each underwater sell candidate — bounce-likely (hold)
        vs re-rating (cut). One /timeseries fetch per underwater holding; silent on error."""
        from . import recovery
        from .tax import post_tax_received
        lat = self.latest()
        out: dict = {}
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
            if a:
                out[r["item_id"]] = a
        return out

    @staticmethod
    def _split_sells(sell_rows: list, rec: dict) -> tuple[list, list]:
        """Split sells into (to_list, holds). An underwater holding the recovery read says will bounce
        is a hold, not a sell: listing it at break-even sells into the dip and misses the bounce, and
        ties up a slot for an offer that won't fill. Holds are kept in the bag — not listed, no slot."""
        hold_ids = {r["item_id"] for r in sell_rows if rec.get(r["item_id"], {}).get("recover")}
        to_list = [r for r in sell_rows if r["item_id"] not in hold_ids]
        holds = [r for r in sell_rows if r["item_id"] in hold_ids]
        return to_list, holds

    @staticmethod
    def _recovery_note(row: dict, a: dict) -> str:
        """The ↩ bounce / ✂ cut one-liner for one underwater holding, given its recovery read."""
        if a["recover"]:
            up = (a["median"] / a["cur"] - 1) * 100 if a["cur"] else 0
            tail = "hold for the bounce" if a["median"] >= row["avg_cost"] else "hold / double down (`recover`)"
            return alert.color(f"       ↩ {str(row['name'])[:18]}: bounce likely — week median "
                               f"{a['median']:,.0f} ({up:+.0f}% up), z={a['z']:+.1f} → {tail}", "green")
        return alert.color(f"       ✂ {str(row['name'])[:18]}: no bounce signal "
                           "(re-rating/downtrend) — hold at break-even; cut only if a better flip is ready", "yellow")

    @staticmethod
    def _next_action(review_rows: list, sell_rows: list, free: int, picks: list,
                     decants: list | None = None) -> str:
        """The single most important thing to do right now, synthesized from current state."""
        verds = [v for (_o, v, *_rest) in review_rows]
        ready_decants = [d for d in (decants or []) if d.get("ready")]
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
                return " + ".join(actions) + " now (auto-logged from RuneLite)"
        if ready_decants:  # decanting is off-GE (no slot needed) — a real to-do the slot logic won't surface
            return ("decant your low-dose potion(s) at Bob Barter → sell the (4)s, then `decant log` "
                    "(see DECANT above)")
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
    def _refine_verdict(o, verdict: str, *, avg_cost: float | None = None,
                        bounce_likely: bool | None = None, better_flip: bool | None = None) -> tuple[str, str]:
        """Consult fresh prices on a flagged offer so the advice is actionable, not churn:
          - priced right, just slow   → downgrade to on-track (no pointless cancel/re-list)
          - genuinely mispriced        → keep the flag, show the price to move to
          - no profitable spread (buy) → keep the flag, say cancel & redeploy
        Returns (possibly-downgraded verdict, indented hint line).

        For a SELL, `avg_cost` (your cost basis) makes the advice loss-aware: when the market has
        fallen below your break-even, we don't chase it down — we hold at break-even — UNLESS a cut
        is justified (`recovery.cut_below_cost`: no near-term bounce AND a better flip to redeploy
        into). `bounce_likely`/`better_flip` carry those signals from the caller."""
        # RuneLite reports the offer price as 0 until it starts filling — for an unfilled offer
        # we genuinely don't know your price, so we can't say it's mispriced, only show the market.
        known = o.price > 0
        if o.is_buy:
            from .quote import optimal_quote
            from .tax import post_tax_received
            q = optimal_quote(o.item_id, max(1, o.qty - o.filled), horizon_h=1.0)
            if not q:
                return verdict, alert.color("         → no spread to buy into now — cancel the buy & redeploy; "
                                            "stock you already hold may still sell at cost+", "yellow")
            if not known:  # market spread exists → most likely just slow; show it to compare against
                return "slow", alert.color(f"         → market now: buy {q.buy_px:,} / sell {q.sell_px:,} (net {q.net_unit}/ea) — fine if your bid ≥ {q.buy_px:,}", "yellow")
            # known price: fine if within a deadband of the competitive buy AND still profitable to
            # sell into; only under-bidding by MORE than tick jitter (won't fill) or over-paying (no
            # margin) needs a re-quote. The deadband stops a 1gp book wiggle triggering a chase.
            net_at_mine = post_tax_received(q.sell_px, item_id=o.item_id) - o.price
            tol = q.buy_px * config.REPRICE_DEADBAND
            if o.price >= q.buy_px - tol and net_at_mine > 0:
                return "ontrack", alert.color(f"         → your bid {o.price:,} still clears (sell ~{q.sell_px:,}, net {net_at_mine}/ea) — just slow; hold", "green")
            return verdict, alert.color(f"         → re-quote: buy {q.buy_px:,} / sell {q.sell_px:,}  (net {q.net_unit}/ea)", "bold")
        # SELL: reference the 5m average buy price BLENDED toward the last tick (so noise is
        # smoothed but a genuine sharp drop still moves it), and only advise a re-list when you're
        # above that by more than the deadband.
        avg5m = api.five_min().get(o.item_id, {}).get("avgHighPrice")
        tick = api.latest().get(o.item_id, {}).get("high")
        ask = scanner.blended_ref(avg5m, tick, config.REPRICE_BIG_MOVE) \
            or api.one_hour().get(o.item_id, {}).get("avgHighPrice")
        if not ask:
            return verdict, ""
        ask = int(round(ask))
        if not known:
            return "slow", alert.color(f"         → market ask ~{ask:,} (5m avg) — fine if you're listed ≤ {ask:,}", "yellow")
        if o.price <= ask + ask * config.REPRICE_DEADBAND:
            return "ontrack", alert.color(f"         → listed {o.price:,} ≈ market ~{ask:,} (5m avg) — priced to sell, just slow; hold", "green")
        # you're above market. If re-listing at the market would sell BELOW your break-even, don't
        # chase down into a loss: hold at break-even, unless a cut is genuinely justified.
        if avg_cost and avg_cost > 0:
            from .recovery import cut_below_cost
            from .tax import breakeven_sell
            be = breakeven_sell(avg_cost, item_id=o.item_id)
            if ask < be:
                if cut_below_cost(True, bounce_likely, better_flip):
                    loss = int(round((be - ask) * max(1, o.qty - o.filled)))
                    return verdict, alert.color(f"         → market ~{ask:,} < your break-even {be:,}; no bounce + a better "
                                                f"flip is ready → cut & redeploy (~{loss:,} loss)", "bold")
                return "ontrack", alert.color(f"         → market ~{ask:,} is below your break-even {be:,} — "
                                              "hold at break-even; don't sell into the dip", "green")
        return verdict, alert.color(f"         → re-list nearer {ask:,} (5m avg) — you're above market", "bold")

    def _sell_cut_context(self, o, cash: int) -> dict:
        """Cost-basis + cut-policy signals for a flagged SELL, so `_refine_verdict` won't advise
        re-listing below break-even unless a cut is justified. Cheap by default: the recovery read
        and opportunity scan run only when the holding is actually underwater at the live bid."""
        pos = self.j.position(o.item_id)
        if not pos or pos.avg_cost <= 0:
            return {}
        ctx: dict = {"avg_cost": pos.avg_cost}
        from .tax import post_tax_received
        bid = (self.latest().get(o.item_id) or {}).get("low")
        if bid and post_tax_received(int(bid), item_id=o.item_id) < pos.avg_cost:  # a loss is on the table
            ctx["bounce_likely"] = self._bounce_likely(o.item_id, pos.avg_cost, int(bid))
            ctx["better_flip"] = self._better_flip(cash)
        return ctx

    def _bounce_likely(self, item_id: int, avg_cost: float, bid: int) -> bool | None:
        """Does the past-week read expect a near-term bounce for this underwater holding? None if it
        can't be judged (too little history). Cached per `go` to avoid a repeat /timeseries fetch."""
        if item_id in self._cut_bounce:
            return self._cut_bounce[item_id]
        from . import recovery
        from .tax import post_tax_received
        val: bool | None = None
        try:
            a = recovery.assess_recovery(avg_cost, post_tax_received(int(bid), item_id=item_id),
                                         recovery.week_mids(api.timeseries(item_id, "1h")))
            if a is not None:
                val = bool(a["recover"])
        except Exception:  # noqa: BLE001 — best-effort; unknown → treated as "not a clear bounce"
            val = None
        self._cut_bounce[item_id] = val
        return val

    def _better_flip(self, cash: int) -> bool:
        """Is a materially better flip available to redeploy freed capital into ASAP? True when the
        best affordable balanced-scan candidate fills within CUT_ALT_MAX_ETA_H at ≥ CUT_ALT_MIN_ROI_H
        ROI/hour. Cached per `go` (one scan) since it's identical for every underwater holding."""
        if self._cut_better is not None:
            return self._cut_better
        res = False
        try:
            df = scanner.scan(mode="balanced", bankroll=max(1, cash), top=5, limit_used=self._limit_used(),
                              fill_cal=self._fill_cal(), edges=self._edges(), beta=self._beta(), cal_eta=self._eta_cal())
            for _, r in df.iterrows():
                eta = float(r.get("fill_eta_h") or 0)
                roi = float(r.get("margin_pct") or 0)
                if 0 < eta <= config.CUT_ALT_MAX_ETA_H \
                        and scanner.roi_per_hour(roi, eta, config.MIN_FILL_ETA_H) >= config.CUT_ALT_MIN_ROI_H:
                    res = True
                    break
        except Exception:  # noqa: BLE001 — best-effort; unknown → no better flip (safe: bias to hold)
            res = False
        self._cut_better = res
        return res

    # --- background Discord alerts -------------------------------------------
    def _alerts_running(self) -> bool:
        return self._auto_push

    def _start_alerts(self) -> bool:
        """Enable auto-push (the idle-tick mirrors `go` to Discord on the MAIN thread — no watcher
        thread, so no DuckDB race). Auto-on only when a Discord channel is configured."""
        if config.DISCORD_WEBHOOK_URL or alert.bot_enabled():
            self._auto_push = True
        return self._auto_push

    def _stop_alerts(self) -> None:
        self._auto_push = False

    def _render_go(self, echo: bool, args: list | None = None) -> str:
        """Run the full `go` dashboard and return its rendered text. echo=True also prints it locally
        (interactive `go`); echo=False is silent (background auto-tick). Main-thread only — cmd_go touches
        the journal, and only this thread does. A `_Tee` mirrors stdout to the console AND the buffer."""
        buf = io.StringIO()
        sink = _Tee(sys.stdout, buf) if echo else buf
        with contextlib.redirect_stdout(sink):
            self.cmd_go(args or [])
        return buf.getvalue()

    def _go(self, args: list) -> None:
        """Interactive `go`: print the full dashboard locally AND (if pushing is on) repost the compact
        board to Discord — but only when its actionable content changed (see _status_sig)."""
        self._maybe_repost(_compact_status(self._render_go(echo=True, args=args)))

    def _bank_partial(self, o) -> tuple[int, int, int] | None:
        """For a partially-filled BUY, the profit of banking the already-filled units at the competitive
        sell RIGHT NOW: (sell_px, net/unit, total gp). None if not a partial buy or not profitable. Reuses
        the same optimal_quote as _refine_verdict so the board's bank line matches the re-quote figures."""
        if not (o.is_buy and 0 < o.filled < o.qty and o.price > 0):
            return None
        from .quote import optimal_quote
        from .tax import post_tax_received
        q = optimal_quote(o.item_id, max(1, o.qty - o.filled), horizon_h=1.0)
        if not q:
            return None
        net_now = post_tax_received(q.sell_px, item_id=o.item_id) - o.price
        return (int(q.sell_px), int(net_now), int(net_now * o.filled)) if net_now > 0 else None

    def _maybe_repost(self, compact: str) -> None:
        """Repost the board as a fresh message at the bottom (deleting the previous one) ONLY when its
        ACTIONABLE content changed — that's everything but the first line (the header's cash / net worth /
        slots / regime tick constantly), so a bare cash tick never reposts. Bot only — a webhook can't
        delete/edit, so it gets no live board."""
        if not (self._auto_push and alert.bot_enabled() and compact):
            return
        sig = compact.split("\n", 1)[1] if "\n" in compact else compact  # drop the volatile header line
        if sig != self._last_dash:
            self._status_msg_id = alert.repost_status(compact, self._status_msg_id)
            self._last_dash = sig

    def _auto_tick(self) -> None:
        """Idle tick (main thread): keep the journal fresh, then repost the board to Discord — but only
        when its actionable content changed (not on a bare cash tick). Silent locally; no-op unless
        auto-push is on with a channel. The board is the SINGLE Discord message — it carries attention
        offers (with re-quote targets), bank-a-partial, sells, the buy plan and NEXT. No separate pings."""
        if not self._auto_push or not (config.DISCORD_WEBHOOK_URL or alert.bot_enabled()):
            return
        try:
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    self._autosync()  # import fills / reconcile bag / detect decants so the board isn't
                    #                    frozen at the last typed command
            except Exception:  # noqa: BLE001 — a sync hiccup must still let the last-known board post
                pass
            self._maybe_repost(_compact_status(self._render_go(echo=False)))
        except Exception:  # noqa: BLE001 — a push failure must never break the REPL
            pass

    def cmd_alerts(self, args: list[str]) -> None:
        """Background Discord alerts when an offer needs you (filled/margin-gone/stale).
          alerts           status      alerts on|off    toggle      alerts test    send a test ping"""
        sub = args[0].lower() if args else "status"
        chan = "bot" if alert.bot_enabled() else "webhook" if config.DISCORD_WEBHOOK_URL else None
        if sub == "on":
            if not chan:
                print("  no Discord channel — set OSRS_FLIPPER_DISCORD_BOT_TOKEN + _CHANNEL_ID "
                      "(or OSRS_FLIPPER_DISCORD_WEBHOOK), then `reload`")
            else:
                self._start_alerts()
                print(f"  alerts ON — auto-mirroring the `go` dashboard to {chan} every "
                      f"{config.ALERT_POLL_S}s (and on each manual `go`), pushing transitions as they happen")
        elif sub == "off":
            self._stop_alerts()
            print("  alerts OFF")
        elif sub == "test":
            if not chan:
                print("  no Discord channel configured — nothing to test")
            else:
                ok = alert.notify("\U0001f514 osrs-flipper test alert — you're wired up.")
                print(f"  {chan} test: {'sent ✓' if ok else 'FAILED — check token/channel/permissions'}")
        else:
            state = "ON" if self._alerts_running() else "OFF"
            print(f"  alerts {state} · channel: {chan or 'NONE (set bot token+channel or a webhook)'} · "
                  f"poll {config.ALERT_POLL_S}s")
            print("  `alerts on|off` to toggle · `alerts test` to verify · margin-gone/stale/collect are pushed")

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
        held = self.j.positions()
        offers = self._active_offers()  # live GE offers from the active data source
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
        rows = self._overnight_rows(cash, free, exclude)
        print(f"  OVERNIGHT plan — ≥{config.OVERNIGHT_MIN_MARGIN:.0%} cushion, sized to fill over ~8h:")
        print(alert.format_overnight(rows, cash, free))

    def _overnight_rows(self, cash: int, free: int, exclude: set[int]) -> list[dict]:
        """Compute one big cushioned ~8h buy per free slot (the `overnight` plan's rows). Shared by
        `overnight` and by `go`'s patient regime, where they become the fast-flip candidates."""
        from .quote import optimal_quote
        limit_used = self._limit_used()
        mapping = {r["id"]: r for r in api.mapping()}
        df = scanner.scan(mode="offline", bankroll=cash, top=40, limit_used=limit_used, beta=self._beta())
        exclude = set(exclude)
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
            q = optimal_quote(iid, qty, name=r["name"], horizon_h=config.OVERNIGHT_FILL_TARGET_H,
                              target_fill_h=config.OVERNIGHT_FILL_TARGET_H) if qty > 0 else None
            if not q:
                continue
            # the quote posts INSIDE the spread, so buy_px > bid — re-cap qty to what the remaining
            # cash actually affords at that price, or the plan deploys more than you have.
            aqty = min(int(q.qty), int(remaining // q.buy_px))
            if aqty <= 0:
                continue
            deploy = aqty * q.buy_px
            # expected overnight profit = net/unit × qty × P(fill). Don't floor a single unit's
            # fractional expected fill to 0 — int(1 × 0.73) = 0 made a fat-spread ring show +0.
            profit = round(q.net_unit * aqty * q.p_buy)
            # how many 4h buy-limit windows this qty spans — the overnight cap is ~2 windows, and one GE
            # offer keeps filling past a window reset, so a >1-window qty is fillable but must be labelled.
            one_window = max(0, (meta.get("limit") or 0) - limit_used.get(iid, 0))
            windows = -(-aqty // one_window) if one_window else 1
            rows.append({"item_id": iid, "name": q.name, "buy": q.buy_px, "sell": q.sell_px, "qty": aqty,
                         "deploy": deploy, "fill8h": q.p_buy, "profit": profit, "windows": windows})
            exclude.add(iid)
            remaining -= deploy
            slots_left -= 1
        return rows

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
        q = optimal_quote(iid, qty, name=name, horizon_h=config.OVERNIGHT_FILL_TARGET_H,
                          target_fill_h=config.OVERNIGHT_FILL_TARGET_H)
        if not q:
            print(f"  {name}: no profitable overnight quote")
            return
        # the quote posts inside the spread (buy_px > bid), so re-cap to what cash affords at buy_px
        aqty = min(int(q.qty), cash // int(q.buy_px))
        if aqty <= 0:
            print(f"  {name}: not enough cash at the quote price")
            return
        exp_filled = round(aqty * q.p_buy)            # expected units filled overnight (don't floor 1×0.73→0)
        profit = round(q.net_unit * aqty * q.p_buy)   # expected profit = net/unit × qty × P(fill)
        margin_pct = q.net_unit / q.buy_px if q.buy_px else 0
        print(f"  OVERNIGHT (~8h) — {name}")
        print(f"    BUY  {aqty:,} @ {q.buy_px:,}   (~{q.p_buy:.0%} fill ≈ {exp_filled:,} units overnight, ties up ~{aqty * q.buy_px:,} gp)")
        print(f"    AM   collect + SELL @ {q.sell_px:,}   → ~{profit:,} gp profit (net {q.net_unit}/unit, {margin_pct:.1%})")
        if margin_pct < config.OVERNIGHT_MIN_MARGIN:
            print(alert.color(f"    ⚠ thin margin ({margin_pct:.1%}) — risky to leave overnight; a small dip could go red", "red"))

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

    def cmd_analyze(self, args: list[str]) -> None:
        """Realized P&L, win rate, per-item winners/losers and churn across all trade history."""
        print(analysis.report())

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

    def cmd_gear(self, args: list[str]) -> None:
        """Big-ticket / low-frequency items (Barrows, GWD gear) at their FULL spread — patient flips
        you post at the bid/ask and leave for hours. These are excluded from `scan`/`go` by the unit-
        volume and 1h-staleness gates; here both are relaxed and the spread is taken whole (BETA→0,
        since you wait rather than queue-jump). OPTIMISTIC by design: it assumes you capture the full
        bid-ask, so treat it as best-case — calibration tells you what actually fills. `gear <n>`
        shows n rows (default 15)."""
        cash = int(self.j.cash()) or config.BANKROLL
        # the gear set: genuinely big-ticket items (≥ GEAR_MIN_PRICE) the normal scan drops on unit
        # volume but that clear on gp turnover, with a real full-spread margin, not a manip artifact.
        # Shared with `go` via _gear_rows so the gate lives in one place.
        g = self._gear_rows(api.latest(), api.one_hour(), api.mapping(), cash, self._limit_used())
        if getattr(g, "empty", True):
            print("  no big-ticket flips clear tax + the full spread right now (most have <2% spreads "
                  "the 2% tax eats — see `scan` for the liquid stuff)")
            return
        show_all = args and args[0] == "all"  # `gear all` = aspirational list ignoring cash
        n = next((int(a) for a in args if a.isdigit()), 15)
        # size each flip by what you can AFFORD (cash ÷ buy) capped by volume, and rank by the gp that
        # actually achieves — so a big-ticket flip you can do leads, not a 100M item out of reach.
        g["qty"] = (cash // g["buy_px"]).clip(upper=g["hold_units"]).astype(int)
        g["gp"] = g["margin_abs"] * g["qty"]
        if not show_all:
            afford = g[g["qty"] >= 1]
            hidden = len(g) - len(afford)
            if afford.empty:
                cheapest = g.nsmallest(1, "buy_px").iloc[0]
                print(f"  your {cash:,} can't afford a full-spread big-ticket flip yet — the cheapest is "
                      f"{cheapest['name']} at {int(cheapest['buy_px']):,}/ea. Keep compounding via `go`, "
                      f"or `gear all` to see the aspirational list.")
                return
            g = afford
        else:
            g["qty"] = g["hold_units"].astype(int)  # aspirational: full volume-realizable size
            g["gp"] = g["margin_abs"] * g["qty"]
            hidden = 0
        from . import anomaly
        g = g.sort_values("gp", ascending=False)
        lat, hr = api.latest(), api.one_hour()
        kept, pumped = [], 0
        for _, r in g.iterrows():  # keep the best n that pass the pump/knife check
            if len(kept) >= n:
                break
            # gear skips the volume gate, so big-ticket + thin = the most corner-able class. Run the
            # SAME anomaly check scan/go use, so a manipulated spike (live ≫ 2wk baseline) is excluded
            # here too — a fat "spread" on a pumped tome is a trap, not a flip.
            if not anomaly.is_buyable(anomaly.assess(int(r["item_id"]), lat, hr, api.timeseries, long=True)):
                pumped += 1  # sharp pump, falling knife, OR slow pump vs the 3mo baseline on a thin item
                continue
            kept.append(r)
        if not kept:
            print("  every big-ticket candidate is dislocated from its baseline right now (pump / falling "
                  "knife) — nothing safe to leave. These revert; check back later.")
            return
        tag = "aspirational (ignores cash)" if show_all else f"affordable with cash {cash:,}"
        print(f"  PATIENT / GEAR · full spread (β={config.PATIENT_BETA:g}, tax netted) · {tag}"
              f"  ⚠ best-case: assumes you fill AT the bid/ask")
        print(f"  {'item':22}{'buy':>13}{'sell':>13}{'margin':>11}{'pct':>6}{'qty':>5}{'gp':>11}{'  fill'}")
        for r in kept:
            qty = int(r["qty"])
            eta = r["fill_eta_h"]
            fill = f"{eta:.1f}h" if eta and eta < 100 else "—"
            flag = "" if not show_all or int(r["buy_px"]) * qty <= cash else " ⟵ need cash"
            print(f"  {str(r['name'])[:22]:22}{int(r['buy_px']):>13,}{int(r['sell_px']):>13,}"
                  f"{int(r['margin_abs']):>11,}{r['margin_pct'] * 100:>5.1f}%{qty:>5}"
                  f"{int(r['gp']):>11,}  {fill}{flag}")
        if pumped:
            print(f"  ({pumped} candidate(s) skipped — price dislocated from baseline / falling; likely manipulated)")
        if hidden:
            print(f"  ({hidden} pricier items beyond your {cash:,} hidden — `gear all` to see them)")
        print("  qty = affordable × volume-realizable over ~8h; post at the bid/ask and leave it. "
              "Slow to fill — don't expect day-flip turnover.")

    def cmd_sets(self, args: list[str]) -> None:
        """GE set arbitrage: buy the pieces and sell the combined set (ASSEMBLE), or buy the set and sell
        the pieces (BREAK) — whichever nets more after tax. Combining/breaking at the GE set clerk is free
        and instant, so this is pure price arbitrage; no skill needed. Priced patiently like `gear` (full
        spread β=0, 6h staleness) because sets are big-ticket / low-frequency, and OPTIMISTIC by design —
        it assumes you fill AT the bid/ask. Every leg must be liquid and pass the pump/knife check.
          sets [n] [roi] [all]   n rows (default 15) · `roi` ranks by ROI% · `all` = aspirational (ignore
          cash, include unprofitable, ignore your buy-limit room)."""
        from . import anomaly, combinations, combos
        cash = int(self.j.cash()) or config.BANKROLL
        show_all = "all" in args
        by_roi = "roi" in args
        n = next((int(a) for a in args if a.isdigit()), 15)
        lat, hr, mp = api.latest(), api.one_hour(), api.mapping()
        scan_cash = 1 << 62 if show_all else cash  # aspirational view: don't let capital bind sizing
        rows = combos.scan_combinations(
            combinations.load("set"), lat, hr, mp,
            cash=scan_cash, limit_used=None if show_all else self._limit_used(),
            beta=config.COMBO_BETA, staleness_max=config.PATIENT_STALENESS_S,
            members=config.MEMBERS, keep_unprofitable=show_all)
        rows = [r for r in rows if show_all or r["roi"] >= config.COMBO_MIN_ROI]
        if not rows:
            print("  no set arbitrage clears tax + the full spread right now — set/piece prices are in "
                  "line. These gaps open and close; check back later. (`sets all` shows the aspirational list.)")
            return
        if by_roi:
            rows.sort(key=lambda r: r["roi"], reverse=True)
        # pump/knife gate on the legs you'd BUY — same check `gear`/`go` use — until n rows survive
        kept, pumped = [], 0
        for r in rows:
            if len(kept) >= n:
                break
            if config.COMBO_ANOMALY_CHECK and not show_all and not all(
                anomaly.is_buyable(anomaly.assess(int(iid), lat, hr, api.timeseries, long=True))
                for iid in r["bought_ids"]):
                pumped += 1
                continue
            kept.append(r)
        if not kept:
            print("  every set arbitrage has a dislocated leg right now (pump / falling knife) — nothing "
                  "safe to work. These revert; check back later.")
            return
        tag = "aspirational (ignores cash)" if show_all else f"affordable with cash {cash:,}"
        rank = "ROI%" if by_roi else "total gp"
        print(f"  COMBINATIONS · sets · full spread (β={config.COMBO_BETA:g}, tax netted) · {tag} · by {rank}"
              f"  ⚠ best-case: assumes you fill AT the bid/ask")
        print(f"  {'set':24}{'dir':>9}{'cost':>13}{'sell':>13}{'net/conv':>11}{'roi':>6}{'conv':>6}{'gp':>12}  fill")
        for r in kept:
            eta = r["fill_eta_h"]
            fill = f"{eta:.1f}h" if eta and eta < 100 else "—"
            conv = r["conversions"]
            flag = "" if not show_all or conv >= 1 else " ⟵ need cash"
            print(f"  {str(r['name'])[:24]:24}{r['direction']:>9}{int(r['cost_per_conv']):>13,}"
                  f"{int(r['proceeds_per_conv']):>13,}{int(r['profit_per_conv']):>+11,}"
                  f"{r['roi'] * 100:>5.1f}%{conv:>6}{int(r['total_gp']):>12,}  {fill}{flag}")
        if pumped:
            print(f"  ({pumped} set(s) skipped — a leg is price-dislocated / falling; likely manipulated)")
        print("  ASSEMBLE = buy pieces → sell set (tax the set once); BREAK = buy set → sell pieces (tax "
              "each piece). Combine/break is free & instant at the GE set clerk.")
        print("  net/conv = gp kept per set after tax · conv = affordable × buy-limit-bound × "
              "volume-realizable · this is many buys + a sell, so it's slot- & click-heavy vs a single flip.")

    def cmd_decant(self, args: list[str]) -> None:
        """Potion decanting: buy the cheaper low-dose potion (1)/(2)/(3), decant UP to (4) at Bob Barter
        (free, instant, no Herblore), sell the (4) — capturing the per-dose premium 4-doses carry, net of
        the single GE tax on the (4) sale. Freed empty vials are credited. Priced patiently like `sets`
        (full spread β=0, 6h staleness) and OPTIMISTIC by design — it assumes you fill AT the bid/ask.
        MEMBERS-ONLY (Bob Barter is a members service); every leg must be liquid and pass the pump/knife check.
          decant [n] [roi] [all]   n rows (default 15) · `roi` ranks by ROI% · `all` = aspirational (ignore
          cash, include unprofitable, ignore your buy-limit room)
          decant log <potion(dose)> <count>   after you decant in-game, move the cost basis onto the (4)s so
          the later sell books real P&L — e.g. `decant log Super strength(3) 2000`."""
        if args and args[0] == "log":  # bookkeeping, not a scan — allow it regardless of the members flag
            return self._decant_log(args[1:])
        if not config.MEMBERS:
            print("  decanting is a members-only service (Bob Barter at the GE) — set OSRS_FLIPPER_MEMBERS=1 "
                  "once you've redeemed a bond.")
            return
        from . import anomaly, combinations, combos
        cash = int(self.j.cash()) or config.BANKROLL
        show_all = "all" in args
        by_roi = "roi" in args
        n = next((int(a) for a in args if a.isdigit()), 15)
        lat, hr, mp = api.latest(), api.one_hour(), api.mapping()
        scan_cash = 1 << 62 if show_all else cash  # aspirational view: don't let capital bind sizing
        rows = combos.scan_combinations(
            combinations.decant_recipes(mp), lat, hr, mp,
            cash=scan_cash, limit_used=None if show_all else self._limit_used(),
            beta=config.COMBO_BETA, staleness_max=config.PATIENT_STALENESS_S,
            members=True, keep_unprofitable=show_all)
        rows = [r for r in rows if show_all or r["roi"] >= config.COMBO_MIN_ROI]
        if not rows:
            print("  no potion decant clears tax + the full spread right now — dose prices are in line. "
                  "These gaps open and close; check back later. (`decant all` shows the aspirational list.)")
            return
        if by_roi:
            rows.sort(key=lambda r: r["roi"], reverse=True)
        # pump/knife gate on the legs you'd BUY — same check `sets`/`go` use — until n rows survive
        kept, pumped = [], 0
        for r in rows:
            if len(kept) >= n:
                break
            if config.COMBO_ANOMALY_CHECK and not show_all and not all(
                anomaly.is_buyable(anomaly.assess(int(iid), lat, hr, api.timeseries, long=True))
                for iid in r["bought_ids"]):
                pumped += 1
                continue
            kept.append(r)
        if not kept:
            print("  every potion decant has a dislocated leg right now (pump / falling knife) — nothing "
                  "safe to work. These revert; check back later.")
            return
        tag = "aspirational (ignores cash)" if show_all else f"affordable with cash {cash:,}"
        rank = "ROI%" if by_roi else "total gp"
        print(f"  COMBINATIONS · decant · full spread (β={config.COMBO_BETA:g}, tax netted, vials credited) · "
              f"{tag} · by {rank}  ⚠ best-case: assumes you fill AT the bid/ask")
        print(f"  {'potion (from→to)':22}{'buy → decant → sell (totals for the plan)':44}{'ROI':>6}"
              f"{'net gp':>12}  fill")
        for r in kept:
            eta = r["fill_eta_h"]
            fill = f"{eta:.1f}h" if eta and eta < 100 else "—"
            conv = r["conversions"]
            flag = "" if not show_all or conv >= 1 else " ⟵ need cash"
            bt, st = r["in_qty"] * conv, r["out_qty"] * conv   # totals you'd actually buy / sell
            seg = (f"{bt:,} ({r['in_dose']})@{int(r['in_unit_px']):,} → "
                   f"{st:,} ({r['out_dose']})@{int(r['out_unit_px']):,}")
            print(f"  {str(r['name'])[:22]:22}{seg:44}{r['roi'] * 100:>5.1f}%{int(r['total_gp']):>12,}"
                  f"  {fill}{flag}")
        if pumped:
            print(f"  ({pumped} decant(s) skipped — a leg is price-dislocated / falling; likely manipulated)")
        print("  Read a row as: BUY that many of the (from)-dose at its price, decant UP for free at Bob Barter, "
              "then SELL the (to=4)-dose (post-tax) at its price. You buy the low dose, you sell the (4).")
        print("  Counts differ because doses are conserved — 4×(3) = 3×(4) — not multiplied. The edge is a dose "
              "being cheaper as a low dose than as a (4). ROI = per gp deployed · totals fit cash+buy-limit · members-only.")
        print("  After you decant in-game, run `decant log <potion(dose)> <count>` so the (4) sell books real P&L.")

    def _decant_log(self, args: list[str]) -> None:
        """Record an executed decant so cost basis follows the potion: `decant log <source-potion(dose)>
        <count>` moves the basis of `count` low-dose potions onto the (4)s they became, so the later sell
        reports true realised P&L instead of 0. e.g. `decant log Super strength(3) 2000`."""
        import math
        import re
        if len(args) < 2 or not args[-1].isdigit():
            print("  usage: decant log <potion(dose)> <count>   e.g. decant log Super strength(3) 2000")
            return
        count = int(args[-1])
        src = self.resolve(" ".join(args[:-1]))
        if not src:
            print("  source not found — name the low dose exactly, e.g. 'Super strength(3)'")
            return
        m = re.match(r"^(.+?)\((\d)\)$", src["name"])
        if not m or int(m.group(2)) >= 4:
            print(f"  '{src['name']}' isn't a (1),(2) or (3) dose — decant UP means the source is a low dose")
            return
        base, dose = m.group(1), int(m.group(2))
        out = self.resolve(f"{base}(4)")
        if not out:
            print(f"  couldn't find {base}(4) in the mapping")
            return
        out_qty = count * dose // 4
        if out_qty <= 0:
            print(f"  {count}×({dose}) = {count * dose} doses — not enough for a single (4)")
            return
        if (count * dose) % 4:
            step = 4 // math.gcd(dose, 4)  # smallest whole-(4) source batch for this dose
            print(f"  ⚠ {count}×({dose}) = {count * dose} doses isn't a whole number of (4)s — recording "
                  f"{out_qty} and leaving {count * dose - out_qty * 4} dose(s) behind. Decant multiples of {step}.")
        moved, navg, in_avg = self.j.record_decant(src["id"], src["name"], count, out["id"], out["name"], out_qty)
        if in_avg <= 0:
            print(f"  ⚠ no tracked cost for {src['name']} (position already synced away or bought off-journal). "
                  f"Recorded {out_qty:,} {out['name']} at avg 0 — its sell P&L will read low. Re-`buy` the source, "
                  f"or `hold {out['name']} {out_qty} <avg>` to set the basis.")
        else:
            print(f"  decanted {count:,} {src['name']} → {out_qty:,} {out['name']} · moved {moved:,.0f} gp basis "
                  f"→ avg {navg:,.0f}/ea (cash & tax unchanged; the sell will now book real P&L)")

    def cmd_sync(self, args: list[str]) -> None:
        """Share the learner across your machines without a server. `sync export` writes this device's
        resolved attempts + blacklist to sync/<device>.json (commit & push it); `sync import` merges every
        OTHER device's file from sync/ (idempotent) and recalibrates over the union; `sync` does both.
        Calibration is derived, so merging the raw attempts gives one unified learner across machines."""
        import json
        op = args[0] if args else "both"
        d = config.SYNC_DIR
        d.mkdir(parents=True, exist_ok=True)
        dev = self.j.device_id()
        if op in ("export", "both"):
            payload = self.j.export_learning()
            f = d / f"{dev}.json"
            f.write_text(json.dumps(payload))
            print(f"  exported {len(payload['attempts'])} attempt(s) + {len(payload['blacklist'])} "
                  f"blacklisted → {f}")
            print("  commit & push it, then run `sync import` (or just `sync`) on your other machine.")
        if op in ("import", "both"):
            ta = tb = 0
            for f in sorted(d.glob("*.json")):
                if f.stem == dev:
                    continue
                try:
                    na, nb = self.j.import_learning(json.loads(f.read_text()))
                except Exception as e:  # noqa: BLE001 — a corrupt/foreign file shouldn't abort the merge
                    print(f"  ⚠ skipped {f.name} ({type(e).__name__})")
                    continue
                ta, tb = ta + na, tb + nb
                if na or nb:
                    print(f"  merged {f.name}: +{na} attempt(s), +{nb} blacklist")
            if ta or tb:
                config.BLACKLIST_IDS |= self.j.blacklist_ids()
                self._cal_at = -1
                self._ensure_calibration()  # recompute β / fill / fill-time over the merged union
                print(f"  imported {ta} attempt(s) + {tb} blacklist from other devices → recalibrated")
            else:
                print("  nothing new to import (no other-device files in sync/, or all already merged)")

    def cmd_recs(self, args: list[str]) -> None:
        """Recommendation ledger: how many buys the engine advised, how many you acted on, and how many
        it pulled (with the reason). Pull QUALITY — whether pulling was the right call — lands with the
        regret scorecard in `analyze`."""
        s = self.j.recommendation_stats()
        if not s["total"]:
            print("  no recommendations logged yet — run `go` a few times and they accrue.")
            return
        acted_pct = s["acted"] / s["total"] * 100
        print(f"  RECOMMENDATIONS: {s['total']} logged · {s['acted']} acted ({acted_pct:.0f}%) · "
              f"{s['pulled']} pulled")
        if s["reasons"]:
            print("  pulled by reason: "
                  + " · ".join(f"{k} {v}" for k, v in sorted(s["reasons"].items(), key=lambda kv: -kv[1])))
        # pull-quality scorecard: of the pulls old enough to judge, how many were good vs regret, by reason
        quality = self.j.pull_quality()
        if quality:
            by_reason: dict[str, dict[str, int]] = {}
            for reason, verdict, cnt in quality:
                by_reason.setdefault(reason, {})[verdict] = cnt
            good = sum(v.get("good_pull", 0) for v in by_reason.values())
            regret = sum(v.get("regret", 0) for v in by_reason.values())
            tot = good + regret
            if tot:
                print(f"  PULL QUALITY: {good}/{tot} good pulls ({good / tot * 100:.0f}%), "
                      f"{regret} regret (a still-good flip we dropped)")
                for reason, v in sorted(by_reason.items(), key=lambda kv: -(kv[1].get("regret", 0))):
                    g, rg = v.get("good_pull", 0), v.get("regret", 0)
                    flag = "  ⚠ over-pulling?" if rg > g and (g + rg) >= 3 else ""
                    print(f"    {reason:>11}: {g} good · {rg} regret{flag}")
                print("  regret = the flip held up after we pulled it — a hint a gate or scan churn is too "
                      "eager. good_pull = the spread degraded, so dropping it dodged a dud.")

    def cmd_blacklist(self, args: list[str]) -> None:
        """Never recommend an item again (e.g. one whose spread never fills). It's dropped at the feature
        source, so it vanishes from `go`, `scan`, `gear`, `sets` and `decant` alike. Persisted across sessions.
          blacklist                 list the blacklist
          blacklist <item>          add (name or id)
          blacklist rm <item>       remove"""
        if not args:
            items = self.j.blacklist_items()
            if not items:
                print("  blacklist empty — `blacklist <item>` to never see an item recommended again")
                return
            print("  BLACKLIST (never recommended):")
            for iid, name in items:
                print(f"    {iid:>7}  {name}")
            return
        op = args[0].lower()
        rest = args[1:] if op in ("add", "rm", "remove") else args
        meta = self.resolve(" ".join(rest))
        if not meta:
            print("  item not found — name it exactly, or pass its id")
            return
        if op in ("rm", "remove"):
            self.j.blacklist_remove(meta["id"])
            config.BLACKLIST_IDS.discard(meta["id"])
            print(f"  removed {meta['name']} from the blacklist — it can be recommended again")
        else:
            self.j.blacklist_add(meta["id"], meta["name"])
            config.BLACKLIST_IDS.add(meta["id"])
            print(f"  blacklisted {meta['name']} — never recommended again "
                  f"(`blacklist rm {meta['name']}` to undo)")

    def cmd_audit(self, args: list[str]) -> None:
        """Full reconciliation from the authoritative sources — RuneLite's complete buy/sell history
        (the trades file) + your live bag. Per item: total bought, total sold, history net, what you
        ACTUALLY hold, avg cost, and realised P&L (avg-cost basis). Flags any discrepancy (a sell that
        rolled out of RuneLite's retained window or happened on another device). Read-only."""
        from collections import defaultdict

        from .tax import post_tax_received
        names = {r["id"]: r["name"] for r in api.mapping()}
        src = datasource.active()
        bag = src.holdings() or {}
        completed = src.completed_offers()
        if not bag and not completed:
            print("  no data — is the Flip Exporter plugin running and are you logged in?")
            return
        agg: dict[int, dict] = defaultdict(lambda: {"bq": 0, "bc": 0, "sq": 0, "sp": 0})
        for f in completed:  # authoritative completed buys + sells
            a = agg[f.item_id]
            if f.is_buy:
                a["bq"] += f.qty
                a["bc"] += f.qty * f.price
            else:
                a["sq"] += f.qty
                a["sp"] += f.qty * post_tax_received(f.price, item_id=f.item_id)
        items = sorted(set(agg) | set(bag), key=lambda i: -(agg[i]["sp"] + bag.get(i, 0)))
        tot_real = tot_hold = 0.0
        print(f"  {'item':22}{'bought':>8}{'sold':>8}{'net':>7}{'held':>7}{'avg':>7}{'realP&L':>10}  flag")
        for iid in items:
            a = agg[iid]
            held = bag.get(iid, 0)
            if a["bq"] == 0 and held == 0:
                continue
            avg = a["bc"] / a["bq"] if a["bq"] else 0.0
            realized = a["sp"] - a["sq"] * avg
            net = a["bq"] - a["sq"]
            tot_real += realized
            tot_hold += held * avg
            flag = "" if net == held else f"Δ{held - net:+,} (off-device/rolled out)"
            print(f"  {names.get(iid, str(iid))[:22]:22}{a['bq']:>8,}{a['sq']:>8,}{net:>7,}{held:>7,}"
                  f"{avg:>7,.0f}{realized:>+10,.0f}  {flag}")
        print(f"  {'TOTAL':22}{'':>8}{'':>8}{'':>7}{'':>7}{'':>7}{tot_real:>+10,.0f}  (history, avg-cost)")
        print(f"  journal realised P&L: {self.j.realized_pnl():>+,.0f} (live ledger) · "
              f"holdings cost basis: {tot_hold:,.0f}")
        print("  net = bought − sold (history). held = your live bag + open offers. a Δ flag means "
              "the bag and the retained history disagree — bag wins (it's what you actually have).")

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
        # fast margin-decay read: how durable is the achievable spread at 5m resolution (the 1h view can't see it)
        from .persistence import fetch_reliability
        from .tax import post_tax_received
        ah, al = a.get("avg_high"), a.get("avg_low")
        if ah and al:
            qnet = post_tax_received(int(ah), item_id=meta["id"]) - int(al)
            rel = fetch_reliability(meta["id"], qnet)
            if rel and not rel["thin"]:
                tag = alert.color("⚠ fleeting spread", "yellow") if rel["reliab_mult"] < 0.95 else "durable"
                print(f"  margin (5m, last hr): held ≥half-quote {rel['uptime'] * 100:.0f}% of bars · "
                      f"gone {rel['gone_frac'] * 100:.0f}% · rank ×{rel['reliab_mult']:.2f}  {tag}")
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
        to 10M/100M at a growth rate re-fit from your own trade history. Saves + opens a PNG.

        History source: RuneLite's authoritative completed fills, replayed on an avg-cost basis — so it
        works for a pure auto-sync workflow (whose manual `ledger` is empty). Falls back to the typed
        buy/sell/decant `ledger` when RuneLite isn't connected."""
        from . import progress
        from .journal import realized_history_from_fills
        rows: list = []
        try:
            completed = datasource.active().completed_offers()
        except Exception:  # noqa: BLE001 — a datasource hiccup just means fall back to the ledger
            completed = []
        if completed:
            rows = realized_history_from_fills(completed, self.j.decant_events())
        if len(rows) < 2:  # no live RuneLite history → the typed buy/sell/decant ledger
            rows = self.j.con.execute("SELECT ts, cash_delta, realized_pnl FROM ledger ORDER BY ts").fetchall()
        if len(rows) < 2:
            print("  not enough trade history yet — flip a bit (or start RuneLite), then `progress`")
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

    def cmd_help(self, args: list[str]) -> None:
        """Show the daily/occasional commands; `help all` adds the maintenance ones."""
        print(__doc__)
        if args and args[0].lower() in ("all", "full", "-a"):
            print(_HELP_RARE)

    # --- loop ----------------------------------------------------------------
    def run(self) -> None:
        print("osrs-flipper terminal — type `help`, `quit` to exit")
        src = datasource.active()
        ver = src.version()
        print(alert.color(f"  data source: {src.name}" + (f" v{ver}" if ver else ""), "green"))
        for w in src.warnings():
            print(alert.color(f"  ⚠ data looks wrong: {w}. Slot/limit advice may be unsafe — "
                              f"pass free slots explicitly (`port <n>`).", "red"))
        config.BLACKLIST_IDS |= self.j.blacklist_ids()  # persisted never-recommend list → live filter
        n0 = self._autosync()
        if n0:
            print(f"  (auto-synced {n0} fill(s) from RuneLite)")
        if self._start_alerts():  # auto-on when a Discord channel is configured
            chan = "bot" if alert.bot_enabled() else "webhook"
            print(f"  (Discord auto-push ON via {chan} — the `go` dashboard mirrors there + transitions "
                  f"ping, refreshed every {config.ALERT_POLL_S}s; `alerts off` to stop)")
        handlers = {
            # daily
            "go": lambda a: self._go(a),
            "quote": lambda a: self.cmd_quote(a), "why": lambda a: self.cmd_why(a),
            "overnight": lambda a: self.cmd_overnight(a), "gear": lambda a: self.cmd_gear(a),
            # occasional
            "scan": lambda a: self.cmd_scan(a), "port": lambda a: self.cmd_port(a),
            "sets": lambda a: self.cmd_sets(a), "decant": lambda a: self.cmd_decant(a),
            "sellquote": lambda a: self.cmd_sellquote(a), "anomaly": lambda a: self.cmd_anomaly(a),
            "pnl": lambda a: self.cmd_pnl(), "progress": lambda a: self.cmd_progress(a),
            "pos": lambda a: self.cmd_pos(), "inv": lambda a: self.cmd_inventory(a),
            "recent": lambda a: self.cmd_recent(a), "recover": lambda a: self.cmd_recover(a),
            "analyze": lambda a: self.cmd_analyze(a), "analysis": lambda a: self.cmd_analyze(a),
            # rare / maintenance (hidden from `help`; shown in `help all`)
            "buy": lambda a: self._trade(a, "buy"), "sell": lambda a: self._trade(a, "sell"),
            "hold": lambda a: self.cmd_hold(a), "forget": lambda a: self.cmd_forget(a),
            "blacklist": lambda a: self.cmd_blacklist(a), "sync": lambda a: self.cmd_sync(a),
            "recs": lambda a: self.cmd_recs(a),
            "audit": lambda a: self.cmd_audit(a), "calibrate": lambda a: self.cmd_calibrate(a),
            "preds": lambda a: self.cmd_preds(a), "alerts": lambda a: self.cmd_alerts(a),
            "update": lambda a: self.cmd_update(a), "reload": lambda a: self.cmd_reload(a),
            "help": lambda a: self.cmd_help(a), "?": lambda a: self.cmd_help(a),
        }
        # input runs in a reader thread → a queue, so the main loop can wake on a timer (queue.Empty) to
        # run the auto-push tick while idle. ALL journal access stays on this (main) thread → no DB race.
        inq: queue.Queue = queue.Queue()

        def _reader() -> None:
            for line in sys.stdin:
                inq.put(line)
            inq.put(None)  # stdin closed (EOF / piped input exhausted)

        threading.Thread(target=_reader, daemon=True).start()
        sys.stdout.write("osrs> ")
        sys.stdout.flush()
        while True:
            try:
                try:
                    raw = inq.get(timeout=config.ALERT_POLL_S)
                except queue.Empty:
                    self._auto_tick()  # idle → mirror `go` to Discord if anything changed (silent locally)
                    continue
                if raw is None:  # EOF
                    print()
                    break
                raw = raw.strip()
                if not raw:
                    raw = "go"  # bare Enter → the everything dashboard
                cmd, *args = raw.split()
                cmd = cmd.lower()
                if cmd in ("quit", "exit", "q"):
                    break
                synced = self._autosync()  # mirror completed fills into the journal before every command
                if synced:
                    print(f"  (auto-synced {synced} new fill(s) from RuneLite)")
                fn = handlers.get(cmd)
                if not fn:
                    print(f"  unknown command: {cmd} (type `help`)")
                else:
                    try:
                        fn(args)
                    except Exception as e:  # keep the REPL alive on any error
                        print(f"  error: {e}")
                sys.stdout.write("osrs> ")  # re-prompt only after handling a command (not on idle ticks)
                sys.stdout.flush()
            except KeyboardInterrupt:
                print()
                break


def run() -> None:
    Terminal().run()
