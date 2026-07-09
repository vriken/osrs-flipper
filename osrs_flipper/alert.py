"""Output: formatted console table + optional Discord webhook."""

from __future__ import annotations

import re
import sys

import pandas as pd

from . import config
from .http import get_session

_ANSI = {"red": "\033[31m", "yellow": "\033[33m", "green": "\033[32m", "bold": "\033[1m", "reset": "\033[0m"}
_USE_COLOR = sys.stdout.isatty()
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")  # strip colour codes before sending to Discord


def color(text: str, c: str) -> str:
    """Wrap text in an ANSI colour when stdout is a terminal (no-op when piped/tested)."""
    return f"{_ANSI[c]}{text}{_ANSI['reset']}" if _USE_COLOR and c in _ANSI else text

_BASE_COLUMNS = [
    ("name", "item", 20, "s"),
    ("buy_px", "buy", 8, ",d"),
    ("sell_px", "sell", 8, ",d"),
    ("margin_abs", "net", 6, ",d"),
    ("margin_pct", "mg%", 6, ".1%"),
    ("capacity", "qty", 7, ",d"),
    ("capital_deployed", "deploy", 9, ",d"),
    ("fill_eta_h", "eta(h)", 6, ".1f"),
    ("p_complete", "fill", 5, ".0%"),
]
# shown only when the persistence stage ran
_PERSIST_COLUMNS = [
    ("persist", "persist", 7, ".0%"),
    ("exp_gp_cycle_adj", "gp/cyc", 9, ",.0f"),
    ("score", "SCORE", 10, ",.0f"),
]
_SNAPSHOT_COL = [
    ("exp_gp_cycle", "gp/cyc", 9, ",.0f"),
    ("score", "SCORE", 10, ",.0f"),
]

_MODE_NOTE = {
    "online": "SCORE = gp per real-time HOUR (fast fills win — you're at the keyboard)",
    "offline": "SCORE = gp per CYCLE (fat margins win — fill speed ignored, you're away)",
    "balanced": "SCORE = gp/cycle ÷ √fill_eta (balanced speed vs margin)",
}


def format_table(df: pd.DataFrame, mode: str = "balanced") -> str:
    if df.empty:
        return "(no flips passed the filters)"
    cols = _BASE_COLUMNS + (_PERSIST_COLUMNS if "exp_gp_cycle_adj" in df.columns else _SNAPSHOT_COL)
    header = " ".join(f"{title:>{w}}" if fmt != "s" else f"{title:<{w}}"
                      for _, title, w, fmt in cols)
    lines = [header, "-" * len(header)]
    for _, row in df.iterrows():
        cells = []
        for col, _title, w, fmt in cols:
            val = row[col]
            if val is None or (isinstance(val, float) and pd.isna(val)):
                cells.append(f"{'—':>{w}}" if fmt != "s" else f"{'—':<{w}}")
            elif fmt == "s":
                cells.append(f"{str(val)[:w]:<{w}}")
            else:
                cells.append(f"{val:>{w}{fmt}}")
        lines.append(" ".join(cells))
    lines.append("")
    lines.append("KEY  net = gp/unit after tax · qty = units you can afford · fill = P(both legs fill) · "
                 "gp/cyc = net×qty×fill (one buy→sell)")
    lines.append(f"     SCORE = ranking key only, NOT gp  ·  [{mode}] " + _MODE_NOTE.get(mode, _MODE_NOTE["balanced"]))
    if "exp_gp_cycle_adj" in df.columns:
        lines.append(f"     scores shrunk for the optimizer's curse · spread across your top {config.GE_SLOTS} — don't all-in #1")
    return "\n".join(lines)


def format_bond_line(progress: dict) -> str:
    price, pct = progress.get("bond_price"), progress.get("pct")
    if not price:
        return "bond: price unavailable"
    return f"bond: {price:,.0f} gp  |  bankroll {progress['bankroll']:,} = {pct:.1f}% of a bond"


