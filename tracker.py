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
WALLET = None          # wallet currently being processed (set in main loop)
MIN_MOVE = 50_000      # USD; smaller transfers are ignored
SYSTEM_PREFIX = "0x2000000000000000000000000000000000"  # HyperEVM bridge addresses
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
    main = w["0xe81309ec9b79309e4d108ae3a92d1aa618cd6d74"]
    if "last_fill_ms" in state:  # from the first version
        main["last_fill_ms"] = state.pop("last_fill_ms")
    return w


def short(a):
    return a[:6] + "…" + a[-4:]


def check_money(state, w):
    """Alert on money moving in/out, liquidations, and the account being emptied."""
    me = WALLET
    ws = state["wallets"][me]
    ups = info({"type": "userNonFundingLedgerUpdates", "user": me,
                "startTime": ws["last_ledger_ms"] + 1})
    tag = f"[{ws['label']} {short(me)}]"
    for u in sorted(ups, key=lambda u: u["time"]):
        d = u["delta"]; t = d["type"]
        amt = float(d.get("usdcValue") or d.get("usdc") or d.get("amount") or 0)
        if t == "liquidation":
            send(f"🚨 <b>LIQUIDATED</b> {tag}\nPositions were force-closed by the exchange.")
        elif t == "withdraw" and amt >= MIN_MOVE:
            send(f"💸 <b>WITHDRAWAL {usd(amt)}</b> {tag}\nMoney left Hyperliquid to an outside wallet/exchange.\n"
                 "If he starts a new wallet, it will usually show up with a deposit of a similar amount.")
        elif t in ("send", "spotTransfer") and amt >= MIN_MOVE:
            dest, src = d.get("destination", "").lower(), d.get("user", "").lower()
            if src == me and dest != me:
                if dest.startswith(SYSTEM_PREFIX):
                    send(f"💸 <b>MOVED {usd(amt)} OFF HYPERLIQUID</b> {tag}\nSent to the HyperEVM bridge "
                         "(this is how he moved funds out of his old wallet on Sep 17).")
                elif dest in state["wallets"]:
                    send(f"↔️ <b>{usd(amt)} moved</b> {tag} → {state['wallets'][dest]['label']} {short(dest)}")
                elif not ws.get("follow", True):
                    send(f"↗️ <b>{usd(amt)} sent out</b> {tag} → {short(dest)}\n"
                         f"https://hypurrscan.io/address/{dest}")
                else:
                    now = int(time.time() * 1000)
                    state["wallets"][dest] = {"label": f"followed from {short(me)}", "last_fill_ms": now,
                                              "last_ledger_ms": now, "peak": 0}
                    send(f"🧭 <b>{usd(amt)} SENT TO A NEW WALLET</b> {tag}\n→ {dest}\n"
                         f"✅ I'm now tracking this wallet automatically.\nhttps://hypurrscan.io/address/{dest}")
            elif dest == me:
                who = state["wallets"].get(src, {}).get("label", "unknown")
                if src not in state["wallets"]:
                    send(f"💰 <b>{usd(amt)} ADDED</b> {tag} from {short(src)} ({who})")
        elif t == "deposit" and amt >= MIN_MOVE:
            send(f"💰 <b>DEPOSIT {usd(amt)}</b> {tag}\nNew money added from outside Hyperliquid.")
    if ups:
        ws["last_ledger_ms"] = max(u["time"] for u in ups)
    # emptied / big drawdown check
    acct = equity(dict(info({"type": "portfolio", "user": me})))
    ws["peak"] = max(ws.get("peak", 0), acct)
    if ws["peak"] >= 1e6 and acct < 0.1 * ws["peak"] and not ws.get("emptied_alerted"):
        send(f"⚠️ <b>WALLET EMPTIED</b> {tag}\nNow {usd(acct)}, down from {usd(ws['peak'])}. "
             "Check the alerts above to see where the money went.")
        ws["emptied_alerted"] = True
    elif acct >= 0.5 * ws["peak"]:
        ws["emptied_alerted"] = False


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
    def pnl(k):
        h = pf.get(k, {}).get("pnlHistory") or [[0, 0]]
        return float(h[-1][1])
    return {
        "acct": equity(pf) or float(cs["marginSummary"]["accountValue"]),
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
                  "startTime": state["wallets"][WALLET]["last_fill_ms"] + 1, "aggregateByTime": True})
    if not fills:
        return
    snap = snapshot()
    for (coin, d), v in sorted(group(fills).items(), key=lambda kv: kv[1]["t"]):
        icon, head, meaning = describe(d, coin, v, snap)
        verdict, flags = risk_read(snap, coin)
        szs = f"{v['sz']:,.0f}" if v['sz'] >= 100 else f"{v['sz']:,.4f}"
        lbl = state["wallets"][WALLET]["label"]
        lines = [f"{icon} <b>{head}</b>  [{lbl} {short(WALLET)}]", f"<i>{meaning}</i>",
                 f"Size {szs} {coin} ≈ {usd(v['ntl'])} @ avg {px(v['ntl']/v['sz'])}"]
        if "Close" in d or ">" in d:
            lines.append(f"Realized PnL: <b>{usd(v['pnl'])}</b>")
        lines += ["", f"<b>Risk read: {verdict}</b>"] + [f"• {f}" for f in flags]
        lines += ["", "<i>Informational only, not financial advice.</i>",
                  f"https://hypurrscan.io/address/{WALLET}"]
        send("\n".join(lines))
    state["wallets"][WALLET]["last_fill_ms"] = max(f["time"] for f in fills)


