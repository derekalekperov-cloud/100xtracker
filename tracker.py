#!/usr/bin/env python3
"""Hyperliquid wallet tracker: Telegram alert on every new trade + daily summary.

Usage:
  python tracker.py           # check for new trades, alert if any
  python tracker.py --daily   # send the daily summary
  python tracker.py --dry-run # print instead of sending

Env: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

Tracked wallets live in state.json. Money movements are watched too: big transfers
out, withdrawals, top-ups, liquidations, and the account being emptied. If he sends
funds straight to another Hyperliquid address, that address is tracked automatically.
"""
import json, os, sys, time, urllib.request
from collections import defaultdict
from datetime import datetime, timezone

DEFAULT_WALLETS = {
    "0xe81309ec9b79309e4d108ae3a92d1aa618cd6d74": "main wallet",
    "0x89da4baec446f35a1cbe17a9d1ee5c70b05ee43f": "old wallet",
}
# Top traders shortlisted from the Hyperliquid leaderboard (Nov 2026). Their alerts are
# tagged with a ⭐ and their own label so they never mix with the fund manager's.
# Money sent from these wallets to new addresses is reported but not auto-followed.
TOP_TRADERS = {
    "0x015354106478dda69c4aae3c0cf801290b738052": "Top 1 · BTC/ZEC short-seller",
    "0xfce053a5e461683454bf37ad66d20344c0e3f4c0": "Top 2 · BTC trader",
    "0x9c16bc8f1104e4d2f72267eb981fa12de7cc4a6f": "Top 3 · SOL/BTC/ETH",
    "0xf899937184168b1d9dee75acbaa3fef0f52888db": "Top 4 · SOL/BTC/ETH short-seller",
    "0x15a4f009bb324a3fb9e36137136b201e3fe0dfdb": "Top 5 · big ETH/BTC whale",
}
# Long-history traders (consistent records over many months). Tagged 🏅.
# "batch": very active wallets get one hourly digest instead of an alert per trade.
PRO_TRADERS = {
    "0x885989fd94d30c150e6eaf897090509bce4f6aa8": ("Pro A", False),
    "0x987163b6b482c30c2f5f3aa2760109668eb0091d": ("Pro B", False),
    "0x2312171890250347dbf1b082bdf7950504909e92": ("Pro C", False),
    "0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1": ("Pro D · Loco (very active)", True),
    "0x8af700ba841f30e0a3fcb0ee4c4a9d223e1efa05": ("Pro E", False),
}
# Your own account: set the MY_WALLET repo secret to your public address. It is stored
# in state.json under the key "me", so the address never appears in this public repo.
MY_KEY = "me"
EXTRA_DEXES = ["xyz"]  # HIP-3 dexes with stock/commodity perps (xyz:NVDA, xyz:GOLD, ...)
WALLET = None          # address currently being processed (set in main loop)
KEY = None             # its key in state.json (the address, or "me")
MIN_MOVE = 50_000      # USD; smaller transfers are ignored
SYSTEM_PREFIX = "0x2000000000000000000000000000000000"  # HyperEVM bridge addresses
API = "https://api.hyperliquid.xyz/info"
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
DRY = "--dry-run" in sys.argv


