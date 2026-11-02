# trade_web_automation

Reads cricket and tennis tips from Telegram channels, works out the instruction in each message (text,
screenshot or voice note) with exact rules and an AI that check each other, checks it against your safety
limits, and trades it on the iob7.com exchange with a real Chrome window.

```
Telegram channels -> rules + AI read the tip -> safety checks -> browser trades on the site
                  -> data/trades.db + alerts to your Telegram Saved Messages
```

## Setup (once)

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env                     # then fill in .env
copy channels.example.json channels.json   # then check the chat ids (see Channels below)
.\.venv\Scripts\python -m tools.telegram_setup   # log in to Telegram, list your chats and their ids
```

The bot drives your installed Chrome (`BROWSER_CHANNEL=chrome`, or `msedge` for Edge) with its own
profile in `data/browser`, so nothing extra needs downloading.

The bot checks that **1 Click Betting is OFF** before every click on an odds box and switches it off
if needed: with it on, a click alone would place a bet. It also closes ads and pop-ups (Escape or their
close icon) after login and on every page.

## Running, in this order

| Step | Command | What happens |
|---|---|---|
| 1. Test the reading on real tips | `python -m tools.replay_channel` and `python -m tools.check_parser` | Replays a saved channel and typical instructions through the rules + AI, checks every decision. |
| 2. Signals only | `python main.py --signals-only` | Live: reads new tips, logs what it *would* bet. No browser. |
| 3. Dry run on the site (paper trading) | `python main.py` with `DRY_RUN=true` | Opens the site, finds the match, checks the live price, fills the slip, screenshots it, cancels. Paper P/L per channel: `/report week`. |
| 4. Live | `python main.py` with `DRY_RUN=false` | Clicks Place Bet. Start with a small `UNIT_STAKE`. |

See what happened with `python -m tools.report` (decisions) or `python -m tools.report --pl week` (P/L per
channel). Check a day by hand with `python -m tools.daily_review` (each channel's decisions with reasons, the
day's P/L, and the messages that look like tips but led to nothing) or `--days 10` for one line per day. Screenshots go to `data/screenshots/`, the full log to `data/bot.log`. When a step fails, the bot
also saves the page's HTML next to its screenshot.

The bot finds the match in the home page list, opens its match page and bets from the Match Odds rows
and the Place Bet panel, the same clicks a person makes. Only Match Odds is automated. Where things are
on the site is kept in `tradebot/site_selectors.py`; if the site changes, save the page with
`python -m tools.snapshot_page` and update it.

## Channels (channels.json)

`channels.json` lists the chats to follow and how each is traded (`channels.example.json` has the Tipster
VIP, the Tipster open group, SS sports and a TEST group). Without it, only `TELEGRAM_CHAT` is followed.

- Chats are followed by their **numeric id**, which stays the same when a channel is renamed or gets a new
  photo. `python -m tools.telegram_setup` lists the ids of your chats.
- `title_regex`: a chat you join later whose title matches is followed too, e.g. the tipster's next
  tournament channel ("Nov (Paris)"). You get a Telegram alert when that happens; `/unfollow <id>` if it is
  not wanted. New chats are looked for every 10 minutes and remembered in `data/trades.db`.
- A watch-only channel (`"trade": false, "watch_links": true`) is never traded; you are alerted when it posts
  a Telegram group link, so you can join the new channel.
- Each **profile** sets how its channels are read and traded:

| Key | Meaning |
|---|---|
| `rules` | `tipster`, `ss` or `tennistv`: the tipster's usual formats are also read by exact rules and compared with the AI (`tennistv`: "X Win Karo … 1.70 limit use kro 4 limit rhega" = back X, 1.7 units, odds 1 + 4/1.7). |
| `act_on_forwards` | `false`: forwarded messages (members' "book set maar diya" testimonials) are only context. |
| `strict_only` | Only the strict command format below, no AI (the TEST group). |
| `voice` | Voice and video notes are transcribed (`TRANSCRIBE_MODEL`) and kept in the chat context the AI reads. |
| `act_on_voice` | A voice note can also be an instruction by itself. Off by default: on SS sports, transcribed Tamil ("bookset-ku ready-aa irunga") was read as a bookset and closed trades early, and no entry was ever given only by voice. |
| `entry_words` | Regex: an entry only the AI found, in a text without a screenshot, needs one of these words ("back", "entry", "try"...) and must name its team (misspelt or as a code is fine: "beaz", "kg"), so price updates ("Sl 1.2 coming") and teams guessed from the context ("Now 8.4 odds back" read as India) don't trade. |
| `hint` | What is special about this tipster, given to the AI ("Losscut = close now"). |
| `aliases` | Tipster's short names → the site's names (`"kg": "Knights"`). |
| `settings` | `.env` settings that differ for this channel's trades: `unit_stake`, `odds_tolerance`, `auto_bookset_units`, `max_units_per_bet`, `min_odds`, `max_odds`, ... |
| `stake_wait_s` | An entry without a stake waits this long for its "Limit 2" / "Green 20%" message. |
| `limit_followup_s` | "Limit 2" this long after a 1-unit entry tops the trade up to 2 units. |
| `small_units` | Stake of "try small amount" tips (0 = skip them). |
| `green_money` | "Green 20% back 7 odds": `skip` (default) or `stake` 20% of the profit just booked. |
| `confirm` | Ask you first (`/yes`): `always`, `disagree` (rules and AI read it differently), `below_confidence`, `above_units`. Booksets and loss cuts are never held up. |

## How a message is read

1. **Strict commands** (TEST group, your Saved Messages) are executed exactly, no AI:
   `BACK Shelton 3.25 1u`, `LAY Sinner 1.55 0.5u`, `BACK AT Berrettini 3 1u` (waits for 3), `EXIT Shelton 1.6`,
   `BOOKSET Shelton`, `LOSSCUT Shelton`, `CANCEL Berrettini`.
2. **Channels with rules**: short messages in the tipster's usual formats ("Back ruplev 2.4 1 unit",
   "5 odds back kg", "Limit 2", "Loss cut 2.5 la") are read by exact rules, and every message is read by the
   AI too (with the last 15 messages as context, the screenshot and the voice note). Then:
   - both read the same instruction → **agree**: the AI's spelling of the name (it knows "ruplev" is Rublev),
     the rules' numbers. The rules' spelling is kept as a second name for the site ("gea" when the AI wrote
     "Gaea"), and nothing is placed if the two names point to different matches.
   - different action, odds, units, or names that can't be the same player ("gea" vs "Garin") →
     **disagree**: the rules' reading, and you are asked first when the profile says `"disagree": true`.
   - only one of them found an instruction → **rule-only** / **ai-only** (ai-only below `below_confidence` is asked).
3. A trade is recorded under the **site's names** of the player and match, so later booksets, price checks
   and the finished-match check find it however the tipster spells the player.
4. Checks on what was read: a close in a message about a bookset still to come ("Ready for bookset", "wait for
   bookset") is dropped; a close naming a team the message itself doesn't mention (the AI took it from the
   context) acts on the channel's latest entry, and on nothing when that entry was never placed (so a "Bookset"
   for a tip the bot skipped never books an older trade). Team names that look like typos of each other
   (India / West Indies, Austria / Australia) are never matched loosely.
5. Check a channel's reading end to end with `python -m tools.replay_channel <export.json> --profile <name>`:
   every message of a saved channel with the right answer, judged PASS / ASK / RULE / MISS / WRONG.

## Several channels at the same time

- Each channel has its own reader. Messages of different channels are read side by side, so a tip in one
  channel is read while the other channel's trade is on the site.
- A channel's own messages are always handled in the order they came. A voice note that takes seconds to
  transcribe stays before the "Limit 2" that follows it.
- The site does one thing at a time (one browser). Waiting actions go in arrival order, except bookings
  (bookset, loss cut, exit, cancel), which go first. An action already on the site is never interrupted,
  so a booking can wait for at most one entry (about 20–30 s).
- **One trade per match.** A tip on another player of a match the bot already trades, from any channel, is
  skipped with "SAME MATCH". The site's Cashout books the whole match, so the second trade would be closed by
  the first one's bookset. For the same reason, don't bet by hand on a match the bot trades.
- Context, held entries, auto-bookset and "Bookset" without a name stay per channel. Limits, stops and the
  balance are shared: one account, one day's result.

## How trades work

| Tipster writes | Read as | Bot does |
|---|---|---|
| "Broska back 2.9", "Back ruplev 2.4 1 unit", "5 odds back kg", "Back Namibia 2.88 Rs10", "USA lay 1.5 1k" | entry | Backs or lays at the best price on offer. Stake = the rupee amount if the tip gives one, else units × `UNIT_STAKE` (no units = 1 unit). Skipped if the live price is more than `ODDS_TOLERANCE` worse than the tip. If the price moves during the site's 6-second delay, it re-checks the price once. |
| a back at or above `MAX_BACK_ODDS` (the site takes no back above 10), or one the site shows no back price for | entry | Placed at once as a **lay of the opponent** at its lay price b: the same as a back at b/(b−1) (lay 1.07 = back 15.3), lay stake = back stake × (that − 1), so the win and the loss are the same as the back's. Checked against the tip's odds like any entry. If the site's Max Bet cuts the lay, the trade is recorded at the smaller back stake it stands for. Only in 2-runner markets (not with a Draw). |
| "Berriting 3 odds pona edunga", "if 3 odds reached means taken back oman" | waiting entry | Waits (checked every 10 s) until the price reaches 3 (2% leeway), for up to `CONDITIONAL_ENTRY_MINUTES`; `CANCEL <name>` drops it. |
| "Limit 2", "Only one limit", "Half limit" after an entry | stake | Completes the entry held for it (`stake_wait_s`), or tops up a trade placed within `limit_followup_s` (one trade, stake-weighted average odds). |
| "If odds up means 4 or 3.9 exit", "3.2 odds now exit with profit" | exit | Books the trade at that price: an opposite bet sized for equal profit whichever side wins, left waiting with "Place Bet in Unmatch". A newer exit cancels the waiting one and replaces it, allowing for any part already matched. |
| "Book set", "bookset", "book pannuga", "Loss cut", "losscut", "Cashout" | close | Cancels any waiting exit, then uses the site's own **Cashout** button and confirms it (never Turbo Cashout, 7% fee). Markets without Cashout are booked by hand with one equal-profit bet. With no team named, it closes the channel's latest open trade. A refused booking is retried every 10 s for up to `BOOKSET_RETRY_MINUTES`. |
| "Green money 20% back 3.7 odds" | entry | Skipped unless the profile has `"green_money": "stake"`. |
| "Again back 2 limit add" | add | Skipped (adds are switched off). |
| "Broska win 🔥", "3 to 8 over wi zone", results, chat | nothing | Logged only. |

Example: ₹1000 back @ 2.9 then "exit at 1.5" lays ₹1933.33 @ 1.5, which leaves +₹933.33 whichever side wins.

"limit" in a tip is always the stake in units ("6 odds back war limit 1", "half limit"), never a price.

Before every Placebet the bot checks the slip the way a trader would: the stake is entered with the
site's quick-stake button when one matches (1K for ₹1000) or typed otherwise, and must then show exactly
that stake and the expected odds; the stake must be within the slip's Min Bet / Max Bet (a market showing
Max Bet 0 is skipped; a stake above Max Bet is cut to it); Placebet must be enabled; the balance must cover
the worst-case loss. After placing, a bet counts as confirmed when the site's success message appears
and/or the header BAL / EXP changes, and it is looked up in Open Bets.

A new entry on the same player waits until the previous trade has an exit or has been cashed out.

If the internet drops, the bot keeps retrying a tip for as long as it is fresh (`SIGNAL_MAX_AGE_SECONDS`);
after that it alerts `TIP MISSED` with the tip text, so nothing is lost silently. Telegram reconnects by itself.

## Safety limits (.env)

| Setting | Meaning |
|---|---|
| `DRY_RUN` | `true` never clicks Place Bet or confirms a Cashout. |
| `UNIT_STAKE` | Stake of 1 unit (e.g. 100 while testing, 1000 later). A channel can set its own `unit_stake`. |
| `MAX_UNITS_PER_BET` | Skip tips staking more than this many units' worth (also rupee amounts), to catch a misread tip. 0 = off. |
| `MAX_BETS_PER_DAY` | Stop opening new trades after this many. 0 = off. |
| `MAX_DAILY_EXPOSURE` | Stop opening trades when today's worst-case losses on entries would pass this. 0 = off. |
| `MAX_LIABILITY_PER_BET` | Skip bets (including exits) that risk more than this. 0 = off. |
| `MIN_ODDS`, `MAX_ODDS` | Only enter trades inside this odds range. |
| `MAX_BACK_ODDS` | Backs above this are placed as a lay of the opponent (the site's limit: 10). 0 = off. |
| `ODDS_TOLERANCE` | Skip if the live price is this much worse than the tip (0.05 = 5%; the tipster profile uses 15%, his quoted odds are usually 8-12% above the live price). |
| `MIN_CONFIDENCE` | Skip tips the AI could not read clearly. |
| `SIGNAL_MAX_AGE_SECONDS` | Skip tips older than this. |
| `DUPLICATE_WINDOW_MINUTES` | Skip the same tip repeated within this window. |
| `DAILY_STOP_UNITS`, `WEEKLY_STOP_UNITS`, `MONTHLY_STOP_UNITS` | No new entries once the day's / week's / month's result reaches −N units. A trade that ran to the end unbooked counts as lost until you `/settle` it. 0 = off. |
| `CONDITIONAL_ENTRY_MINUTES` | How long a waiting entry waits for its price. |
| `CONFIRM_SECONDS` | A CONFIRM question not answered in this time is dropped (the tip is skipped). |
| `CONFIRM_IN_DRY_RUN` | `false` (default): paper trading doesn't ask, it goes ahead and notes "[live would ask: …]", so it can run unattended. |

Book sets, loss cuts and exits are never blocked by the daily caps or the stops. Limits are counted
separately per mode (signals-only, dry run, live), so testing never uses up the live budget.

## Telegram commands (your Saved Messages)

| Command | |
|---|---|
| `/status` | Bot state, stops, open trades, today's result. |
| `/report [day\|week\|month]` | P/L per channel: booked, settled, finished-unsettled (counted as lost), open. |
| `/settle <trade id> win\|lose\|<amount>` | Result of a trade that ran to the end of its match. |
| `/pause`, `/resume` | Stop / allow new entries (bookings always work). |
| `/yes <n>`, `/no <n>` | Answer a CONFIRM question. |
| `/channels`, `/follow <chat id> <channel>`, `/unfollow <chat id>` | Chats followed. |
| `/back ...`, `/lay ...`, `/exit ...`, `/bookset ...`, `/losscut ...`, `/cancel ...` | Trade by hand (strict format). |
| `/help` | This list. |

Besides an alert for every decision, the bot sends a **📊 TRADE SUMMARY** when a trade is booked, a
**🏁 MATCH FINISHED** message when a trade's match disappears from the site without having been booked
(two misses in a row, checked every `KEEPALIVE_MINUTES`), and a "bot running" heartbeat every `HEARTBEAT_HOURS`.

## Running 24/7

**On your own PC** (it must stay on, awake and online; set Windows sleep to "Never"):

```powershell
# HEADLESS=true in .env first, so no Chrome window appears
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1          # starts now and at every log-on
Stop-ScheduledTask -TaskName TradeBot                                    # stop; Start-ScheduledTask to start
powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Remove  # uninstall
```

`scripts\run_forever.ps1` restarts the bot 30 s after any stop and logs it in `data\restarts.log`.

**On a Windows VPS** (a server that is always on; pick one in India, e.g. Mumbai, so the site and its prices
load fast): install Python and Chrome, copy the project folder without `.venv`, create the venv as in Setup,
copy `.env` and `channels.json`, then either copy `data\telegram.session` and `data\browser` from your PC or
log in again there (`python -m tools.telegram_setup`, `python -m tools.snapshot_page`). Run
`scripts\install_task.ps1` and set the VPS user to log on automatically, so the task starts after a reboot.
Keep only one copy of the bot running: two would place every bet twice.

**On Oracle Cloud Always Free** (Ampere A1, 4 CPU / 24 GB, Ubuntu, region Mumbai or Hyderabad; upgrade the
account to Pay As You Go, still free within the limits, so an idle Always Free instance isn't reclaimed):

```bash
# on your PC (Git Bash): copy the code, without .venv / data
scp -r main.py tradebot tools scripts tests requirements.txt channels.example.json ubuntu@<server-ip>:~/trade_web_automation/
# on the server
cd ~/trade_web_automation && bash scripts/oracle_setup.sh
# back on the PC: STOP the PC bot first, then copy the private files
scp .env channels.json ubuntu@<server-ip>:~/trade_web_automation/
scp data/telegram.session ubuntu@<server-ip>:~/trade_web_automation/data/
# on the server: BROWSER_CHANNEL=chromium and HEADLESS=true in .env, then
sudo systemctl start tradebot && journalctl -u tradebot -f
```

The site logs in again on the server (a Windows Chrome profile doesn't carry over); a new IP may get an OTP.

## Tests

```powershell
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest tests          # book-set maths, safety rules, rules + AI checks, channels, replays, site selectors
.\.venv\Scripts\python -m tools.check_parser --runs 3   # how the AI reads typical instructions, 3 times each (calls OpenAI)
.\.venv\Scripts\python -m tools.replay_channel          # the real 'Oct' channel through rules + AI (--profile ss for SS)
.\.venv\Scripts\python -m tools.try_bet back Svrcina 10 --dry-run   # one action on the site, step by step
```

## Files

- `main.py`: runs the whole pipeline; Saved Messages commands
- `channels.json` (yours, git-ignored) / `channels.example.json`: chats to follow and how each is traded
- `tradebot/telegram_listener.py`: reads the channels with your Telegram account (Telethon), finds new ones
- `tradebot/rules.py`: exact rules for the strict format and the tipsters' usual formats; compares them with the AI
- `tradebot/signal_parser.py`: the AI turns a message, screenshot or transcript into structured instructions
- `tradebot/voice.py`: voice notes to text
- `tradebot/channels.py`: channels.json
- `tradebot/risk.py`: the safety rules
- `tradebot/exchange_bot.py`, `tradebot/site_selectors.py`: browser automation (Playwright)
- `tradebot/store.py`: SQLite log, trades, P/L
- `scripts/`: running 24/7 on Windows
- `tools/`: setup, testing and analysis helpers

`.env`, `channels.json`, `data/` (Telegram session, browser login, logs) and `samples/` are private and git-ignored.