def daily(state):
    now = int(time.time() * 1000)
    fills = info({"type": "userFillsByTime", "user": WALLET,
                  "startTime": now - 86_400_000, "aggregateByTime": True})
    snap = snapshot()
    if snap["acct"] < 1 and not fills:
        return
    lines = [f"📊 <b>Daily summary</b> {datetime.now(timezone.utc):%d %b %Y} [{state['wallets'][WALLET]['label']} {short(WALLET)}]",
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


DATAROMA_RSS = "https://www.dataroma.com/m/rss.xml"  # public feed of superinvestor 13F updates


def check_hedge_funds(st):
    """Alert on new superinvestor portfolio updates from Dataroma's public RSS feed.
    Checked every 30 min. Source data is quarterly 13F filings (up to 45 days old)."""
    import re, xml.etree.ElementTree as ET
    hf = st.setdefault("hedge_funds", {"seen": [], "last_check": 0})
    if time.time() - hf["last_check"] < 1800:
        return
    hf["last_check"] = time.time()
    req = urllib.request.Request(DATAROMA_RSS, headers={"User-Agent": "personal-portfolio-alerts/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        root = ET.fromstring(r.read())
    first_run = not hf["seen"]
    for item in reversed(root.findall("./channel/item")):
        guid = (item.findtext("guid") or item.findtext("link") or "").strip()
        if not guid or guid in hf["seen"]:
            continue
        hf["seen"].append(guid)
        if first_run:
            continue  # don't flood with the existing backlog
        name = (item.findtext("title") or "").strip()
        desc = item.findtext("description") or ""
        moves = []
        for label, tickers in re.findall(r"<b>([^<:]+):</b>\s*([^<]*)", desc):
            if tickers.strip():
                moves.append(f"<b>{label}:</b> {tickers.strip()}")
        m = re.search(r"#(\d{4})(\d)$", guid)
        qtr = f" (Q{m.group(2)} {m.group(1)} filing)" if m else ""
        send("\n".join([f"🏦 <b>{name}</b> updated its portfolio{qtr}", *moves, "",
                         "<i>From quarterly 13F filings: these trades happened up to ~3–4 months ago.</i>",
                         f"Source: Dataroma · {(item.findtext('link') or '').strip()}"]))
    hf["seen"] = hf["seen"][-500:]


def daily_due(st):
    """09:00 Madrid (08:00 UTC in winter; good enough year-round) and not yet sent today."""
    now = datetime.now(timezone.utc)
    return now.hour >= 8 and st.get("last_daily", "")[:10] != now.strftime("%Y-%m-%d")


def run_once(st, force_daily=False):
    global WALLET
    do_daily = force_daily or daily_due(st)
    for addr in list(wallets(st)):
        WALLET = addr
        try:
            check_money(st, addr)
            check_trades(st)
            if do_daily:
                daily(st)
        except Exception as e:  # one bad wallet shouldn't stop the others
            print(f"error on {addr}: {e!r}", file=sys.stderr)
    try:
        check_hedge_funds(st)
    except Exception as e:
        print(f"error on dataroma feed: {e!r}", file=sys.stderr)
    if do_daily:
        st["last_daily"] = datetime.now(timezone.utc).isoformat()  # daily commit keeps the scheduled workflow active
    save_state(st)


if __name__ == "__main__":
    st = load_state()
    minutes = int(sys.argv[sys.argv.index("--loop") + 1]) if "--loop" in sys.argv else 0
    end = time.time() + minutes * 60
    run_once(st, force_daily="--daily" in sys.argv)
    while time.time() + 60 < end:  # keep polling every minute (GitHub's cron is often late)
        time.sleep(60)
        run_once(st)
