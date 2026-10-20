"""Tennis Elo model and value-betting backtest on real historical odds (tennis-data.co.uk).

    python -m tools.tennis_elo download                 # ATP + WTA results with odds, 2013 onwards
    python -m tools.tennis_elo backtest                 # bets from 2023 onwards, ratings built from 2013
    python -m tools.tennis_elo backtest --from 2024-01-01 --min-matches 30

The model rates every player from match results only (overall Elo blended with surface Elo, K-factor
shrinking with experience). Before each match it gives a win chance; a "value bet" is a side whose odds
pay more than that chance is worth (chance × odds − 1 > edge threshold). Each bet is 1 unit, settled on
the real result, so the totals show what following the model would have made at those odds.

The odds are closing prices: PS = Pinnacle (the sharpest bookmaker), BFE = Betfair Exchange (closest to
iob7, recent seasons only), Avg = market average, B365 = Bet365, Max = best price across bookmakers (not
available on any single site).
Files go to data/tennis/ (downloads) and data/tennis/bets.csv (every simulated bet).
"""
import argparse
import csv
import math
import re
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from tradebot.config import DATA_DIR

SITE = "http://www.tennis-data.co.uk/"
FOLDER = DATA_DIR / "tennis"
FIRST_YEAR = 2013  # .xlsx files start here; older years are .xls
SOURCES = ("PS", "BFE", "Avg", "B365", "Max")  # BFE = Betfair Exchange, only in recent seasons
THRESHOLDS = (0.0, 0.05, 0.10, 0.20)
MIN_ODDS, MAX_ODDS = 1.10, 10.0
INITIAL_RATING = 1500.0
SURFACE_WEIGHT = 0.5  # share of the surface rating in the blended rating


# ---------- download ----------

def download(force_all: bool = False) -> None:
    FOLDER.mkdir(parents=True, exist_ok=True)
    page = _fetch(SITE + "alldata.php").decode("latin-1")
    links = sorted(set(re.findall(r'href="([^"]*/(\d{4})(w?)/\d{4}\.xlsx)"', page, re.IGNORECASE)))
    this_year = date.today().year
    for href, year, women in links:
        if int(year) < FIRST_YEAR:
            continue
        out = FOLDER / f"{'wta' if women else 'atp'}_{year}.xlsx"
        if out.exists() and int(year) < this_year and not force_all:
            continue  # past seasons don't change
        out.write_bytes(_fetch(SITE + href))
        print(f"saved {out.name} ({out.stat().st_size // 1024} KB)")


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


# ---------- reading the spreadsheets ----------

XL = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def read_xlsx(path: Path) -> list[dict[str, str]]:
    """First sheet as a list of {header: value}. Standard library only (no openpyxl)."""
    with zipfile.ZipFile(path) as z:
        strings = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{XL}si"):
                strings.append("".join(t.text or "" for t in si.iter(f"{XL}t")))
        sheet_name = sorted(n for n in z.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))[0]
        rows = []
        for row in ET.fromstring(z.read(sheet_name)).iter(f"{XL}row"):
            cells = {}
            for c in row.iter(f"{XL}c"):
                column = re.match(r"[A-Z]+", c.get("r")).group()
                value = c.find(f"{XL}v")
                if c.get("t") == "s":
                    cells[column] = strings[int(value.text)] if value is not None else ""
                elif c.get("t") == "inlineStr":
                    cells[column] = "".join(t.text or "" for t in c.iter(f"{XL}t"))
                else:
                    cells[column] = value.text if value is not None else ""
            rows.append(cells)
    header = {col: (name or "").strip() for col, name in rows[0].items()}
    return [{header[col]: v for col, v in r.items() if col in header} for r in rows[1:]]


@dataclass
class Match:
    tour: str
    day: date
    surface: str
    winner: str
    loser: str
    walkover: bool
    odds: dict[str, tuple[float, float]] = field(default_factory=dict)  # source -> (winner, loser)


