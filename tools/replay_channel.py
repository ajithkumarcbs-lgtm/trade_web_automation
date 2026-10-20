"""Replays a saved channel through the bot's reading (its rules + the real AI, compared as the live bot does)
and its own trade rules (no betting site), and checks every decision against the right answer.

    python -m tools.replay_channel                       # tests/channel_oct.json, read as the 'tipster' profile
    python -m tools.replay_channel data/history/ss_replay.json --profile ss --media data/history/-1003769891853
    python -m tools.replay_channel --profile ""          # the AI alone, no rules

The profile comes from channels.json (or channels.example.json until you have one). Each message has
"expect": "entry:<team>", "stake:<units>", "close:<team>", "exit:<team>" or "none" (several spellings of a
team separated by "|"), and optionally "odds" / "units" / "side" to check as well. A message may carry
"voice" (the transcript of its voice note, read the way the live bot reads it) and "photo" (a file name in
--media, sent to the AI like the live screenshot). The trade log's clock is set to each message's time, so
"the same tip within 30 min" and the other windows behave as they did live.

Each message gets one verdict:
    PASS   read and acted on as expected, or rightly left alone
    NOOP   no instruction, and the bot's reaction does nothing (a close when it holds no trade)
    ASK    read right, but the live bot would ask you first (/yes)
    RULE   read right, then skipped by a safety rule (odds range, already in a trade, green money off, ...)
    MISS   an instruction the bot did not read, or read as another kind of instruction
    WRONG  acted on a message that is no instruction, or on the wrong team / odds / units
Calls the AI once per message (about $0.0015, more with a photo). Exit code 1 if any MISS or WRONG.
"""
import argparse
import asyncio
import json
import sys
import tempfile
from collections import Counter, deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import tradebot.store as store_module
from main import Pipeline
from tradebot.channels import CHANNELS_FILE, Channel, Channels, Profile
from tradebot.config import ROOT, load_settings
from tradebot.exchange_bot import BetResult
from tradebot.models import BetSignal
from tradebot.names import contains_player
from tradebot.risk import Decision, RiskManager
from tradebot.signal_parser import CONTEXT_MESSAGES, CONTEXT_WINDOW, context_line
from tradebot.store import Store
from tradebot.telegram_listener import IncomingMessage

DEFAULT = ROOT / "tests" / "channel_oct.json"
EXAMPLE = ROOT / "channels.example.json"
MODE = "signals_only"
VERDICTS = ("PASS", "NOOP", "ASK", "RULE", "MISS", "WRONG")
ODDS_SLACK = 0.06  # "5.7" read as 5.75 still counts


@dataclass
class Did:
    """What the bot did with one instruction it read."""
    sig: BetSignal
    outcome: str  # acted | asked | skipped | stake
    reason: str
    names: list[str]  # names the trade was found / recorded / closed under
    effective: bool = True  # a stake message: it would change a trade (an entry of the last minutes to raise)


def load_profile(name: str, settings) -> Profile:
    """The profile of that name from channels.json (else channels.example.json); "" = no rules, AI only."""
    if not name:
        return Profile("replay")
    path = CHANNELS_FILE if CHANNELS_FILE.exists() else EXAMPLE
    for channel in Channels.load(settings, path).all():
        if channel.profile.name == name:
            return channel.profile
    raise SystemExit(f"no channel in {path.name} uses a profile called '{name}'")


def message_text(m: dict) -> str:
    """The text the live bot reads: the message plus the transcript of its voice note."""
    voice = (m.get("voice") or "").strip()
    return f"{m.get('text') or ''}\n(voice note) {voice}".strip() if voice else (m.get("text") or "")


def photo_bytes(m: dict, media: Path | None) -> bytes | None:
    name = m.get("photo")
    if isinstance(name, str) and name and media and (media / name).exists():
        return (media / name).read_bytes()
    return None


