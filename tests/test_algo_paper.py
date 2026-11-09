"""The paper algo's page reading and booking arithmetic (algo_paper/), on text copied from iob7's match page."""
from algo_paper.live import read_section, same_side, winner_of
from algo_paper.paper import Price, book, locked
from tradebot.fancy import Match
from datetime import datetime

PAGE = """MATCH ODDS
Cashout
Matched: 2.6M
Min: 10 | Max: 2K
BACK
LAY
Chitwan Rhinos
0.0
2.28
41.8
2.36
19.0
2.38
430.1
2.42
373.3
2.44
418.7
2.88
10.4
Kathmandu Gurkhas
0.0
1.56
13.3
1.7
600.9
1.71
528.3
1.72
595.2
1.73
26.0
1.78
53.6
🏏🔥 BIG BASH WOMEN CUP WINNER MARKET IS NOW LIVE! 🎉🏆💥
BOOKMAKER 0 COMMISSION
Cashout
Min: 10 | Max: 50K
BACK
LAY
Chitwan Rhinos
0.0
-
50K
-
50K
133
50K
142
50K
-
50K
-
50K
Kathmandu Gurkhas
0.0
SUSPENDED
68
50K
69
50K
70
50K
75
50K
76
50K
77
50K
FANCY""".splitlines()
RUNNERS = ["Chitwan Rhinos", "Kathmandu Gurkhas"]


def test_match_odds_and_bookmaker_are_read_from_the_page_text():
    mo = read_section(PAGE, lambda u: u == "MATCH ODDS", RUNNERS, rates=False)
    assert mo["Chitwan Rhinos"] == Price(2.38, 2.42, False) and mo["Kathmandu Gurkhas"] == Price(1.71, 1.72, False)
    bm = read_section(PAGE, lambda u: u.startswith("BOOKMAKER"), RUNNERS, rates=True)
    assert (round(bm["Chitwan Rhinos"].back, 2), round(bm["Chitwan Rhinos"].lay, 2)) == (2.33, 2.42)
    assert bm["Kathmandu Gurkhas"].suspended and not bm["Kathmandu Gurkhas"].ok()


def test_booking_arithmetic_matches_the_real_trades():
    assert locked([("back", 50, 2.44)], Price(1.61, 1.62)) == 25.31  # Berrettini, 5 Nov: +25.31 on the site
    assert book([("lay", 50, 1.47), ("lay", 50, 1.18), ("lay", 50, 1.04)]) == (-34.5, 150)  # Renegades, 7 Nov


def test_team_names_and_results():
    assert same_side("Kathmandu Gurkhas", "Kathmandu Gorkhas") and same_side("Hobart Hurricanes W", "Hobart Hurricanes Women")
    assert not same_side("Sydney Sixers W", "Sydney Thunder Women")
    m = Match(1, "x", [("Hobart Hurricanes Women", "HBHW"), ("Adelaide Strikers Women", "ADSW")], "Complete",
              datetime.min, "T20", status="Adelaide Strikers Women won by 5 wkts")
    assert winner_of(m, ["Hobart Hurricanes W", "Adelaide Strikers W"]) == (True, "Adelaide Strikers W")
    m.status = "Match tied (Hobart Hurricanes Women won the Super Over)"
    assert winner_of(m, ["Hobart Hurricanes W", "Adelaide Strikers W"]) == (True, "Hobart Hurricanes W")
