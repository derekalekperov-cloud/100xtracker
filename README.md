# Hyperliquid wallet tracker (free)

Sends a Telegram message on every new trade by the wallet (with a rule-based risk read),
plus a daily summary at 09:00 Madrid. Runs free on GitHub Actions (checks every ~5 min).

## Setup (about 10 minutes)
1. **Telegram bot:** in Telegram, message @BotFather → `/newbot` → copy the token.
   Then send any message to your new bot and open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your `chat.id`.
2. **GitHub:** create a **public** repo (unlimited free Actions minutes; secrets stay hidden), upload `tracker.py` and `.github/workflows/tracker.yml`.
3. Repo → Settings → Secrets and variables → Actions → add `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID`.
4. Actions tab → hyper-tracker → Run workflow (first run records "now"; only later trades alert).

Test locally: `python tracker.py --dry-run` (prints instead of sending).
Change the wallet with the `WALLET` env var.

Also tracks top traders from the Hyperliquid leaderboard (see `TOP_TRADERS` and `PRO_TRADERS` in `tracker.py`;
add an address to `RETIRED` to stop tracking it).
Their alerts are tagged ⭐ with the wallet's label and short address. Remove a line there to stop tracking one.

Note: GitHub may delay scheduled runs by 5–15 min at busy times. Risk read is informational, not financial advice.

## What it tracks
- The fund manager's wallet and his old wallet (every trade, money movements, daily summary at 09:00 Madrid)
- ⭐ Top 1–5 leaderboard traders and 🏅 Pro A–E long-history traders (trade alerts; Pro D "Loco" as an hourly digest)
- 👤 Your own account: add a repo secret `MY_WALLET` with your public address (read-only; never a seed phrase)
- 📅 Weekly report of every wallet, Mondays 09:00 Madrid
