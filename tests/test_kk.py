"""KK Expert: one format for its tennis tips, read by rules with no AI. The posts here are its own, 2026."""
import asyncio
from pathlib import Path

from conftest import make_settings, signal
from test_channels_rules import FakeSite, make, msg
from tradebot.channels import Channels, Profile
from tradebot.risk import booked_profit
from tradebot.rules import parse_known

KK = Profile("kk", rules="kk", rules_only=True, act_on_forwards=False)
CLOSE = "LOSS CUT BOOK SET KARO 🔥🔥🔥\n\nREMEMBER THA NAME #KK"
HALF_ON_THE_FAVOURITE = "22p SHNAIDER HALF LIMIT OR USE KARO UDEGA"
HALF = "23p OPTION \nHALF LIMIT OR USE KARO YHA PE"
SITE_NAMES = {"Fernandez": ("Leylah Fernandez", "L Fernandez v Di Shnaider")}


def entry(player):
    return f"TENNIS MATCH \n\n{player} PLUS KARO GUYS ❤️✔️\n\n#KK✅"


def answering(player):
    return f"[18:35] {entry(player)}"


class SiteWithNames(FakeSite):
    """Gives a trade the site's names, as the real site does."""

    async def place(self, sig, position, require_green=False):
        result = await super().place(sig, position, require_green)
        if sig.action == "entry" and sig.selection in SITE_NAMES:
            result.name, result.event = SITE_NAMES[sig.selection]
        return result


def kk(store, live=2.56):
    return make(store, KK, live=live, site_class=SiteWithNames)


def post(pipe, text, reply_to=""):
    asyncio.run(pipe._handle(msg(text, reply_to=reply_to)))


def one(text, reply_to=""):
    (found,) = parse_known(text, "kk", reply_to)
    return found


def test_its_entries_back_the_player_at_one_limit_at_the_price_on_offer():
    for text, player in ((entry("FERNANDEZ"), "Fernandez"), (entry("ANN LI"), "Ann Li"),
                         ("TENNIS MATCH \n\nFILSPLUS KARO GUYS ❤️✔️\n\n#KK✅", "Fils"),
                         ("TENNIS MATCH \n\nBORGES  PLUS KARO GUYS ❤️✔️\n\n#KK✅", "Borges"),
                         ("TENNIS MATCH \n\nSAWANGKAEW KARO GUYS ❤️✔️\n\n#KK✅", "Sawangkaew"),
                         ("RUNNING MATCH \n\nMARIE PLUS KROO ✅", "Marie")):
        found = one(text)
        assert (found.action, found.selection, found.side, found.sport, found.units, found.odds) == (
            "entry", player, "back", "tennis", None, None), text


def test_a_booking_is_about_the_entry_it_answers():
    found = one(CLOSE, answering("SELEKHMETEVA"))
    assert (found.action, found.selection, found.loss_cut) == ("close", "Selekhmeteva", True)
    assert one("LOSS CUT BOOK SET 🔥🔥🔥\n\nREMEMBER THA NAME #KK", answering("DART")).selection == "Dart"
    assert one("Bookset karte chalna", answering("SARA")).selection == "Sara"
    assert one("25p Ka Jackpot Mubarak ho \n\nCashout Kr Lo 👍💋", answering("DAMAS")).selection == "Damas"
    assert one(CLOSE).selection == ""  # no reply: the channel's latest trade


def test_a_booking_told_as_done_counts_only_as_a_reply():
    for text in ("BOOK SET GAME PASS❤️", "35p VIT SE \n\nBOOK SET GAME PASS❤️", "BOOM BOOM 💥💥 BOOKSET PASS❤️❤️❤️",
                 "20p SE HUM 60P FEV AAYE BOOKSET MATCH PASS❤️", "EK GAME ME SIDHA 3.5-3.5 LIMIT KI BOOK DONE✅",
                 "Sidha 1.5-1.5 limit ka book bana hoga esme", "22 paise oposite se 80 paise ham fav loss cut 😎💪"):
        assert one(text, answering("IGA")).action == "close", text
        assert parse_known(text, "kk") is None, text


def test_a_word_after_the_booking_can_be_no_other_trades_close():
    for text in ("Sidha 1.5-1.5 limit ki bookset❤️", "SIDHA 2.5-2.5 LIMIT KA BOOKSET DONE ENTRY KE HISAB SE SABO KA❤️",
                 "40p CERUNDOLO SE 45P HUM FEV \nSAB KA BOOKSET HO GAYA HOGA ❤️"):
        assert parse_known(text, "kk") is None, text


def test_results_talk_and_promos_are_no_signal():
    for text in ("NONCUTTING WIN PASS❤️❤️✅✅✅", "BOOM BOOM 💥💥 TABILO WIN PASS✅", "04p KA JACKPOT MUBARAK FAMILY❤️",
                 "06 p KA GHODA MUBARAK FAMILY ✅✅", "5/6 PASS TODAY \nAARAM SE 5 SE 6 LIMIT HO SAB AAJ OPEN ME\nGUD NIGHT",
                 "3/3 DONE IN PAID ✅✅", "UDA UDAYA GAME LOSSS GAYA EK SHOT NAHI LAGA", "GM ALL FAMILY ❤️",
                 "30% SHERING WORK DONE✅", "ACTIVE RAHNA 8.10 SE WORK START KAREGE", "Wait", "",
                 "ONLY LOSS ZERO KARO YHA PE \n\nUDER 10P BOOKSET KAREGE ESME ❤️", "Bookset kar lena aapne aap se❤️",
                 "Ye match aaj hoga dekhta hu koi alg agr Oversure hoga to deta hu"):
        assert parse_known(text, "kk") is None, text
        assert parse_known(text, "kk", answering("DART")) is None, text


