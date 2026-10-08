# trade_web_automation

Reads cricket and tennis tips from a Telegram group, uses OpenAI to work out the bet in each
message (text or image), checks it against your safety limits, and places it on the
iob7.com exchange with a real Chrome window.

```
Telegram group -> AI reads the tip -> safety checks -> browser places the bet -> logged in data/trades.db
```

## Setup (once)

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env      # then fill in .env
.\.venv\Scripts\python -m tools.telegram_setup   # log in to Telegram, find the group id for TELEGRAM_CHAT
```

The bot drives your installed Chrome (`BROWSER_CHANNEL=chrome`, or `msedge` for Edge) with its own
profile in `data/browser`, so nothing extra needs downloading.

The bot checks that **1 Click Betting is OFF** before every click on an odds box and switches it off
if needed: with it on, a click alone would place a bet. It also closes ads and pop-ups (Escape or their
close icon) after login and on every page.

## Running, in this order

| Step | Command | What happens |
|---|---|---|
| 1. Test the AI on real tips | `python -m tools.fetch_samples` then `python -m tools.parse_samples` | Saves recent group messages to `samples/` and prints the bet the AI read from each one. |
| 2. Signals only | `python main.py --signals-only` | Live: reads new tips, logs what it *would* bet. No browser. |
| 3. Dry run on the site | `python main.py` with `DRY_RUN=true` | Opens the site, finds the match, checks the live price, fills the slip, screenshots it, cancels. |
| 4. Live | `python main.py` with `DRY_RUN=false` | Clicks Place Bet. |

See what happened with `python -m tools.report`. Screenshots go to `data/screenshots/`, the full log to
`data/bot.log`. When a step fails, the bot also saves the page's HTML next to its screenshot.

The bot finds the match in the home page list, opens its match page and bets from the Match Odds rows
and the Place Bet panel, the same clicks a person makes. Only Match Odds is automated. Where things are
on the site is kept in `tradebot/site_selectors.py`; if the site changes, save the page with
`python -m tools.snapshot_page` and update it.

## How trades work

The AI reads each new message together with the last 15 messages from the group, because tips depend
on earlier ones ("Limit 1.5" and "Book set" refer to the earlier "Broska back 2.9").

| Tipster writes | AI reads it as | Bot does |
|---|---|---|
| "Broska back 2.9", "Broska back 2.9 2 units", "Neumayer lay 1.55 half unit", "Back Namibia 2.88 Rs10", "USA lay 1.5 1k" | entry | Backs or lays at the best price on offer. Stake = the rupee amount if the tip gives one ("Rs10", "₹500", "for 1000", "1k"), else units × `UNIT_STAKE` (no units = 1 unit). Skipped if the live price is more than `ODDS_TOLERANCE` worse than the tip. If the price moves during the site's 6-second delay, it re-checks the price once. |
| "Limit 1.5", "Total 3.5 limit" | exit | Books the trade at that price: an opposite bet sized for equal profit whichever side wins (the site's Cashout formula), left waiting with "Place Bet in Unmatch". A newer exit cancels the waiting one (Open Bets → Unmatch) and replaces it, allowing for any part already matched. |
| "Book set", "bookset", "book profit", "Loss cut", "losscut", "Cashout" | close | Cancels any waiting exit, then uses the site's own **Cashout** button and confirms it, the same as booking by hand. Never uses Turbo Cashout (7% fee). Also works on a trade you placed by hand (the site only shows Cashout where you hold a position). With no team named, it closes the bot's latest open trade. |
| "Again back 2 limit add" | add | Skipped (adds are switched off). |
| "Broska win 🔥", "3 to 8 over wi zone", results | nothing | Logged only. |

Example: ₹1000 back @ 2.9 then "Limit 1.5" lays ₹1933.33 @ 1.5, which leaves +₹933.33 whichever side wins.

A new entry on the same player waits until the previous trade has an exit or has been cashed out.

If the internet drops, the bot keeps retrying a tip for as long as it is fresh (`SIGNAL_MAX_AGE_SECONDS`);
after that it logs `MISSED` with the tip text, so nothing is lost silently. Telegram reconnects by itself.

## Safety limits (.env)

| Setting | Meaning |
|---|---|
| `DRY_RUN` | `true` never clicks Place Bet or confirms a Cashout. |
| `UNIT_STAKE` | Stake of 1 unit (e.g. 1000). Before units existed this was `STAKE`, which is still read if `UNIT_STAKE` is missing. |
| `MAX_UNITS_PER_BET` | Skip tips staking more than this many units' worth (also rupee amounts: 5 × 1000 = ₹5000), to catch a misread tip. 0 = off. |
| `MAX_BETS_PER_DAY` | Stop opening new trades after this many. 0 = off. |
| `MAX_DAILY_EXPOSURE` | Stop opening trades when today's worst-case losses on entries would pass this. 0 = off. |
| `MAX_LIABILITY_PER_BET` | Skip lay bets (including exits) that risk more than this. 0 = off. |
| `MIN_ODDS`, `MAX_ODDS` | Only enter trades inside this odds range. |
| `ODDS_TOLERANCE` | Skip if the live price is this much worse than the tip (0.05 = 5%). |
| `MIN_CONFIDENCE` | Skip tips the AI could not read clearly. |
| `SIGNAL_MAX_AGE_SECONDS` | Skip tips older than this. |
| `DUPLICATE_WINDOW_MINUTES` | Skip the same tip repeated within this window. |

Book sets, loss cuts and exits are never blocked by the daily caps. Limits are counted separately per
mode (signals-only, dry run, live), so testing never uses up the live budget.

## Tests

```powershell
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest tests          # book-set maths, safety rules, trade replays, site selectors
.\.venv\Scripts\python -m tools.check_parser --runs 3   # how the AI reads 23 typical instructions, 3 times each (calls OpenAI)
```

## Files

- `main.py`: runs the whole pipeline
- `tradebot/telegram_listener.py`: reads the group with your Telegram account (Telethon)
- `tradebot/signal_parser.py`: OpenAI turns a message or image into structured bets
- `tradebot/risk.py`: the safety rules
- `tradebot/exchange_bot.py`, `tradebot/site_selectors.py`: browser automation (Playwright)
- `tradebot/store.py`: SQLite log
- `tools/`: setup and testing helpers

`.env`, `data/` (Telegram session, browser login, logs) and `samples/` are private and git-ignored.