def info(body):
    req = urllib.request.Request(API, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def load_state():
    try:
        return json.load(open(STATE))
    except FileNotFoundError:
        return {}  # first run: only future activity is reported


def wallets(state):
    """Upgrade old single-wallet state and make sure defaults are tracked."""
    now = int(time.time() * 1000)
    w = state.setdefault("wallets", {})
    for addr, label in DEFAULT_WALLETS.items():
        if addr not in w:
            w[addr] = {"label": label, "last_fill_ms": now, "last_ledger_ms": now, "peak": 0}
    for addr, label in TOP_TRADERS.items():
        if addr not in w:
            w[addr] = {"label": "⭐ " + label, "last_fill_ms": now, "last_ledger_ms": now,
                       "peak": 0, "follow": False}
    for addr, (label, batch) in PRO_TRADERS.items():
        if addr not in w:
            w[addr] = {"label": "🏅 " + label, "last_fill_ms": now, "last_ledger_ms": now,
                       "peak": 0, "follow": False, "batch": batch}
    if os.environ.get("MY_WALLET") and MY_KEY not in w:
        w[MY_KEY] = {"label": "👤 You", "last_fill_ms": now, "last_ledger_ms": now,
                     "peak": 0, "follow": False}
    main = w["0xe81309ec9b79309e4d108ae3a92d1aa618cd6d74"]
    if "last_fill_ms" in state:  # from the first version
        main["last_fill_ms"] = state.pop("last_fill_ms")
    return w


def short(a):
    return a[:6] + "…" + a[-4:]


def save_state(s):
    json.dump(s, open(STATE, "w"), indent=2)


def px(x):
    x = float(x)
    return f"{x:,.2f}" if x >= 1 else f"{x:.6g}"


def usd(x):
    x = float(x)
    s = "-" if x < 0 else ""
    x = abs(x)
    return f"{s}${x/1e6:.2f}M" if x >= 1e6 else f"{s}${x/1e3:.1f}k" if x >= 1e3 else f"{s}${x:.0f}"


def equity(pf):
    """Total account value as Hyperliquid reports it (perps + spot, no double counting)."""
    h = pf.get("day", {}).get("accountValueHistory") or [[0, 0]]
    return float(h[-1][1])


def snapshot():
    cs = info({"type": "clearinghouseState", "user": WALLET})
    pf = dict(info({"type": "portfolio", "user": WALLET}))
    mids = info({"type": "allMids"})
    positions = [p["position"] for p in cs["assetPositions"]]
    ntl = float(cs["marginSummary"]["totalNtlPos"])
    for dex in EXTRA_DEXES:  # stock/commodity perps (e.g. xyz:NVDA) live on separate dexes
        try:
            x = info({"type": "clearinghouseState", "user": WALLET, "dex": dex})
            positions += [p["position"] for p in x.get("assetPositions", [])]
            ntl += float(x["marginSummary"]["totalNtlPos"])
            if x.get("assetPositions"):
                mids.update(info({"type": "allMids", "dex": dex}))
        except Exception:
            pass
    def pnl(k):
        h = pf.get(k, {}).get("pnlHistory") or [[0, 0]]
        return float(h[-1][1])
    return {
        "acct": equity(pf) or float(cs["marginSummary"]["accountValue"]),
        "ntl": ntl,
        "positions": positions,
        "pnl_day": pnl("day"), "pnl_week": pnl("week"),
        "pnl_month": pnl("month"), "pnl_all": pnl("allTime"),
        "mids": mids,
    }


def group(fills):
    g = defaultdict(lambda: {"sz": 0.0, "ntl": 0.0, "pnl": 0.0, "fee": 0.0, "t": 0, "start": None})
    for f in sorted(fills, key=lambda f: f["time"]):
        k = (f["coin"], f["dir"])
        if g[k]["start"] is None:
            g[k]["start"] = float(f.get("startPosition") or 0)
        sz = float(f["sz"])
        g[k]["sz"] += sz; g[k]["ntl"] += sz * float(f["px"])
        g[k]["pnl"] += float(f["closedPnl"]); g[k]["fee"] += float(f["fee"])
        g[k]["t"] = max(g[k]["t"], f["time"])
    return g


def describe(d, coin, v, snap):
    """Turn Hyperliquid's fill direction into a plain-language headline.
    Closing a long is NOT a short, and closing a short is NOT a long."""
    pos = next((p for p in snap["positions"] if p["coin"] == coin), None)
    now = float(pos["szi"]) if pos else 0.0
    start = v["start"] or 0.0
    if d == "Open Long":
        return ("🟢", f"NEW LONG {coin}" if start == 0 else f"ADDED TO LONG {coin}",
                "Betting the price goes UP")
    if d == "Open Short":
        return ("🔴", f"NEW SHORT {coin}" if start == 0 else f"ADDED TO SHORT {coin}",
                "Betting the price goes DOWN")
    if d == "Close Long":
        full = now <= 0
        return ("⚪", f"{'CLOSED' if full else 'REDUCED'} LONG {coin}",
                "Taking profit/loss on a long, this is NOT a short"
                + ("" if full else f" (still long {now:,.4g} {coin})"))
    if d == "Close Short":
        full = now >= 0
        return ("⚪", f"{'CLOSED' if full else 'REDUCED'} SHORT {coin}",
                "Exiting a short, this is NOT a new long"
                + ("" if full else f" (still short {abs(now):,.4g} {coin})"))
    if d == "Long > Short":
        return ("🔴", f"FLIPPED LONG → SHORT {coin}", "Closed the long AND opened a short (now betting DOWN)")
    if d == "Short > Long":
        return ("🟢", f"FLIPPED SHORT → LONG {coin}", "Closed the short AND opened a long (now betting UP)")
    return ("🔵", f"{d.upper()} {coin}", "Spot trade" if d in ("Buy", "Sell") else d)


# ───────────────────────── Message design ─────────────────────────
# Every alert follows the same layout:
#   ICON  TITLE                      ← what happened, in capitals
#   label · 0xabc…1234               ← whose wallet
#   ┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈
#   emoji  Field   value             ← the numbers
#   ┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈
#   ⚡ Key info (quote block)         ← one-glance summary
#   footer + buttons (wallet / chart)
LINE = "┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"


def madrid_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Madrid"))
    except Exception:
        return datetime.now(timezone.utc)


def who(ws):
    """Nickname for use inside sentences."""
    return nick(KEY, ws)[1] if KEY else ws.get("label", "wallet")


def sub(ws, addr):
    badge, name, tag = nick(KEY, ws)
    return f"{badge} <b>{name}</b>" + (f" · {tag}" if tag else "") + f" · <code>{short(addr)}</code>"


def field(emoji, name, value):
    return f"{emoji} {name}: <b>{value}</b>"


def key_info(text):
    return f"<blockquote>⚡ <b>Key info</b>\n{text}</blockquote>"


def footer():
    return f"<i>Not financial advice · {madrid_now():%H:%M} Madrid</i>"


def pct(x):
    return f"{x:+.1f}%"


def buttons(addr, coin=None):
    row = [{"text": "🔍 Wallet", "url": f"https://hypurrscan.io/address/{addr}"}]
    if coin:
        row.append({"text": f"📈 {coin} chart", "url": f"https://app.hyperliquid.xyz/trade/{coin}"})
    return [row]


def send(text, kb=None):
    if DRY:
        extra = ("\n[" + "] [".join(b["text"] for r in kb for b in r) + "]") if kb else ""
        print(text + extra + "\n" + "-" * 40)
        return
    tok, chat = os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    msg = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if kb:
        msg["reply_markup"] = {"inline_keyboard": kb}
    urllib.request.urlopen(urllib.request.Request(
        f"https://api.telegram.org/bot{tok}/sendMessage", json.dumps(msg).encode(),
        {"Content-Type": "application/json"}), timeout=30).read()


def risk_read(snap, coin):
    """Rule-based, informational risk read. Not financial advice.
    Returns (level 0-2, flags, facts)."""
    flags, facts = [], {}
    pos = next((p for p in snap["positions"] if p["coin"] == coin), None)
    acct = snap["acct"] or 1
    eff = snap["ntl"] / acct
    reds = 0
    if pos:
        mid = float(snap["mids"].get(coin, pos["entryPx"]))
        facts["mid"] = mid
        facts["lev"] = pos["leverage"]["value"]
        liq = pos.get("liquidationPx")
        if liq:
            dist = abs(mid - float(liq)) / mid * 100
            facts["dist"] = dist
            dot = "🔴" if dist < 15 else "🟡" if dist < 35 else "🟢"
            reds += dist < 15
            flags.append(f"{dot} Liquidation {px(liq)} ({dist:.0f}% away)")
        share = float(pos["positionValue"]) / max(snap["ntl"], 1) * 100
        if share > 80 and len(snap["positions"]) == 1:
            flags.append(f"🟡 All-in: 100% of exposure in {coin}")
    dot = "🔴" if eff > 5 else "🟡" if eff > 2.5 else "🟢"
    reds += eff > 5
    flags.append(f"{dot} Leverage {eff:.1f}x on {usd(acct)}")
    dot = "🔴" if snap["pnl_all"] < 0 else "🟢"
    reds += snap["pnl_all"] < 0
    flags.append(f"{dot} Track record {usd(snap['pnl_all'])} all-time · {usd(snap['pnl_month'])} 30d")
    facts["eff"] = eff
    return min(reds, 2), flags, facts


RISK = ["🟢 Contained", "🟡 Elevated", "🔴 High"]


def trade_summary(ws, d, coin, v, level, facts, snap):
    name = who(ws)
    size = usd(v["ntl"])
    lev = facts.get("lev")
    levs = f" at {lev}x" if lev else ""
    if d.startswith("Open"):
        side = "long" if "Long" in d else "short"
        verb = "added to a" if (v["start"] or 0) else "opened a"
        s = f"{name} {verb} {size} {coin} {side}{levs}."
        if "dist" in facts:
            s += f" Liquidation is {facts['dist']:.0f}% away."
    elif d.startswith("Close"):
        side = "long" if "Long" in d else "short"
        res = "profit" if v["pnl"] >= 0 else "loss"
        s = (f"{name} closed {'part of ' if v.get('partial') else ''}a {side} on {coin} for a {usd(abs(v['pnl']))} {res}. "
             f"This is an exit, not a new {'short' if side == 'long' else 'long'}.")
    elif ">" in d:
        s = f"{name} flipped {coin} to {'SHORT' if d.endswith('Short') else 'LONG'} with {size}{levs}."
    else:
        s = f"{name}: {d} {coin} for {size}."
    tail = {0: "Risk looks contained.", 1: "Some risk flags, size carefully.",
            2: "Several red flags, high risk to copy."}[level]
    if snap["pnl_all"] < 0 and ws.get("label") != "👤 You":
        tail += f" Note: this wallet is {usd(snap['pnl_all'])} all-time."
    return f"{s} {tail}"


def check_money(state, w):
    """Alert on money moving in/out, liquidations, and the account being emptied."""
    me = WALLET
    ws = state["wallets"][KEY]
    ups = info({"type": "userNonFundingLedgerUpdates", "user": me,
                "startTime": ws["last_ledger_ms"] + 1})
    head = lambda icon, title: [f"{icon} <b>{title}</b>", sub(ws, me), LINE]
    out = lambda lines, summary, kb=None: send("\n".join(lines + [LINE, key_info(summary), footer()]),
                                              kb or buttons(me))
    for u in sorted(ups, key=lambda u: u["time"]):
        d = u["delta"]; t = d["type"]
        amt = float(d.get("usdcValue") or d.get("usdc") or d.get("amount") or 0)
        if t == "liquidation":
            out(head("🚨", "LIQUIDATED") + ["💀 Positions were force-closed by the exchange."],
                f"{who(ws)} got liquidated. Any copied positions in the same coins are in danger.")
        elif t == "withdraw" and amt >= MIN_MOVE:
            out(head("💸", f"WITHDRAWAL · {usd(amt)}") + [field("📤", "Amount", usd(amt)),
                "🏦 Destination: outside Hyperliquid"],
                f"{who(ws)} pulled {usd(amt)} off the exchange. A new wallet often appears with a similar deposit.")
        elif t in ("send", "spotTransfer") and amt >= MIN_MOVE:
            dest, src = d.get("destination", "").lower(), d.get("user", "").lower()
            if src == me and dest != me:
                if dest.startswith(SYSTEM_PREFIX):
                    out(head("💸", f"MOVED OFF HYPERLIQUID · {usd(amt)}") + [field("📤", "Amount", usd(amt)),
                        "🌉 Via the HyperEVM bridge"],
                        f"{who(ws)} moved {usd(amt)} off Hyperliquid, the same way the fund manager left his old wallet on Sep 17.")
                elif dest in state["wallets"]:
                    out(head("↔️", f"INTERNAL MOVE · {usd(amt)}") + [
                        f"➡️ To: <b>{state['wallets'][dest]['label']}</b> <code>{short(dest)}</code>"],
                        f"Money moved between two wallets you already track. Nothing new to follow.")
                elif not ws.get("follow", True):
                    out(head("↗️", f"SENT OUT · {usd(amt)}") + [f"➡️ To: <code>{dest}</code>"],
                        f"{who(ws)} sent {usd(amt)} to another wallet (not auto-followed for this trader).",
                        buttons(dest))
                else:
                    now = int(time.time() * 1000)
                    state["wallets"][dest] = {"label": f"🧭 Followed from {short(me)}", "last_fill_ms": now,
                                              "last_ledger_ms": now, "peak": 0}
                    out(head("🧭", f"NEW WALLET FOUND · {usd(amt)}") + [f"➡️ To: <code>{dest}</code>",
                        "✅ Now tracking it automatically"],
                        f"{who(ws)} sent {usd(amt)} to a new wallet. You'll get its trades from now on.",
                        buttons(dest))
            elif dest == me and src not in state["wallets"]:
                out(head("💰", f"FUNDS ADDED · {usd(amt)}") + [f"⬅️ From: <code>{short(src)}</code>"],
                    f"{who(ws)} topped up by {usd(amt)}. More capital often means bigger positions next.")
        elif t == "deposit" and amt >= MIN_MOVE:
            out(head("💰", f"DEPOSIT · {usd(amt)}") + [field("📥", "Amount", usd(amt))],
                f"{who(ws)} brought {usd(amt)} of new money onto Hyperliquid.")
    if ups:
        ws["last_ledger_ms"] = max(u["time"] for u in ups)
    acct = equity(dict(info({"type": "portfolio", "user": me})))
    ws["peak"] = max(ws.get("peak", 0), acct)
    if ws["peak"] >= 1e6 and acct < 0.1 * ws["peak"] and not ws.get("emptied_alerted"):
        out(head("⚠️", "WALLET EMPTIED") + [field("📉", "Now", usd(acct)), field("🏔", "Peak", usd(ws["peak"]))],
            f"{who(ws)} drained this wallet ({usd(acct)} left of {usd(ws['peak'])}). Check the alerts above for where it went.")
        ws["emptied_alerted"] = True
    elif acct >= 0.5 * ws["peak"]:
        ws["emptied_alerted"] = False


def check_trades(state):
    ws = state["wallets"][KEY]
    if ws.get("batch") and time.time() - ws.get("last_batch", 0) < 3600:
        return  # busy wallet: checked once an hour, sent as one digest
    fills = info({"type": "userFillsByTime", "user": WALLET,
                  "startTime": ws["last_fill_ms"] + 1, "aggregateByTime": True})
    ws["last_batch"] = time.time()
    if not fills:
        return
    snap = snapshot()
    groups = sorted(group(fills).items(), key=lambda kv: kv[1]["t"])
    if ws.get("batch"):
        realized = sum(v["pnl"] for _, v in groups)
        lines = [f"🗂 <b>HOURLY DIGEST · {len(groups)} moves</b>", sub(ws, WALLET), LINE]
        for (coin, d), v in groups:
            icon, head, _ = describe(d, coin, v, snap)
            words = head.split()
            pretty = " ".join(w.capitalize() for w in words[:-1]) + " " + words[-1]
            lines.append(f"{icon} {pretty} · <b>{usd(v['ntl'])}</b> @ {px(v['ntl']/v['sz'])}"
                         + (f" · {usd(v['pnl'])}" if "Close" in d or ">" in d else ""))
        lines += [LINE, field("💰", "Account", usd(snap["acct"])), field("📆", "30d PnL", usd(snap["pnl_month"]))]
        coins = sorted({c for (c, _), _ in groups})
        lines.append(key_info(f"{who(ws)} made {len(groups)} moves in {', '.join(coins)} this hour, "
                              f"realizing {usd(realized)}."))
        lines.append(footer())
        send("\n".join(lines), buttons(WALLET))
        ws["last_fill_ms"] = max(f["time"] for f in fills)
        return
    for (coin, d), v in groups:
        icon, head, meaning = describe(d, coin, v, snap)
        level, flags, facts = risk_read(snap, coin)
        avg = v["ntl"] / v["sz"]
        szs = f"{v['sz']:,.0f}" if v["sz"] >= 100 else f"{v['sz']:,.4f}"
        lines = [f"{icon} <b>{head}</b>", sub(ws, WALLET), f"<i>{meaning}</i>", LINE,
                 field("💵", "Size", f"{usd(v['ntl'])}") + f"  ({szs} {coin})",
                 field("🎯", "Price", px(avg))]
        if "mid" in facts:
            lines[-1] += f"  · now {px(facts['mid'])} ({pct((facts['mid'] / avg - 1) * 100)})"
        if facts.get("lev") and not d.startswith("Close"):
            lines.append(field("⚖️", "Leverage", f"{facts['lev']}x"))
        if "Close" in d or ">" in d:
            res = v["pnl"]
            lines.append(field("✅" if res >= 0 else "❌", "Realized", usd(res)))
        lines += [LINE, f"🛡 <b>Risk: {RISK[level]}</b>"] + flags
        lines += [key_info(trade_summary(ws, d, coin, v, level, facts, snap)), footer()]
        send("\n".join(lines), buttons(WALLET, coin))
    ws["last_fill_ms"] = max(f["time"] for f in fills)


def pos_line(p):
    long = float(p["szi"]) > 0
    upnl = float(p["unrealizedPnl"])
    liq = p.get("liquidationPx")
    return (f"{'🟢' if long else '🔴'} <b>{'Long' if long else 'Short'} {p['coin']}</b> {usd(p['positionValue'])}"
            f" · {p['leverage']['value']}x · entry {px(p['entryPx'])}\n"
            f"     {'📈' if upnl >= 0 else '📉'} uPnL <b>{usd(upnl)}</b>" + (f" · liq {px(liq)}" if liq else ""))


def daily(state):
    now = int(time.time() * 1000)
    fills = info({"type": "userFillsByTime", "user": WALLET,
                  "startTime": now - 86_400_000, "aggregateByTime": True})
    snap = snapshot()
    if snap["acct"] < 1 and not fills:
        return
    ws = state["wallets"][KEY]
    g = group(fills)
    realized = sum(v["pnl"] for v in g.values())
    lines = [f"☀️ <b>DAILY BRIEF · {madrid_now():%a %d %b}</b>", sub(ws, WALLET), LINE,
             field("💰", "Account", usd(snap["acct"])),
             field("📅", "24h", usd(snap["pnl_day"])) + f"  ·  7d <b>{usd(snap['pnl_week'])}</b>",
             field("🗓", "30d", usd(snap["pnl_month"])) + f"  ·  all-time <b>{usd(snap['pnl_all'])}</b>",
             LINE, "<b>🔁 Trades (24h)</b>"]
    lines += [f"• {d} {c} · {usd(v['ntl'])}" + (f" · {usd(v['pnl'])}" if "Close" in d else "")
              for (c, d), v in g.items()] or ["• No trades"]
    lines += ["", "<b>📂 Open positions</b>"]
    lines += [pos_line(p) for p in snap["positions"]] or ["• None"]
    pos = snap["positions"]
    if pos:
        big = max(pos, key=lambda p: abs(float(p["positionValue"])))
        summ = (f"{who(ws)} is {'long' if float(big['szi']) > 0 else 'short'} {big['coin']} "
                f"({usd(big['positionValue'])}, uPnL {usd(big['unrealizedPnl'])}). ")
    else:
        summ = f"{who(ws)} has no open positions. "
    summ += (f"{len(fills)} fills in the last 24h, realized {usd(realized)}." if fills else "No trades in the last 24h.")
    lines += [LINE, key_info(summ), footer()]
    send("\n".join(lines), buttons(WALLET))


# Fun, fictional nicknames (no real people). key -> (badge, nickname, short tag)
NICKNAMES = {
    "0xe81309ec9b79309e4d108ae3a92d1aa618cd6d74": ("💼", "The Insta Guru", "fund manager"),
    "0x89da4baec446f35a1cbe17a9d1ee5c70b05ee43f": ("💼", "Insta Guru (old wallet)", "old wallet"),
    "0x015354106478dda69c4aae3c0cf801290b738052": ("⭐", "The Zcash Reaper", "Top 1"),
    "0xfce053a5e461683454bf37ad66d20344c0e3f4c0": ("⭐", "The Bitcoin Butler", "Top 2"),
    "0x9c16bc8f1104e4d2f72267eb981fa12de7cc4a6f": ("⭐", "Captain 40x", "Top 3"),
    "0xf899937184168b1d9dee75acbaa3fef0f52888db": ("⭐", "Captain Short", "Top 4"),
    "0x15a4f009bb324a3fb9e36137136b201e3fe0dfdb": ("⭐", "The Desert Whale", "Top 5"),
    "0x885989fd94d30c150e6eaf897090509bce4f6aa8": ("🏅", "The Index Hunter", "Pro A"),
    "0x987163b6b482c30c2f5f3aa2760109668eb0091d": ("🏅", "Old Guard", "Pro B"),
    "0x2312171890250347dbf1b082bdf7950504909e92": ("🏅", "The Veteran", "Pro C"),
    "0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1": ("🏅", "Loco the Chip Maniac", "Pro D"),
    "0x8af700ba841f30e0a3fcb0ee4c4a9d223e1efa05": ("🏅", "The Emperor", "Pro E"),
    "me": ("👤", "You", ""),
}


def nick(key, ws=None):
    """(badge, name, tag) for a wallet; falls back to its label."""
    if key in NICKNAMES:
        return NICKNAMES[key]
    lbl = (ws or {}).get("label", "wallet")
    return (lbl[:1] if not lbl[:1].isalnum() else "🧭", lbl.lstrip("⭐🏅👤🧭 "), "")


def send_photo(png, caption, kb=None):
    if DRY:
        open("/tmp/weekly_preview.png", "wb").write(png)
        print("[photo saved to /tmp/weekly_preview.png]\n" + caption + "\n" + "-" * 40)
        return
    import uuid
    tok, chat = os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    b = uuid.uuid4().hex
    parts = []
    fields = {"chat_id": chat, "caption": caption, "parse_mode": "HTML"}
    if kb:
        fields["reply_markup"] = json.dumps({"inline_keyboard": kb})
    for k, v in fields.items():
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="photo"; filename="weekly.png"\r\n'
                 f'Content-Type: image/png\r\n\r\n'.encode() + png + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    urllib.request.urlopen(urllib.request.Request(
        f"https://api.telegram.org/bot{tok}/sendPhoto", b"".join(parts),
        {"Content-Type": f"multipart/form-data; boundary={b}"}), timeout=60).read()


def weekly_chart(rows):
    """Diverging bars of 7-day return % per wallet (so a $1M and a $50M wallet compare fairly).
    Dark theme, gains blue, losses red, dollar PnL in each label."""
    import io
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    BG, INK, MUTED, GAIN, LOSS, GRID = "#1a1a19", "#f0efec", "#c3c2b7", "#3987e5", "#e66767", "#383835"
    rows = sorted(rows, key=lambda r: r["ret"])
    vals = [r["ret"] for r in rows]
    h = 0.46 * len(rows) + 1.5
    fig, ax = plt.subplots(figsize=(8, h), dpi=200)
    fig.patch.set_facecolor(BG); ax.set_facecolor(BG)
    bars = ax.barh([r["name"] for r in rows], vals, height=0.62,
                   color=[GAIN if v >= 0 else LOSS for v in vals])
    span = max(max(abs(v) for v in vals), 1)
    for bar, r in zip(bars, rows):
        v = r["ret"]
        ax.text(v + (span * 0.03 if v >= 0 else -span * 0.03), bar.get_y() + bar.get_height() / 2,
                f"{v:+.1f}%  ({usd(r['w'])})", va="center", ha="left" if v >= 0 else "right",
                color=INK, fontsize=8.5)
    ax.axvline(0, color=MUTED, lw=1)
    lo, hi = min(vals + [0]), max(vals + [0])
    pad = max(hi - lo, 1) * 0.55
    ax.set_xlim(lo - (pad if lo < 0 else pad * 0.1), hi + (pad if hi > 0 else pad * 0.1))
    ax.xaxis.grid(True, color=GRID, lw=0.6); ax.set_axisbelow(True)
    ax.tick_params(axis="y", colors=INK, labelsize=9, length=0)
    ax.tick_params(axis="x", colors=MUTED, labelsize=8, length=0)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, _: f"{x:+.0f}%"))
    for s in ax.spines.values():
        s.set_visible(False)
    top = 1 - 0.95 / h
    fig.text(0.03, 1 - 0.25 / h, "7-day return by wallet", color=INK, fontsize=13, fontweight="bold", va="top")
    fig.text(0.03, 1 - 0.6 / h, f"Week ending {madrid_now():%d %b %Y}  ·  blue = profit, red = loss  ·  label = return (PnL)",
             color=MUTED, fontsize=8.5, va="top")
    fig.tight_layout(rect=(0, 0, 1, top))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    return buf.getvalue()


