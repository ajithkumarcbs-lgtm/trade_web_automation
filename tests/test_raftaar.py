"""Raftaar's two formats, read by rules beside the AI, and what its misses of 29 Oct and 2 Nov showed. The posts are its own."""
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import main
from conftest import make_settings, signal
from test_channels_rules import make, msg
from tradebot.channels import Channels, Profile
from tradebot.leagues import Draw, side_of_first_name
from tradebot.rules import parse_known

RAFTAAR = Profile("raftaar", rules="raftaar", ai_filter=True, bare_photos=False, entry_words="(khao|winner|entry)",
                  act_on_forwards=False)
BOOKED = "60    P KHILAAF SE\n\n40 P HUM😍😍\n\nBOOK. SET\nCASH. OUT\n\nUD GAYA BOOKIE…."
DRAWS = [Draw("WTA Hong Kong", ["Aliaksandra Sasnovich", "Katie Volynets"]), Draw("WTA Other", ["Katie Boulter", "Iga Swiatek"]),
         Draw("ATP Paris", ["Novak Djokovic", "Lorenzo Musetti"]), Draw("ATP Paris", ["Alex de Minaur", "Alex Michelsen"])]
LISTED = ["Melbourne Stars W v Perth Scorchers W", "Volynets v A Sasnovich", "N Djokovic v Musetti", "A De Minaur v Bublik",
          "Michelsen v Fritz"]


def tip(name):
    return (f"WINNER - {name}\n\n\n➥ 100% SURE MATCH...\n➥ PLAYER MERA...\n➥ BOOKIE UDANE KA KAAM MERA...\n\nRAFTAAR 🦅\n"
            "BEST TENNIS ID \n\nhttps://diamond.lottusbook365.com/register?campaignId=1")


def post(pipe, text):
    asyncio.run(pipe._handle(msg(text)))


def test_its_tip_and_its_booking_are_read():
    entry = parse_known(tip("ALEX M"), "raftaar")[0]
    assert (entry.action, entry.selection, entry.side, entry.sport, entry.units) == ("entry", "Alex M", "back", "tennis", None)
    assert parse_known(tip("HUMBERY HURCURZ"), "raftaar")[0].selection == "Humbery Hurcurz"
    close = parse_known(BOOKED, "raftaar")[0]
    assert (close.action, close.selection) == ("close", "")


def test_a_first_name_and_initial_opens_only_a_player_they_fit():
    """10 Nov: 'WINNER - ADRIAN M' (Mannarino) opened 'Adrian Andreev v Pietri'. Its market was suspended, so no bet went on
    the wrong player; the tip was missed after 10 minutes of retries."""
    from tradebot.exchange_bot import _best_index
    listed = ["Adrian Andreev v Pietri", "Mannarino v Alek Shevchenko"]
    assert _best_index(listed, "Adrian M", "Adrian") is None  # Andreev isn't M: ESPN's draws or the AI say who
    assert _best_index(listed, "Adrian A", "Adrian") == 0
    assert _best_index(["Adrian Andreev v Pietri", "Adrian Mannarino v Alek Shevchenko"], "Adrian M", "Adrian") == 1
    assert _best_index(["Tallon Griekspoor v Brandon Nakashima"], "Nakashima B", "Nakashima") == 0  # surname, initial
    assert _best_index(["Tallon Griekspoor v Brandon Nakashima"], "Nakashima C", "Nakashima") is None
    assert _best_index(["Svrcina v Vi Sachko"], "Vitaliy Sachko v ?", "Vitaliy Sachko") == 0  # no initial: as before