def load_matches() -> list[Match]:
    matches = []
    for path in sorted(FOLDER.glob("*.xlsx")):
        tour = path.stem.split("_")[0].upper()
        for i, row in enumerate(read_xlsx(path)):
            day = _to_date(row.get("Date", ""))
            winner, loser = row.get("Winner", "").strip(), row.get("Loser", "").strip()
            if not (day and winner and loser):
                continue
            odds = {}
            for source in SOURCES:
                w, l = _to_float(row.get(f"{source}W")), _to_float(row.get(f"{source}L"))
                if w > 1 and l > 1:
                    odds[source] = (w, l)
            matches.append(Match(tour, day, row.get("Surface", "").strip() or "Hard", winner, loser,
                                 "walkover" in row.get("Comment", "").lower(), odds))
    matches.sort(key=lambda m: m.day)  # stable: keeps the files' round order within a day
    return matches


def _to_date(value: str) -> date | None:
    value = (value or "").strip()
    try:
        return date(1899, 12, 30) + timedelta(days=int(float(value)))  # Excel serial day
    except ValueError:
        for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(value[:10], fmt).date()
            except ValueError:
                pass
    return None


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# ---------- the model ----------

class Elo:
    """Overall + surface Elo, K = 250 / (matches + 5) ** 0.4 (FiveThirtyEight's tennis Elo)."""

    def __init__(self):
        self.overall: dict[str, float] = defaultdict(lambda: INITIAL_RATING)
        self.by_surface: dict[tuple[str, str], float] = defaultdict(lambda: INITIAL_RATING)
        self.played: Counter = Counter()
        self.played_surface: Counter = Counter()

    def rating(self, player: str, surface: str) -> float:
        return ((1 - SURFACE_WEIGHT) * self.overall[player]
                + SURFACE_WEIGHT * self.by_surface[(surface, player)])

    def win_chance(self, a: str, b: str, surface: str) -> float:
        return _expected(self.rating(a, surface), self.rating(b, surface))

    def update(self, winner: str, loser: str, surface: str) -> None:
        w_s, l_s = (surface, winner), (surface, loser)
        e = _expected(self.overall[winner], self.overall[loser])
        self.overall[winner] += _k(self.played[winner]) * (1 - e)
        self.overall[loser] -= _k(self.played[loser]) * (1 - e)
        e = _expected(self.by_surface[w_s], self.by_surface[l_s])
        self.by_surface[w_s] += _k(self.played_surface[w_s]) * (1 - e)
        self.by_surface[l_s] -= _k(self.played_surface[l_s]) * (1 - e)
        self.played[winner] += 1
        self.played[loser] += 1
        self.played_surface[w_s] += 1
        self.played_surface[l_s] += 1


def _expected(a: float, b: float) -> float:
    return 1 / (1 + 10 ** ((b - a) / 400))


def _k(matches: int) -> float:
    return 250 / (matches + 5) ** 0.4


# ---------- backtest ----------

@dataclass
class Bet:
    tour: str
    day: date
    player: str
    opponent: str
    source: str
    threshold: float
    chance: float
    odds: float
    won: bool

    @property
    def pl(self) -> float:
        return self.odds - 1 if self.won else -1.0