def weekly(state):
    """Weekly report: a PnL chart (photo) with a short caption, then one 'open bets' message."""
    global WALLET, KEY
    now = int(time.time() * 1000)
    rows = []
    for key, ws in list(state["wallets"].items()):
        addr = os.environ.get("MY_WALLET", "").strip().lower() if key == MY_KEY else key
        if not addr:
            continue
        WALLET, KEY = addr, key
        try:
            snap = snapshot()
            fills = info({"type": "userFillsByTime", "user": addr,
                          "startTime": now - 7 * 86_400_000, "aggregateByTime": True})
        except Exception as e:
            print(f"weekly error on {ws.get('label', '?')}: {e!r}", file=sys.stderr)
            continue
        if snap["acct"] < 1 and not fills:
            continue
        badge, name, tag = nick(key, ws)
        rows.append({"key": key, "badge": badge, "name": name, "w": snap["pnl_week"],
                     "acct": snap["acct"], "pos": snap["positions"], "fills": len(fills)})
    if not rows:
        return
    for r in rows:  # 7-day return % on starting equity, so big and small wallets compare fairly
        start = r["acct"] - r["w"]
        r["ret"] = r["w"] / start * 100 if start > 1 else 0.0
    traders = [r for r in rows if r["key"] != MY_KEY]
    ranked = sorted(traders, key=lambda r: -r["ret"])
    up = sum(r["w"] > 0 for r in traders)
    total = sum(r["w"] for r in traders)
    medal = ["🥇", "🥈", "🥉"]
    cap = [f"📅 <b>WEEKLY REPORT · {madrid_now():%d %b}</b>", "",
           f"⚡ <b>{up} of {len(traders)}</b> traders were green · combined <b>{usd(total)}</b>", ""]
    cap += [f"{medal[i]} {r['name']}  <b>{r['ret']:+.1f}%</b> ({usd(r['w'])})" for i, r in enumerate(ranked[:3])]
    if ranked and ranked[-1]["ret"] < 0:
        cap.append(f"💀 {ranked[-1]['name']}  <b>{ranked[-1]['ret']:+.1f}%</b> ({usd(ranked[-1]['w'])})")
    me = next((r for r in rows if r["key"] == MY_KEY), None)
    if me:
        cap += ["", f"👤 You: <b>{me['ret']:+.1f}%</b> ({usd(me['w'])}) this week · balance {usd(me['acct'])}"]
    try:
        send_photo(weekly_chart(rows), "\n".join(cap))
    except Exception as e:  # no matplotlib / photo failed: still send the text
        print(f"chart error: {e!r}", file=sys.stderr)
        send("\n".join(cap))
    # second message: where the money is positioned now
    bets = [(r, p) for r in traders for p in r["pos"]]
    longs = sum(float(p["szi"]) > 0 for _, p in bets)
    shorts = len(bets) - longs
    coins = defaultdict(int)
    for _, p in bets:
        coins[p["coin"]] += 1
    mood = "LONG 🐂" if longs > shorts * 1.5 else "SHORT 🐻" if shorts > longs * 1.5 else "split ⚖️"
    lines = ["📂 <b>OPEN BETS RIGHT NOW</b>", LINE,
             f"🟢 <b>{longs}</b> longs · 🔴 <b>{shorts}</b> shorts → crowd is <b>{mood}</b>"]
    if coins:
        lines.append("🔥 Most held: " + " · ".join(f"{c} ({n})" for c, n in
                                                   sorted(coins.items(), key=lambda kv: -kv[1])[:3]))
    lines += ["", "<b>Biggest bets</b>"]
    for r, p in sorted(bets, key=lambda rp: -abs(float(rp[1]["positionValue"])))[:5]:
        long = float(p["szi"]) > 0
        lines.append(f"{'🟢' if long else '🔴'} {r['name']} · {p['coin']} {'long' if long else 'short'} "
                     f"<b>{usd(p['positionValue'])}</b> ({p['leverage']['value']}x)")
    if not bets:
        lines.append("Nobody has an open position.")
    lines += [LINE, footer()]
    send("\n".join(lines))