def test_the_ais_pick_must_fit_the_tips_initial(store):
    from tradebot.exchange_bot import ExchangeBot
    from tradebot.risk import RiskManager
    settings = make_settings()
    bot = ExchangeBot(settings, RiskManager(settings, store, "dry_run"))
    listed = ["Andreev v Pietri", "Mannarino v Alek Shevchenko"]
    picks = {"Adrian M v ?": "Andreev", "Adrian A v ?": "Andreev"}

    async def resolve(name, event, names):
        return picks[event]

    bot.resolve_name = resolve
    assert asyncio.run(bot._found_by_ai(listed, "Adrian M v ?", ["Adrian"])) == {}  # the AI's Andreev isn't M
    assert asyncio.run(bot._found_by_ai(listed, "Adrian A v ?", ["Adrian"])) == {"Andreev": 0}  # asked again: another player
    picks["Adrian M v ?"] = "Mannarino"
    bot._resolved.clear()
    assert asyncio.run(bot._found_by_ai(listed, "Adrian M v ?", ["Adrian"])) == {"Mannarino": 1}


def test_its_other_posts_are_left_to_the_ai():
    for text in ("SINGLE LIMIT SE KHAO", "48 PAISE KHAO", "37 PAISA KHAO", "33 PAISE,....", "NONCUTTING PAAS", "HAAR GYA", "WIN THA",
                 "🔥 TENNIS ALL MATCHES EXCLUSIVE ID AVAILABLE 🔥\n\n🌐 http://www.Lotusbook365.center", "📣Important Audio🔼", "WAIT FOR NEXT", ""):
        assert parse_known(text, "raftaar") is None, text


def test_a_tip_the_ai_calls_no_bet_is_still_entered(store):
    """29 Oct: 'WINNER - ALEX M' read as 'No bet instruction', and nothing was entered."""
    pipe, site, _ = make(store, RAFTAAR)
    post(pipe, tip("ALEX M"))
    assert [call[:2] for call in site.calls] == [("entry", "Alex")] and store.find_position("Alex", "live") is not None
    assert pipe._parser.calls == 1  # the AI still reads it: it knows how a name is spelt


def test_the_ais_spelling_of_the_name_is_used_when_it_reads_the_tip_too(store):
    text = tip("HUMBERY HURCURZ")
    pipe, site, _ = make(store, RAFTAAR, {text: [signal(selection="Hubert Hurkacz", event="Lorenzo Musetti v Hubert Hurkacz", odds=None)]})
    post(pipe, text)
    assert store.find_position("Hubert Hurkacz", "live").event == "Lorenzo Musetti v Hubert Hurkacz"
    assert [call[0] for call in site.calls] == ["entry"]


def test_its_booking_goes_through_when_the_ai_cant_answer(store):
    """29 Oct 20:09: 'AI reply unreadable' on Medvedev's booking, so the trade ran to the result."""
    pipe, site, _ = make(store, RAFTAAR)
    post(pipe, tip("MEDVEDEV"))
    post(pipe, BOOKED)
    assert site.calls[-1][0] == "close" and store.find_position("Medvedev", "live") is None


def test_what_the_rules_read_still_goes_at_the_ais_limit(store):
    async def no_ai(*args, **kwargs):
        return None

    pipe, site, _ = make(store, RAFTAAR)
    pipe._parse_while_fresh = no_ai
    post(pipe, tip("MEDVEDEV"))
    assert store.find_position("Medvedev", "live") is not None
    post(pipe, "48 PAISE KHAO")  # no rule for it: only the AI could have read it
    assert len(site.calls) == 1
    post(pipe, BOOKED)
    assert store.find_position("Medvedev", "live") is None
    assert store.rows("SELECT summary FROM messages ORDER BY id")[0] == ("read by its rule: the AI could not",)


def test_a_bare_booking_on_paper_is_no_repeat_of_another_channels(store):
    """A booking in one channel used to answer 'already booked' for any channel's trade closed after this one opened."""
    pipe, site, notifier = make(store, RAFTAAR, mode="dry_run")
    post(pipe, tip("MEDVEDEV"))
    store.open_position(signal(selection="Sixers", event="Sixers v Heat", channel="Other"), "dry_run", 3.0, 1000, 1000)
    store.set_closed(store.find_position("Sixers", "dry_run").id, pl=500.0)
    later = (datetime.now() + timedelta(minutes=1)).isoformat(timespec="seconds")
    store._db.execute("UPDATE positions SET closed_at = ? WHERE selection = 'Sixers'", (later,))
    pipe._last_entry.clear()  # as after a restart: the booking names no trade
    post(pipe, BOOKED)
    assert site.calls[-1][0] == "close" and store.find_position("Medvedev", "dry_run") is None
    assert not any("already booked" in text for text in notifier.sent)


