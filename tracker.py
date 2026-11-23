#!/usr/bin/env python3
"""Hyperliquid wallet tracker: Telegram alert on every new trade + daily summary.

Usage:
  python tracker.py           # check for new trades, alert if any
  python tracker.py --daily   # send the daily summary
  python tracker.py --dry-run # print instead of sending

Env: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, WALLET (optional)
"""
import json, os, sys, time, urllib.request
from collections import defaultdict
from datetime import datetime, timezone

WALLET = os.environ.get("WALLET", "0xe81309ec9b79309e4d108ae3a92d1aa618cd6d74")
API = "https://api.hyperliquid.xyz/info"
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
DRY = "--dry-run" in sys.argv


def info(body):
    req = urllib.request.Request(API, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def send(text):
    if DRY:
        print(text + "\n" + "-" * 40)
        return
    tok, chat = os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    body = json.dumps({"chat_id": chat, "text": text, "parse_mode": "HTML",
                       "disable_web_page_preview": True}).encode()
    urllib.request.urlopen(urllib.request.Request(
        f"https://api.telegram.org/bot{tok}/sendMessage", body,
        {"Content-Type": "application/json"}), timeout=30).read()


def load_state():
    try:
        return json.load(open(STATE))
    except FileNotFoundError:
        return {"last_fill_ms": int(time.time() * 1000)}  # first run: only future trades


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


def snapshot():
    cs = info({"type": "clearinghouseState", "user": WALLET})
    pf = dict(info({"type": "portfolio", "user": WALLET}))
    mids = info({"type": "allMids"})
    def pnl(k):
        h = pf.get(k, {}).get("pnlHistory") or [[0, 0]]
        return float(h[-1][1])
    return {
        "acct": float(cs["marginSummary"]["accountValue"]),
        "ntl": float(cs["marginSummary"]["totalNtlPos"]),
        "positions": [p["position"] for p in cs["assetPositions"]],
        "pnl_day": pnl("day"), "pnl_week": pnl("week"),
        "pnl_month": pnl("month"), "pnl_all": pnl("allTime"),
        "mids": mids,
    }


def risk_read(snap, coin):
    """Rule-based, informational risk read. Not financial advice."""
    flags, pos = [], next((p for p in snap["positions"] if p["coin"] == coin), None)
    acct = snap["acct"] or 1
    eff_lev = snap["ntl"] / acct
    if pos:
        mid = float(snap["mids"].get(coin, pos["entryPx"]))
        liq = pos.get("liquidationPx")
        if liq:
            dist = abs(mid - float(liq)) / mid * 100
            lvl = "🔴 tight" if dist < 15 else "🟡 moderate" if dist < 35 else "🟢 wide"
            flags.append(f"Liquidation {float(liq):,.2f}, {dist:.0f}% away ({lvl})")
        share = float(pos["positionValue"]) / max(snap["ntl"], 1) * 100
        if share > 80:
            flags.append(f"Concentrated: {share:.0f}% of exposure in {coin}")
    flags.append(f"Effective leverage {eff_lev:.1f}x on {usd(acct)} account"
                 + (" (🔴 high)" if eff_lev > 5 else " (🟡)" if eff_lev > 2.5 else " (🟢)"))
    rec = "📉 losing" if snap["pnl_all"] < 0 else "📈 profitable"
    flags.append(f"Wallet record: {rec} all-time ({usd(snap['pnl_all'])}), "
                 f"30d {usd(snap['pnl_month'])}")
    reds = sum("🔴" in f for f in flags) + (snap["pnl_all"] < 0)
    verdict = ("High risk to copy" if reds >= 2 else "Elevated risk" if reds == 1
               else "Risk looks contained")
    return verdict, flags


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


def check_trades(state):
    fills = info({"type": "userFillsByTime", "user": WALLET,
                  "startTime": state["last_fill_ms"] + 1, "aggregateByTime": True})
    if not fills:
        return
    snap = snapshot()
    for (coin, d), v in sorted(group(fills).items(), key=lambda kv: kv[1]["t"]):
        icon, head, meaning = describe(d, coin, v, snap)
        verdict, flags = risk_read(snap, coin)
        szs = f"{v['sz']:,.0f}" if v['sz'] >= 100 else f"{v['sz']:,.4f}"
        lines = [f"{icon} <b>{head}</b>", f"<i>{meaning}</i>",
                 f"Size {szs} {coin} ≈ {usd(v['ntl'])} @ avg {px(v['ntl']/v['sz'])}"]
        if "Close" in d or ">" in d:
            lines.append(f"Realized PnL: <b>{usd(v['pnl'])}</b>")
        lines += ["", f"<b>Risk read: {verdict}</b>"] + [f"• {f}" for f in flags]
        lines += ["", "<i>Informational only, not financial advice.</i>",
                  f"https://hypurrscan.io/address/{WALLET}"]
        send("\n".join(lines))
    state["last_fill_ms"] = max(f["time"] for f in fills)


def daily(state):
    now = int(time.time() * 1000)
    fills = info({"type": "userFillsByTime", "user": WALLET,
                  "startTime": now - 864e5, "aggregateByTime": True})
    snap = snapshot()
    lines = [f"📊 <b>Daily summary</b> {datetime.now(timezone.utc):%d %b %Y}",
             f"Account value {usd(snap['acct'])} · 24h PnL {usd(snap['pnl_day'])}",
             f"7d {usd(snap['pnl_week'])} · 30d {usd(snap['pnl_month'])} · all-time {usd(snap['pnl_all'])}",
             "", "<b>Trades (24h)</b>"]
    g = group(fills)
    lines += [f"• {d} {c}: {usd(v['ntl'])}" + (f", PnL {usd(v['pnl'])}" if "Close" in d else "")
              for (c, d), v in g.items()] or ["• No trades"]
    lines += ["", "<b>Open positions</b>"]
    for p in snap["positions"]:
        side = "Long" if float(p["szi"]) > 0 else "Short"
        liq = p.get("liquidationPx")
        lines.append(f"• {side} {p['coin']} {usd(p['positionValue'])} @ {px(p['entryPx'])}, "
                     f"{p['leverage']['value']}x, uPnL {usd(p['unrealizedPnl'])}"
                     + (f", liq {px(liq)}" if liq else ""))
    if not snap["positions"]:
        lines.append("• None")
    send("\n".join(lines))


if __name__ == "__main__":
    st = load_state()
    if "--daily" in sys.argv:
        daily(st)
        st["last_daily"] = datetime.now(timezone.utc).isoformat()  # daily commit keeps the scheduled workflow active
    else:
        check_trades(st)
    save_state(st)