def format_portfolio_summary(df: pd.DataFrame, bankroll: int, slots: int = config.GE_SLOTS) -> str:
    """The 'worth it' check: greedily split your cash across the top `slots` flips (each
    capped by what it can absorb), then report realistic deployed / idle / gp-per-cycle."""
    if df.empty or "capital_deployed" not in df.columns or not bankroll:
        return ""
    gp_col = "exp_gp_cycle_adj" if "exp_gp_cycle_adj" in df.columns else "exp_gp_cycle"
    remaining, deployed, gp_total, used = float(bankroll), 0.0, 0.0, 0
    for _, r in df.head(slots).iterrows():
        cap_dep = float(r["capital_deployed"])
        if cap_dep <= 0 or remaining <= 0:
            continue
        alloc = min(remaining, cap_dep)
        gp_total += float(r[gp_col]) * (alloc / cap_dep)  # pro-rate gp by capital actually placed
        deployed += alloc
        remaining -= alloc
        used += 1
    idle = max(0.0, bankroll - deployed)
    line = (f"best {used}-slot allocation deploys {deployed:,.0f} of {bankroll:,.0f} "
            f"({deployed / bankroll * 100:.0f}%, {idle:,.0f} idle)  →  ~{gp_total:,.0f} gp/cycle")
    if idle > 0.5 * bankroll:
        if config.MEMBERS:
            line += ("\n⚠ over half your cash is idle — not enough flips clear the floor. "
                     "Use --mode offline or widen the item set.")
        else:
            line += ("\n⚠ over half your cash is idle — F2P flips can't absorb it. Use --mode offline, run "
                     "more items, or grind toward a bond (members = 8 slots + a far deeper market).")
    return line


def format_portfolio(picks: list[dict], bankroll: int, held=None, idle: float = 0.0,
                     free_slots: int = 0, slot_source: str = "assumed") -> str:
    """Render the two-tier plan: active flips (work in your slots) + hold (accumulate)."""
    held = held or []
    n_active = sum(p["tier"] != "hold" for p in picks)
    lines = [f"=== portfolio · cash {bankroll:,} · {len(held)} held · {n_active} active + "
             f"{len(picks) - n_active} accumulate ==="]
    if slot_source in ("live", "runelite"):
        lines.append(f"  free slots = {free_slots} (LIVE — read from your open GE offers)")
    elif slot_source == "specified":
        lines.append(f"  free slots = {free_slots} (you specified)")
    else:
        lines.append(f"  free slots = {free_slots} (ASSUMED: {config.GE_SLOTS} − {len(held)} holding inventory). "
                     f"Have pending offers? run `port <n>` or let RuneLite report them.")
    if not picks:
        if free_slots <= 0:
            lines.append("  all GE slots are busy — collect a finished offer to free one, then run `port` again.")
        else:
            lines.append("  (nothing passed the filters)")
    else:
        lines.append(f"  {'#':>2} {'type':9} {'item':22} {'buy':>7} {'sell':>7} {'qty':>8} "
                     f"{'deploy':>9} {'ETA':>5} {'fillBy':>6} {'roi%':>6} {'gp':>9}")
        lines.append("  " + "-" * 97)
        tot_dep = 0.0
        for i, p in enumerate(picks, 1):
            label = p["tier"] if p["tier"] != "hold" else "hold↓"
            eta = p.get("buy_eta_h", float("inf"))
            fb = p.get("fill_by_h", float("inf"))
            eta_s = f"{eta:.1f}h" if eta < 100 else "—"
            fb_s = f"{fb:.1f}h" if fb < 100 else "—"
            roi_s = f"{p['margin_abs'] / p['buy_px'] * 100:.1f}%" if p.get("buy_px") else "—"
            lines.append(f"  {i:>2} {label:9} {p['name'][:22]:22} {p['buy_px']:>7,} {p['sell_px']:>7,} "
                         f"{p['qty']:>8,} {p['deploy']:>9,} {eta_s:>5} {fb_s:>6} {roi_s:>6} {p['gp']:>9,.0f}")
            tot_dep += p["deploy"]
        lines.append("  " + "-" * 97)
        active_gp = sum(p["gp"] for p in picks if p["tier"] != "hold")
        hold_gp = sum(p["gp"] for p in picks if p["tier"] == "hold")
        n_now = sum(1 for p in picks if p.get("place_at_h", 0) == 0)
        makespan = max((p.get("fill_by_h", 0) for p in picks), default=0)
        lines.append(f"  deploy {tot_dep:,.0f} of {bankroll:,} ({idle:,.0f} idle)")
        lines.append(f"  est. profit: ~{active_gp:,.0f} gp/CYCLE (active flips) + ~{hold_gp:,.0f} gp WHEN-SOLD (holds)")
        lines.append(f"  place #1-#{n_now} NOW (your free slots); each next as a slot fills · ~{makespan:.1f}h to place all")
        lines.append("  KEY  gp = active: per buy→sell cycle · hold↓: total once sold (different time bases — not added)")
        lines.append("       ETA = fill once placed · fillBy = when actually bought given your slot count")
        lines.append("       roi% = margin kept per gp deployed · rows ranked by ROI-tilted gp/hour")
    if held:
        lines.append("  held (selling): " + ", ".join(f"{h.name} ({h.qty:,})" for h in held[:6]))
    if picks and free_slots > 0 and bankroll and idle > 0.5 * bankroll:
        if config.MEMBERS:
            lines.append("  ⚠ still over half idle — not enough flips clear the quality floor right now; "
                         "the rest stays liquid rather than churning junk.")
        else:
            lines.append("  ⚠ still over half idle — even buy-limit accumulation can't absorb it; "
                         "the F2P market is the ceiling. A bond opens the members market.")
    return "\n".join(lines)