async def main(path: Path, profile_name: str, media: Path | None, show_all: bool) -> int:
    settings = load_settings()
    settings.require("groq_api_key" if settings.ai_provider == "groq" else "openai_api_key")
    # Replay checks reading and trade state, not money limits (a channel's own odds range still applies).
    settings = replace(settings, min_odds=1.01, max_odds=1000, max_liability_per_bet=0, max_daily_exposure=0,
                       max_bets_per_day=0, ignore_slippage=False, daily_stop_units=0, weekly_stop_units=0,
                       monthly_stop_units=0)
    profile = load_profile(profile_name, settings)
    channel = Channel("replay", profile, {0})
    channels = Channels(settings, [channel])
    messages = json.loads(path.read_text(encoding="utf-8"))
    by_id = {m["id"]: m for m in messages}
    store = Store(Path(tempfile.mkdtemp()) / "replay.db")
    pipe = Pipeline(settings, MODE, None, RiskManager(settings, store, MODE, channels), store, channels=channels)
    history: deque = deque(maxlen=CONTEXT_MESSAGES)
    opened_for: dict[str, str] = {}  # recorded trade name -> the player its entry tip was about
    unit = channels.settings_for(channel.name).unit_stake
    stake_window = timedelta(seconds=max(profile.stake_wait_s, profile.limit_followup_s))
    verdicts: Counter = Counter()
    by_kind: Counter = Counter()

    for m in messages:
        date = datetime.strptime(m["time"], "%Y-%m-%d %H:%M")
        store_module.clock = lambda when=date: when  # the trade log's "now" = this message's time
        text, image = message_text(m), photo_bytes(m, media)
        context = [line for when, line in history if date - when <= CONTEXT_WINDOW]
        history.append((date, context_line(date, text, image is not None or bool(m.get("photo")))))
        replied = by_id.get(m.get("reply_to"))
        reply_to = f"[{replied['time'][11:]}] {message_text(replied) or '(media)'}" if replied else ""
        # dated now, so the AI is not given up on as an old message after a network drop
        incoming = IncomingMessage(m["id"], datetime.now(timezone.utc), text, image, reply_to,
                                   voice=bool((m.get("voice") or "").strip()))
        signals, how, parsed = await pipe._read(incoming, channel, context)
        if image and parsed and parsed.summary:  # as live: later tips can refer to what the screenshot showed
            history[-1] = (date, context_line(date, f"{text} [screenshot: {parsed.summary}]", False))

        wanted = _players(m["expect"])  # a close / exit for a trade the bot never opened has nothing to act on
        holds = (not m["expect"].startswith(("close:", "exit:"))
                 or any(store.find_position(p, MODE, channel.name) for p in wanted))
        done = []
        for sig in signals:
            sig, problem = pipe._prepare(sig, text, channel)
            if sig.action == "stake" and not problem:
                # live, a stake message completes a held entry or tops up one of the last minutes
                recent = store.latest_position(MODE, channel.name, stake_window) if stake_window else None
                target = sig.amount or (sig.units or 0) * unit
                done.append(Did(sig, "stake", "", [], effective=bool(recent and target > recent.stake + 0.01)))
                continue
            if problem:
                await pipe._record(None, sig, BetResult("skipped", problem), Decision(False, problem))
                done.append(Did(sig, "skipped", problem, [n for n in (sig.selection, sig.aka) if n]))
                continue
            ask = pipe._confirm_reason(sig, channel, how)
            closed_before = store.recently_closed("", MODE)
            result = await pipe.execute(sig)  # a question you'd answer /yes to: the trade goes on as if you had
            names = [n for n in (sig.selection, sig.aka) if n]
            if sig.action == "close" and result.status == "simulated":
                closed_after = store.recently_closed("", MODE)
                trade = closed_after[0] if closed_after != closed_before else None
                names = [opened_for.get(trade, trade)] if trade else [f"{sig.selection} (no trade)"]
            acted = result.status == "simulated"
            done.append(Did(sig, "asked" if ask and acted else ("acted" if acted else "skipped"),
                            ask if ask and acted else result.reason, names))
            player = _players(m["expect"])
            if sig.action == "entry" and acted and player:
                fits = any(contains_player(n, p) for n in names for p in player)
                opened_for[sig.selection] = player[0] if fits else sig.selection

        verdict, why = judge(m, done, unit, holds)
        verdicts[verdict] += 1
        by_kind[(m["expect"].split(":")[0], verdict)] += 1
        if show_all or verdict != "PASS":
            got = "; ".join(_show(d) for d in done) or "nothing"
            print(f"{verdict:<5} #{m['id']:<4} {m['time'][5:]} {text[:60].replace(chr(10), ' ')!r:62} "
                  f"expect {m['expect']:<22} got {got} ({how}){f' | {why}' if why else ''}")

    print(f"\n{len(messages)} messages, profile '{profile.name}', {pipe._parser.name}:")
    print("  " + ", ".join(f"{v} {verdicts[v]}" for v in VERDICTS))
    for kind in ("entry", "stake", "close", "exit", "none"):
        row = {v: by_kind[(kind, v)] for v in VERDICTS if by_kind[(kind, v)]}
        if row:
            print(f"  {kind:<6} {sum(row.values()):4d}: " + ", ".join(f"{v} {n}" for v, n in row.items()))
    return 1 if verdicts["MISS"] or verdicts["WRONG"] else 0


