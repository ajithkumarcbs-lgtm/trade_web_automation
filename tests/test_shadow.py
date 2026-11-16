"""A channel followed in 'shadow' while the bot is live (IYYAPPA's group from 17 Nov): its posts are logged with the
site's price at each entry / close it names, and nothing is ever placed."""
import asyncio

from conftest import signal
from test_channels_rules import make, msg
from tradebot.channels import Profile

BHW = dict(selection="Brisbane Heat", event="Melbourne Stars v Brisbane Heat", sport="cricket")


def test_a_shadow_channel_logs_prices_and_places_nothing(store, tmp_path):
    script = {"Match 🏆 BHW but jackpot": [signal(odds=None, **BHW)],
              "Book set": [signal(action="close", odds=None, **BHW)]}
    pipe, site, _ = make(store, Profile("iyy", paper_only=True, shadow=True), script, live=2.9)

    async def go():
        for text in script:
            await pipe._handle(msg(text))

    asyncio.run(go())
    assert site.calls == []  # never placed
    assert store.find_position("Brisbane Heat", "live") is None and store.find_position("Brisbane Heat", "shadow") is None
    rows = store._db.execute(
        "SELECT b.mode, b.action, b.status, b.price, m.text FROM bets b JOIN messages m ON m.id = b.message_id "
        "ORDER BY b.id").fetchall()
    assert rows == [("shadow", "entry", "shadow", 2.9, "Match 🏆 BHW but jackpot"),
                    ("shadow", "close", "shadow", 2.9, "Book set")]


def test_a_paper_only_channel_without_shadow_is_still_ignored_live(store):
    script = {"Match 🏆 BHW but jackpot": [signal(odds=None, **BHW)]}
    pipe, site, _ = make(store, Profile("iyy", paper_only=True), script, live=2.9)
    asyncio.run(pipe._handle(msg("Match 🏆 BHW but jackpot")))
    assert site.calls == []
    assert store._db.execute("SELECT COUNT(*) FROM bets").fetchone()[0] == 0