def format_overnight(rows: list[dict], cash: int, free: int) -> str:
    """One big buy per free slot — what you can actually place before sleeping."""
    if not rows:
        return "  no safe overnight buys for your free slots right now"
    lines = [f"  {free} free slot(s) — place these {len(rows)} buy(s) now, then sleep:",
             f"  {'#':>2} {'item':22} {'buy':>7} {'sell':>7} {'qty':>8} {'deploy':>9} {'fill8h':>6} {'profit':>9}"]
    tot_dep = tot_prof = 0.0
    for i, r in enumerate(rows, 1):
        prof = color(f"+{r['profit']:,.0f}", "green")
        lines.append(f"  {i:>2} {str(r['name'])[:22]:22} {r['buy']:>7,} {r['sell']:>7,} "
                     f"{r['qty']:>8,} {r['deploy']:>9,} {r['fill8h']:>5.0%} {prof:>9}")
        tot_dep += r["deploy"]
        tot_prof += r["profit"]
    lines.append(f"  deploy {tot_dep:,.0f} of {cash:,} ({cash - tot_dep:,.0f} idle) · ~{tot_prof:,.0f} gp when sold")
    lines.append("  fill8h = est. fraction that fills overnight · `brief` at wake to collect + sell")
    return "\n".join(lines)


def format_sell_quote(name: str, qty: int, avg_cost: float, rows: list[dict]) -> str:
    """The sell-price tradeoff curve for a held item."""
    if not rows:
        return "  no sell data for that item"
    lines = [f"  SELL {name} — {qty:,} units @ avg cost {avg_cost:,.0f}",
             f"  {'list@':>7} {'fill_eta':>9} {'net/ea':>7} {'total':>11}"]
    for r in rows:
        eta = f"{r['eta_h']:.1f}h" if r["eta_h"] < 100 else "won't fill"
        prof = color(f"{r['profit']:+,.0f}", "green" if r["net_unit"] >= 0 else "red")
        lines.append(f"  {r['price']:>7,} {eta:>9} {r['net_unit']:>+7,} {prof:>11}")
    lines.append("  higher list price = more profit but slower fill (and may not fill at all)")
    return "\n".join(lines)


def format_sell_plan(rows: list[dict]) -> str:
    """Recommended sell listings for inventory you hold (and aren't already selling)."""
    if not rows:
        return ""
    lines = ["  SELL your holdings:",
             f"  {'item':22} {'qty':>7} {'avg':>8} {'sell@':>8} {'eta':>6} {'profit':>9}"]
    for r in rows:
        eta_s = f"{r['eta_h']:.1f}h" if r["eta_h"] < 100 else "—"
        prof = color(f"{r['profit']:+,.0f}", "green" if r["profit"] >= 0 else "red")
        flag = color("  ⚠ underwater — break-even hold", "yellow") if r.get("underwater") else ""
        lines.append(f"  {str(r['name'])[:22]:22} {r['qty']:>7,} {r['avg_cost']:>8,.0f} "
                     f"{r['sell_px']:>8,} {eta_s:>6} {prof:>9}{flag}")
    return "\n".join(lines)


def format_quote(q) -> str:
    """Render an optimal-quote result with per-leg fill probabilities and the frontier."""
    if q is None:
        return "(no quote — insufficient data or no profitable price exists)"
    lines = [
        f"=== {q.name} — optimal quote (qty {q.qty:,}, {q.horizon_h:g}h horizon) ===",
        f"market: bid {q.bid:,} / ask {q.ask:,}",
        f"RECOMMEND  buy {q.buy_px:,}  sell {q.sell_px:,}  →  net {q.net_unit:,}/unit  |  "
        f"fill: buy {q.p_buy:.0%} · sell {q.p_sell:.0%} · round {q.p_round:.0%}  |  EV {q.ev:,.0f} gp",
        "",
        "frontier (thin/fast → fat/slow):",
        f"  {'buy':>6} {'sell':>6} {'net':>5} {'buy%':>6} {'sell%':>6} {'round%':>7} {'EV':>10}",
        "  " + "-" * 50,
    ]
    for r in q.frontier:
        lines.append(
            f"  {r['buy']:>6,} {r['sell']:>6,} {r['net_unit']:>5,} {r['p_buy']:>6.0%} "
            f"{r['p_sell']:>6.0%} {r['p_round']:>7.0%} {r['ev']:>10,.0f}"
        )
    lines.append("")
    lines.append(f"KEY  net = gp/unit after tax · buy%/sell% = fraction of {q.qty:,} filled within {q.horizon_h:g}h "
                 f"· round% = both legs")
    lines.append(f"     EV = net × qty × round% = expected gp realised within {q.horizon_h:g}h")
    return "\n".join(lines)