def weekly_due(st):
    """Mondays from 09:00 Madrid (08:00 UTC), once per week."""
    now = datetime.now(timezone.utc)
    return now.weekday() == 0 and now.hour >= 8 and st.get("last_weekly", "")[:10] != now.strftime("%Y-%m-%d")


def daily_due(st):
    """09:00 Madrid (08:00 UTC in winter; good enough year-round) and not yet sent today."""
    now = datetime.now(timezone.utc)
    return now.hour >= 8 and st.get("last_daily", "")[:10] != now.strftime("%Y-%m-%d")


def run_once(st, force_daily=False, force_weekly=False):
    global WALLET, KEY
    do_daily = force_daily or daily_due(st)
    for key, ws in list(wallets(st).items()):
        KEY = key
        WALLET = os.environ["MY_WALLET"].strip().lower() if key == MY_KEY else key
        try:
            check_money(st, key)
            check_trades(st)
            if do_daily and ws.get("label") in ("main wallet", "old wallet"):
                daily(st)  # daily detail only for the fund manager; everyone is in the weekly
        except Exception as e:  # one bad wallet shouldn't stop the others
            print(f"error on {ws.get('label', '?')}: {e!r}", file=sys.stderr)
    if force_weekly or weekly_due(st):
        try:
            weekly(st)
        except Exception as e:
            print(f"weekly error: {e!r}", file=sys.stderr)
        st["last_weekly"] = datetime.now(timezone.utc).isoformat()
    if do_daily:
        st["last_daily"] = datetime.now(timezone.utc).isoformat()  # daily commit keeps the scheduled workflow active
    save_state(st)


