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

On the betting site, make sure **one-click betting is OFF**. In dry-run mode the bot clicks an odds
box to open the bet slip, and with one-click betting on, that click alone would place a bet.

## Running, in this order

| Step | Command | What happens |
|---|---|---|
| 1. Test the AI on real tips | `python -m tools.fetch_samples` then `python -m tools.parse_samples` | Saves recent group messages to `samples/` and prints the bet the AI read from each one. |
| 2. Signals only | `python main.py --signals-only` | Live: reads new tips, logs what it *would* bet. No browser. |
| 3. Dry run on the site | `python main.py` with `DRY_RUN=true` | Opens the site, finds the match, checks the live price, fills the slip, screenshots it, cancels. |
| 4. Live | `python main.py` with `DRY_RUN=false` | Clicks Place Bet. |

See what happened with `python -m tools.report`. Screenshots go to `data/screenshots/`, the full log to `data/bot.log`.

Step 3 needs the site's selectors in `tradebot/site_selectors.py`. To set them, run
`python -m tools.snapshot_page`, save the tennis list, the cricket list and a match page, and share
the files from `data/snapshots/`.

## How trades work

The AI reads each new message together with the last 15 messages from the group, because tips in
this group depend on earlier ones ("Limit 1.5" refers to the earlier "Wi back 6 odds").

| Tipster writes | AI reads it as | Bot does |
|---|---|---|
| "Wi back 6 odds" | entry | Backs West Indies with the fixed `STAKE` and records an open trade. |
| "Limit 1.5" / "Total 3.5 limit" | exit | Lays the same stake at that price on the trade's runner, ticking "Place Bet in Unmatch" so the bet waits until the price is reached. Match Odds only. |
| "Again back 2 limit add" | add | Skipped (adds are switched off). |
| "Wi fav or 1.5", "3 to 8 over wi zone", results | nothing | Logged only. |

One exit per trade: if a second exit arrives (e.g. "Total 3.5 limit" after "Limit 1.5"), it is skipped
and logged; change the unmatched bet on the site yourself. A new entry on the same team is only
allowed after the previous trade has an exit.

## Safety limits (.env)

| Setting | Meaning |
|---|---|
| `DRY_RUN` | `true` never clicks Place Bet. |
| `STAKE` | Fixed stake for every entry (exits use the same stake). |
| `MAX_BETS_PER_DAY` | Stop opening new trades after this many. Exits still go through. |
| `MAX_DAILY_EXPOSURE` | Stop opening trades when today's worst-case losses on entries would pass this. |
| `MAX_LIABILITY_PER_BET` | Skip lay / NO bets (including exits) that risk more than this. |
| `MIN_ODDS`, `MAX_ODDS` | Only enter trades inside this odds range. This group enters at high odds (e.g. 6), so 5.0 would skip those. |
| `ODDS_TOLERANCE` | Skip if the live price is this much worse than the tip (0.05 = 5%). |
| `MIN_CONFIDENCE` | Skip tips the AI could not read clearly. |
| `SIGNAL_MAX_AGE_SECONDS` | Skip tips older than this. |
| `DUPLICATE_WINDOW_MINUTES` | Skip the same tip repeated within this window. |

Limits are counted separately per mode (signals-only, dry run, live), so testing never uses up the live budget.

## Files

- `main.py`: runs the whole pipeline
- `tradebot/telegram_listener.py`: reads the group with your Telegram account (Telethon)
- `tradebot/signal_parser.py`: OpenAI turns a message or image into structured bets
- `tradebot/risk.py`: the safety rules
- `tradebot/exchange_bot.py`, `tradebot/site_selectors.py`: browser automation (Playwright)
- `tradebot/store.py`: SQLite log
- `tools/`: setup and testing helpers

`.env`, `data/` (Telegram session, browser login, logs) and `samples/` are private and git-ignored.