def post_discord(content: str, webhook_url: str | None = None) -> tuple[bool, str]:
    """Post a fenced message to a Discord webhook. Returns (ok, detail) — detail explains
    the failure (no webhook / HTTP status / exception) so callers can surface why, instead
    of a bare silent False. ANSI colour codes are stripped (they render as garbage in Discord)."""
    url = webhook_url or config.DISCORD_WEBHOOK_URL
    if not url:
        return False, "no webhook configured — set OSRS_FLIPPER_DISCORD_WEBHOOK"
    clean = _ANSI_RE.sub("", content)[:1900]
    try:
        resp = get_session().post(url, json={"content": f"```\n{clean}\n```"}, timeout=config.HTTP_TIMEOUT)
        return (True, "sent") if resp.ok else (False, f"HTTP {resp.status_code}: {resp.text[:120]}")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def to_discord(content: str, webhook_url: str | None = None) -> bool:
    """Back-compat boolean wrapper around post_discord (True on success)."""
    return post_discord(content, webhook_url)[0]


# --- Discord BOT push (posts as your bot; can edit a live status message) --------------------------
_DISCORD_API = "https://discord.com/api/v10"


def bot_enabled() -> bool:
    return bool(config.DISCORD_BOT_TOKEN and config.DISCORD_CHANNEL_ID)


def _fenced(content: str) -> str:
    return f"```\n{_ANSI_RE.sub('', content)[:1900]}\n```"


def post_bot(content: str) -> tuple[bool, str]:
    """Post via the Discord bot REST API (as your bot). Returns (ok, message_id-or-detail)."""
    if not bot_enabled():
        return False, "no bot configured — set OSRS_FLIPPER_DISCORD_BOT_TOKEN + _CHANNEL_ID"
    try:
        r = get_session().post(
            f"{_DISCORD_API}/channels/{config.DISCORD_CHANNEL_ID}/messages",
            headers={"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"},
            json={"content": _fenced(content)}, timeout=config.HTTP_TIMEOUT)
        return (True, str(r.json().get("id"))) if r.ok else (False, f"HTTP {r.status_code}: {r.text[:120]}")
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def edit_bot(message_id: str, content: str) -> bool:
    """Edit an existing bot message (the live status message) — how we update without spamming."""
    if not (bot_enabled() and message_id):
        return False
    try:
        r = get_session().patch(
            f"{_DISCORD_API}/channels/{config.DISCORD_CHANNEL_ID}/messages/{message_id}",
            headers={"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"},
            json={"content": _fenced(content)}, timeout=config.HTTP_TIMEOUT)
        return r.ok
    except Exception:  # noqa: BLE001
        return False


def notify(content: str) -> bool:
    """Push a discrete alert — via the bot if configured, else the webhook."""
    return (post_bot(content)[0] if bot_enabled() else post_discord(content)[0])


def delete_bot(message_id: str) -> bool:
    """Delete a bot message — used to remove the previous status message when reposting a fresh one."""
    if not (bot_enabled() and message_id):
        return False
    try:
        r = get_session().delete(
            f"{_DISCORD_API}/channels/{config.DISCORD_CHANNEL_ID}/messages/{message_id}",
            headers={"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"}, timeout=config.HTTP_TIMEOUT)
        return r.ok
    except Exception:  # noqa: BLE001
        return False


def repost_status(text: str, prev_msg_id: str | None = None) -> str | None:
    """Push the live status as a NEW message so it lands at the BOTTOM of the channel (no scrolling up to
    a stale, edited-in-place message that newer pings buried), then delete the previous status so only one
    ever exists. Posts first, deletes second — if the post fails the old message is kept. Returns the new
    message id, or the previous id on failure."""
    if not bot_enabled():
        return None
    ok, mid = post_bot(text)
    if not ok:
        return prev_msg_id
    if prev_msg_id:
        delete_bot(prev_msg_id)
    return mid