def test_half_a_limit_more_names_the_favourite_or_answers_the_entry():
    named = one(HALF_ON_THE_FAVOURITE)
    assert (named.action, named.selection, named.event, named.units) == ("add", "", "Shnaider v ?", 0.5)
    assert one(HALF, answering("KALININA")).selection == "Kalinina" and one(HALF, answering("KALININA")).units == 0.5
    quinn = "25p OPTION HAI \nHALF LIMIT SE OR KHAO \n1.5 LIMIT MARIZSAN LOSS PR\n4 LIMIT QUINN PLUS ❤️"
    assert one(quinn, answering("QUINN")).selection == "Quinn"
    bare = one("24p HALF LIMIT SE OR  UTHAA LENA SAB")
    assert (bare.selection, bare.event, bare.units) == ("", "? v ?", 0.5)


def test_a_tip_and_its_booking_take_no_ai(store):
    pipe, site, _ = kk(store)
    post(pipe, entry("FERNANDEZ"))
    trade = store.find_position("Fernandez", "live")
    assert (trade.selection, trade.side, trade.price, trade.stake, trade.sport) == ("Leylah Fernandez", "back", 2.56, 1000, "tennis")
    post(pipe, CLOSE, answering("FERNANDEZ"))
    assert site.calls[-1][0] == "close" and store.find_position("Fernandez", "live") is None
    assert pipe._parser.calls == 0


def test_each_booking_goes_to_the_trade_it_answers(store):
    """28 Oct: Selekhmeteva at 18:35, Tiafoe at 20:50 and booked at 21:12, Selekhmeteva booked at 23:33."""
    pipe, _, _ = kk(store)
    post(pipe, entry("SELEKHMETEVA"))
    post(pipe, entry("TIAFOE"))
    post(pipe, CLOSE, answering("TIAFOE"))
    assert store.find_position("Tiafoe", "live") is None and store.find_position("Selekhmeteva", "live") is not None
    post(pipe, CLOSE, answering("SELEKHMETEVA"))
    assert store.find_position("Selekhmeteva", "live") is None


def test_half_a_limit_on_the_favourite_is_more_on_the_trade_in_its_match(store):
    """1 Nov: Fernandez at 2.56, Shnaider down to 1.22 (Fernandez 5.5), booked at 1.35: his 'sidha 2.5-2.5 limit'."""
    pipe, site, _ = kk(store)
    post(pipe, entry("FERNANDEZ"))
    site.live = 5.5
    post(pipe, HALF_ON_THE_FAVOURITE)
    trade = store.find_position("Fernandez", "live")
    assert site.calls[-1] == ("entry", "Leylah Fernandez", 500) and trade.stake == 1500 and trade.side == "back"
    assert round(booked_profit(trade, 1.35)) == 2433
    assert store.find_position("Shnaider", "live") is None


def test_half_a_limit_as_a_reply_or_alone_goes_on_that_trade_or_the_latest(store):
    pipe, site, _ = kk(store)
    post(pipe, entry("DAMM"))
    post(pipe, entry("FRITZ"))
    post(pipe, HALF, answering("DAMM"))
    assert store.find_position("Damm", "live").stake == 1500 and store.find_position("Fritz", "live").stake == 1000
    post(pipe, "24p HALF LIMIT SE OR  UTHAA LENA SAB")
    assert store.find_position("Fritz", "live").stake == 1500


def test_half_a_limit_with_no_trade_in_that_match_adds_nothing(store):
    pipe, site, notifier = kk(store)
    post(pipe, entry("DAMM"))
    calls = len(site.calls)
    post(pipe, HALF_ON_THE_FAVOURITE)
    assert len(site.calls) == calls and store.find_position("Damm", "live").stake == 1000
    assert "no trade of Tip to add to" in notifier.sent[-1]


def test_a_booking_of_a_trade_never_entered_asks_nothing_of_the_site(store):
    pipe, site, notifier = kk(store)
    post(pipe, CLOSE, answering("DART"))
    post(pipe, CLOSE)
    assert site.calls == [] and "nothing to book" in notifier.sent[-1]


def test_a_booking_drops_an_entry_still_being_tried(store):
    pipe, site, _ = kk(store)
    store.add_pending(signal(selection="Dart", event="Dart v ?", channel="Tip"), "live", None, "market suspended")
    post(pipe, CLOSE, answering("DART"))
    assert store.pending_instructions("live") == [] and site.calls == []


def test_results_cost_no_ai_and_open_nothing(store):
    pipe, site, _ = kk(store)
    for text in ("04p KA JACKPOT MUBARAK FAMILY❤️", "5/6 PASS TODAY", "GM ALL FAMILY ❤️", "30% SHERING WORK DONE✅"):
        post(pipe, text)
    assert site.calls == [] and pipe._parser.calls == 0
    assert len(store.rows("SELECT id FROM messages")) == 4  # kept for the audit


def test_the_channel_list_follows_it_on_paper_only(store):
    channels = Channels.load(make_settings(), Path(__file__).resolve().parent.parent / "channels.example.json")
    channel = channels.for_chat(-1001716508379)
    assert channel.name == "KK Expert" and channel.profile.rules == "kk" and channel.profile.rules_only
    assert channel.profile.paper_only and [c.name for c in channels.named("kk")] == ["KK Expert"]
    pipe, site, _ = make(store, Profile("kk", rules="kk", rules_only=True, paper_only=True), mode="live", site_class=SiteWithNames)
    post(pipe, entry("FERNANDEZ"))
    assert site.calls == []  # DRY_RUN=false must not put it on real money
