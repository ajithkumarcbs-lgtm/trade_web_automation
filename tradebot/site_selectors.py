"""Where things are on iob7.com.

Home page list: confirmed on a saved page (data/snapshots). Match page and bet slip: taken from the
site's code, Oct 2026. The bot finds the match in the home list, opens its match page, and bets there.
"""
# Home page lists in-play and upcoming cricket and tennis matches.
LIST_PATH = "/"
MATCH_URL = "**/sports/details/**"

# Header LOGIN button (shown only when logged out) opens the "Login Now" form.
LOGGED_OUT = 'button:text-is("LOGIN")'
LOGIN_USERNAME = 'input[name="user"]'  # "username/mobile number"
LOGIN_PASSWORD = 'input[name="password"]'
LOGIN_SUBMIT = 'button:has-text("Log In")'

# Home list: one desktop row per match; its name span reads "<A> v <B>". Clicking it opens the match page.
EVENT_ROW = 'div[class*="cursor-pointer md:flex"]:has(span.max-line-1)'
EVENT_NAME = "span.max-line-1"

# Match page: one row per team (Match Odds first), name on the left, odds boxes on the right:
# back3 back2 BACK1 | LAY1 lay2 lay3 (narrow screens show only BACK1 | LAY1).
RUNNER_ROW = "div.grid.grid-cols-12:has(div.bl-box)"
RUNNER_NAME = "div.text-ellipsis"
# Your P/L if that runner wins, under its name (green 9.00 / red -10.00); a second span shows the slip preview.
# The runners of one market share a parent element.
RUNNER_BOOK = "div.text-ellipsis + div span"
PRICE_CELL = "div.bl-box"
SUSPENDED_TEXT = "SUSPENDED"

# Bet slip (blue for back, pink for lay). It exists twice (inline for narrow screens, side panel for
# wide ones), so the bot uses the visible one. Fields below are searched inside it.
BET_SLIP = 'div.rounded-b-md:has(input[min="1.01"])'
ODDS_INPUT = 'input[min="1.01"]'
STAKE_INPUT = 'input[min="0"]'
UNMATCH_CHECKBOX = 'input[type="checkbox"]'  # "Place Bet in Unmatch", shown on Match Odds only
QUICK_STAKE = "button"  # stake buttons labelled 100, 500, 1K, 5K, 10K ... (also MIN/MAX STAKE, ALL IN, CLEAR)
SLIP_LIMITS_TEXT = "Min Bet"  # "Min Bet: 10 Max Bet: 25000" in red under the stake buttons

# Header: "BAL <amount>" and "EXP <amount>"; a bet the site accepts changes one of them.
BALANCE = "xpath=//span[normalize-space(text())='BAL']/b"
EXPOSURE = "xpath=//span[normalize-space(text())='EXP']/b"
CANCEL_BET = 'button:has-text("Cancel")'
PLACE_BET = 'button[class*="bg-[#52b202]"]'

# Messages after placing (react-toastify)
TOAST = ".Toastify__toast"
BET_SUCCESS = ".Toastify__toast--success"
BET_ERROR = ".Toastify__toast--error"
PRICE_MOVED_ERROR = "unmatch"  # "Unmatch Bet Not Allow." = the price moved before the bet was accepted

# Pop-ups (reactjs-popup): ads after login, notices. Closed with Escape or their close icon.
POPUP_OVERLAY = "div.popup-overlay"
POPUP_CLOSE = ".popup-content svg.cursor-pointer, .popup-content [class*='close']"

# If "1 Click Betting" were on, clicking an odds box would place a bet with no slip. Must stay off.
ONE_CLICK_CHECKBOX = 'div:has-text("1 Click Betting") > input[type="checkbox"]'

# Book set / loss cut: Match Odds header shows "Cashout: <P/L>" when you hold a position.
# Never use "Turbo Cashout" (7% fee).
# The Match Odds header is itself a <button> containing the Cashout button, so only the innermost one counts.
CASHOUT_BUTTON = 'button:not(:has(button)):has-text("Cashout:")'
TURBO_CASHOUT = 'div:text-is("Turbo Cashout")'
CASHOUT_CONFIRM = '.popup-content button:text-is("CASHOUT")'  # "Do you wish to perform cash out?"
CASHOUT_CANCEL = '.popup-content button:text-is("CANCEL")'

# Open Bets panel (right side of the match page): Unmatch tab lists waiting bets, each with a red cancel
# circle; the row's cells end with stake, P/L, cancel. Cancelling asks "You want to cancel bet?" -> Yes.
UNMATCH_TAB = 'div.cursor-pointer:has(> div:text-is("Unmatch"))'
MATCHED_TAB = 'div.cursor-pointer:has(> div:text-is("Matched"))'
UNMATCH_CANCEL = 'div.cursor-pointer[class*="bg-[darkred]"]'
CONFIRM_YES = '.popup-content button:text-is("Yes")'