def backtest(start: date, min_matches: int) -> None:
    matches = load_matches()
    if not matches:
        raise SystemExit("no data: run  python -m tools.tennis_elo download  first")
    elo = Elo()
    bets: list[Bet] = []
    model_loss = market_loss = scored = 0.0
    for m in matches:
        if m.walkover:
            continue
        rated = min(elo.played[m.winner], elo.played[m.loser]) >= min_matches
        if m.day >= start and rated and m.odds:
            p = elo.win_chance(m.winner, m.loser, m.surface)
            if "PS" in m.odds:  # how well the model predicts compared with the sharpest market
                w, l = m.odds["PS"]
                market = (1 / w) / (1 / w + 1 / l)
                model_loss -= math.log(max(p, 1e-9))
                market_loss -= math.log(market)
                scored += 1
            for source, (w_odds, l_odds) in m.odds.items():
                for threshold in THRESHOLDS:
                    for player, opponent, chance, odds, won in ((m.winner, m.loser, p, w_odds, True),
                                                                 (m.loser, m.winner, 1 - p, l_odds, False)):
                        if MIN_ODDS <= odds <= MAX_ODDS and chance * odds - 1 > threshold:
                            bets.append(Bet(m.tour, m.day, player, opponent, source, threshold,
                                            chance, odds, won))
        elo.update(m.winner, m.loser, m.surface)

    last = max(m.day for m in matches)
    months = max((last.year - start.year) * 12 + last.month - start.month + 1, 1)
    print(f"Data {matches[0].day} to {last}: {len(matches)} matches. Bets from {start} ({months} months), "
          f"both players with {min_matches}+ earlier matches, odds {MIN_ODDS}-{MAX_ODDS}, 1 unit each.\n")
    if scored:
        print(f"Prediction quality on {int(scored)} matches (log-loss, lower = better): "
              f"model {model_loss / scored:.4f}  vs  Pinnacle market {market_loss / scored:.4f}\n")

    print(f"{'tour':4} {'odds':5} {'edge>':>5} {'bets':>6} {'/month':>7} {'win%':>5} {'avg odds':>8} "
          f"{'units':>8} {'ROI':>6} {'units/mo':>8} {'worst run':>9}")
    for tour in ("ATP", "WTA", "ALL"):
        for source in SOURCES:
            for threshold in THRESHOLDS:
                chosen = [b for b in bets if b.source == source and b.threshold == threshold
                          and tour in (b.tour, "ALL")]
                if chosen:
                    _summary_line(tour, source, threshold, chosen)
        print()

    for source in ("BFE", "PS"):
        chosen = [b for b in bets if b.source == source and b.threshold == 0.05]
        print(f"Month by month, ATP+WTA, {source} odds, edge > 5%:")
        by_month = defaultdict(list)
        for b in chosen:
            by_month[b.day.strftime("%Y-%m")].append(b)
        for month, group in sorted(by_month.items()):
            units = sum(b.pl for b in group)
            print(f"  {month}  bets {len(group):4}  won {sum(b.won for b in group):4}  units {units:+8.2f}")
        print()

    out = FOLDER / "bets.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["tour", "date", "player", "opponent", "odds_source", "edge_threshold",
                         "model_chance", "odds", "won", "units"])
        for b in bets:
            writer.writerow([b.tour, b.day, b.player, b.opponent, b.source, b.threshold,
                             f"{b.chance:.3f}", b.odds, int(b.won), f"{b.pl:.2f}"])
    print(f"every bet: {out}")


def _summary_line(tour: str, source: str, threshold: float, chosen: list[Bet]) -> None:
    chosen = sorted(chosen, key=lambda b: b.day)
    first, last = chosen[0].day, chosen[-1].day
    months = (last.year - first.year) * 12 + last.month - first.month + 1  # odds sources cover different years
    units = sum(b.pl for b in chosen)
    running = peak = worst = 0.0
    for b in chosen:
        running += b.pl
        peak = max(peak, running)
        worst = min(worst, running - peak)
    print(f"{tour:4} {source:5} {threshold:5.0%} {len(chosen):6} {len(chosen) / months:7.0f} "
          f"{sum(b.won for b in chosen) / len(chosen):5.0%} {sum(b.odds for b in chosen) / len(chosen):8.2f} "
          f"{units:+8.1f} {units / len(chosen):+6.1%} {units / months:+8.1f} {worst:9.1f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)
    dl = sub.add_parser("download", help="fetch ATP and WTA results with odds")
    dl.add_argument("--all", action="store_true", help="download every season again")
    bt = sub.add_parser("backtest", help="replay the model on past matches")
    bt.add_argument("--from", dest="start", default="2023-01-01", help="first day to bet (YYYY-MM-DD)")
    bt.add_argument("--min-matches", type=int, default=20, help="skip players with fewer earlier matches")
    args = ap.parse_args()
    if args.command == "download":
        download(args.all)
    else:
        backtest(date.fromisoformat(args.start), args.min_matches)