def _players(expect: str) -> list[str]:
    action, _, wanted = expect.partition(":")
    return [p.strip() for p in wanted.split("|") if p.strip()] if action in ("entry", "close", "exit") else []


def _show(d: Did) -> str:
    sig = d.sig
    if d.outcome == "stake":
        return f"stake {sig.units:g}u" if sig.units else "stake"
    numbers = "".join([f" @{sig.odds or sig.when_odds:g}" if (sig.odds or sig.when_odds) else "",
                       f" {sig.units:g}u" if sig.units else "", " lay" if sig.side == "lay" else ""])
    status = {"acted": "", "asked": " [ASK]", "skipped": f" [skipped: {d.reason[:70]}]"}[d.outcome]
    return f"{sig.action}:{'/'.join(d.names) or sig.selection or '-'}{numbers}{status}"


def _units_of(sig: BetSignal, unit: float) -> float | None:
    """The stake in units, also when given in rupees ("Try 200 or 300" = 0.2 at Rs 1000 a unit)."""
    return sig.units if sig.units is not None else (round(sig.amount / unit, 2) if sig.amount and unit else None)


def judge(m: dict, done: list[Did], unit: float = 0, holds: bool = True) -> tuple[str, str]:
    """The verdict on one message and why. unit: rupees per unit, to compare stakes given in money.
    holds: for a close / exit, whether the bot holds a trade on that team (its entry may have been skipped)."""
    action, _, wanted = m["expect"].partition(":")
    active = [d for d in done if d.outcome in ("acted", "asked") or (d.outcome == "stake" and d.effective)]
    if action in ("close", "exit") and not holds and all(
            d.sig.action not in ("entry", "add") and all(n.endswith("(no trade)") for n in d.names)
            for d in active):
        return "RULE", f"the bot holds no trade on {wanted} (its entry was skipped): nothing to close"
    if action == "none":
        if not active:
            return "PASS", ""
        if all(d.sig.action == "close" and all(n.endswith("(no trade)") for n in d.names) for d in active):
            return "NOOP", "a close of a trade the bot doesn't hold: nothing happens"
        return "WRONG", "acted on a message that is no instruction"
    if action == "stake":
        stakes = [d for d in done if d.outcome == "stake"]
        if any(abs((_units_of(d.sig, unit) or 0) - float(wanted)) <= 0.051 for d in stakes):
            extra = [d for d in active if d.outcome != "stake"]
            return ("WRONG", "also acted on more") if extra else ("PASS", "")
        return ("WRONG", "another stake") if stakes else ("MISS", "stake not read")
    players = _players(m["expect"])
    kinds = {"exit", "close"} if action in ("exit", "close") else {action}  # exit ~ close: both book the trade
    same = [d for d in done if d.sig.action in kinds
            and (any(contains_player(n.replace(" (no trade)", ""), p) for n in d.names for p in players)
                 or (action != "entry" and not d.names))]  # "Bookset" naming no team: the channel's latest trade
    if not same:
        other = [d for d in done if d.sig.action in kinds]
        return ("WRONG", "another team") if other else ("MISS", "not read")
    d = same[0]
    if d.outcome == "asked":  # you decide, with the bot's reading in front of you
        return "ASK", d.reason
    if action == "entry":
        odds = d.sig.when_odds or d.sig.odds
        if m.get("odds") and (odds is None or abs(odds - m["odds"]) > ODDS_SLACK):
            return "WRONG", f"odds {odds} instead of {m['odds']}"
        if m.get("units") and abs((_units_of(d.sig, unit) or 1.0) - m["units"]) > 0.05:
            return "WRONG", f"units {_units_of(d.sig, unit)} instead of {m['units']}"
        if m.get("side", "back") != d.sig.side:
            return "WRONG", f"{d.sig.side} instead of {m.get('side', 'back')}"
    extra = [x for x in active if x is not d and x.outcome != "stake"]
    if extra:
        return "WRONG", "also acted on more than the instruction"
    note = "exit read as close" if d.sig.action != action else ""
    if d.outcome == "acted":
        return "PASS", note
    if d.outcome == "asked":
        return "ASK", d.reason
    return "RULE", d.reason


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", type=Path, default=DEFAULT)
    ap.add_argument("--profile", default="tipster", help="profile from channels.json to read the tips with")
    ap.add_argument("--media", type=Path, help="folder with the photos named in the messages' \"photo\"")
    ap.add_argument("--all", action="store_true", help="print every message, not only the ones that didn't PASS")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main(args.path, args.profile, args.media, args.all)))
