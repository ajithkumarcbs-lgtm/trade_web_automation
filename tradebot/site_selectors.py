"""Where things are on iob7.com.

Only the login inputs are confirmed (from the site's code). The betting screens could not be
inspected yet, so every TODO below must be filled in from a page snapshot
(`python -m tools.snapshot_page`) before the browser bot will start.
"""
TODO = "TODO"

# Sport list pages. Betfair-style ids (2 = tennis, 4 = cricket): confirm by clicking the sport on the site.
SPORT_PATHS = {"tennis": "/sport/2", "cricket": "/sport/4"}

LOGIN_PATH = "/login"
LOGIN_USERNAME = 'input[name="username"]'
LOGIN_PASSWORD = 'input[name="password"]'
LOGIN_SUBMIT = 'button[type="submit"], button:has-text("Login")'

# Sport list: one element per match; its text contains both team/player names.
EVENT_ITEM = TODO

# Match page: one container per market, whose text includes the market title.
MARKET_BLOCK = TODO
MARKET_TITLES = {"match_odds": "Match Odds", "bookmaker": "Bookmaker", "fancy": "Fancy"}
# One row per team / player / fancy session inside a market block.
RUNNER_ROW = TODO
# Clickable odds boxes inside a runner row, left to right.
# Match Odds desktop: back3 back2 BACK1 | LAY1 lay2 lay3 (mobile shows only BACK1 | LAY1).
PRICE_CELL = TODO
FANCY_NO_INDEX = 0
FANCY_YES_INDEX = 1

# Bet slip
STAKE_INPUT = TODO
ODDS_INPUT = TODO  # exits type the tipster's price here
UNMATCH_OPTION = TODO  # the slip's "Place Bet in Unmatch" control, so an exit can wait for its price
PLACE_BET = 'button:has-text("Place Bet")'
CANCEL_BET = 'button:has-text("Cancel")'
BET_SUCCESS = TODO
BET_ERROR = TODO


def check_configured() -> None:
    missing = [name for name, value in globals().items() if name.isupper() and value == TODO and name != "TODO"]
    if missing:
        raise SystemExit(
            "Browser selectors not set yet: " + ", ".join(missing) + ".\n"
            "Run `python main.py --signals-only` meanwhile, and save a match page with "
            "`python -m tools.snapshot_page` so the selectors can be filled in."
        )