def test_a_channels_own_repeated_booking_is_still_a_repeat(store):
    pipe, site, notifier = make(store, RAFTAAR, mode="dry_run")
    post(pipe, tip("MEDVEDEV"))
    post(pipe, BOOKED)
    calls = len(site.calls)
    store._db.execute("UPDATE positions SET opened_at = '2026-01-01T00:00:00'")  # the next one has no open trade either way
    pipe._last_entry.clear()
    post(pipe, BOOKED)
    assert len(site.calls) == calls


def test_a_booking_of_a_trade_never_held_asks_nothing_of_the_site_on_paper(store):
    """2 Nov: Panther's bookset for an entry the site had refused."""
    close = {"Make Bookset #MLS": [signal(action="close", selection="MLS", event="MLS v ?", odds=None)]}
    pipe, site, notifier = make(store, Profile("tip"), close, mode="dry_run")
    post(pipe, "Make Bookset #MLS")
    assert site.calls == [] and "nothing to book: Tip holds no trade on MLS" in notifier.sent[-1]
    live, site, _ = make(store, Profile("tip"), close, mode="live")  # a trade you placed by hand
    post(live, "Make Bookset #MLS")
    assert [call[0] for call in site.calls] == ["close"]


def test_a_first_name_is_found_in_the_days_draws():
    assert side_of_first_name(DRAWS, "Katie", "Katie V v ?", LISTED) == "Volynets"
    assert side_of_first_name(DRAWS, "Katie", "Katie v ?", LISTED) == "Volynets"  # Boulter isn't on the site's list
    assert side_of_first_name(DRAWS, "Novak", "Novak v ?", LISTED) == "N Djokovic"
    assert side_of_first_name(DRAWS, "Alex", "Alex v ?", LISTED) is None  # two players
    assert side_of_first_name(DRAWS, "Alex", "Alex M v ?", LISTED) is None  # Michelsen, and de Minaur's last word
    assert side_of_first_name(DRAWS, "pkr", "pkr v ?", LISTED) is None
    assert side_of_first_name([Draw("X", ["Katie Bow", "A B"])], "Katie", "Katie v ?", ["Perth Scorchers W v Hobart W"]) is None


def test_the_draws_answer_before_the_ai_is_asked(store):
    asked = []

    async def draws(day):
        return DRAWS

    async def resolve(name, event, listed):
        asked.append(name)
        return "Michelsen"

    pipe, _, _ = make(store, RAFTAAR)
    pipe._parser.resolve_name = resolve
    real, main.tennis_draws = main.tennis_draws, draws
    try:
        assert asyncio.run(pipe.resolve_name("Katie", "Katie V v ?", LISTED)) == "Volynets" and asked == []
        assert asyncio.run(pipe.resolve_name("Alex", "Alex v ?", LISTED)) == "Michelsen" and asked == ["Alex"]
    finally:
        main.tennis_draws = real


def test_espn_being_down_leaves_it_to_the_ai(store):
    async def no_page(day):
        raise OSError("ESPN is down")

    async def resolve(name, event, listed):
        return "Volynets"

    pipe, _, _ = make(store, RAFTAAR)
    pipe._parser.resolve_name = resolve
    real, main.tennis_draws = main.tennis_draws, no_page
    try:
        assert asyncio.run(pipe.resolve_name("Katie", "Katie V v ?", LISTED)) == "Volynets"
    finally:
        main.tennis_draws = real


def test_the_channel_list_reads_it_with_its_rule_and_the_ai():
    channels = Channels.load(make_settings(), Path(__file__).resolve().parent.parent / "channels.example.json")
    profile = channels.get("Raftaar").profile
    assert profile.rules == "raftaar" and not profile.rules_first and not profile.rules_only and profile.paper_only