def preview(st, only_weekly=False):
    """Send one example of every alert type using live data (state is not saved)."""
    import copy
    global WALLET, KEY, info
    st = copy.deepcopy(st)
    wallets(st)
    if only_weekly:
        weekly(st)
        return
    real_info = info
    MAIN = "0xe81309ec9b79309e4d108ae3a92d1aa618cd6d74"
    LOCO = "0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1"
    send("👋 <b>PREVIEW</b>\nHere's one of each alert type, using real wallet data. "
         "The money-movement alerts use example amounts.")
    WALLET = KEY = MAIN
    st["wallets"][MAIN]["last_fill_ms"] = 1794692400000  # Nov 14: SOL long opened, ETH long closed
    check_trades(st)
    WALLET = KEY = LOCO
    st["wallets"][LOCO].update(last_batch=0, last_fill_ms=int(time.time() * 1000) - 6 * 3600_000)
    check_trades(st)
    fake = [{"time": 1, "delta": {"type": "send", "user": MAIN, "destination": "0x1111111111111111111111111111111111111111",
                                  "usdcValue": "2500000"}},
            {"time": 2, "delta": {"type": "withdraw", "usdc": "1500000"}}]
    info = lambda b: fake if b["type"] == "userNonFundingLedgerUpdates" else real_info(b)
    WALLET = KEY = MAIN
    st["wallets"][MAIN]["last_ledger_ms"] = 0
    check_money(st, MAIN)
    st["wallets"].pop("0x1111111111111111111111111111111111111111", None)
    info = real_info
    daily(st)
    weekly(st)


if __name__ == "__main__":
    st = load_state()
    if "--preview" in sys.argv:
        preview(st, only_weekly="weekly" in sys.argv)
        sys.exit(0)
    minutes = int(sys.argv[sys.argv.index("--loop") + 1]) if "--loop" in sys.argv else 0
    end = time.time() + minutes * 60
    run_once(st, force_daily="--daily" in sys.argv, force_weekly="--weekly" in sys.argv)
    while time.time() + 60 < end:  # keep polling every minute (GitHub's cron is often late)
        time.sleep(60)
        run_once(st)
