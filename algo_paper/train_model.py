"""Trains the paper algo's T20 win-probability model from Cricsheet's ball-by-ball archives (cricsheet.org).

    python -I algo_paper/train_model.py <folder with the *_json.zip files> algo_paper/model.json

Two logistic models per gender, fitted by IRLS on the state after every delivery of 20-over matches with a winner
(no D/L, no reduced innings, no super overs):
- chase: P(the chasing side wins | runs needed, balls left, wickets left)
- first: P(the side batting first wins | runs, balls bowled, wickets lost)
15% of the matches are held out to report how well the model is calibrated. Standard library only.
"""
from __future__ import annotations

import json
import math
import random
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

NOT_OUT = {"retired hurt", "retired not out"}


def chase_features(runs_needed: int, balls_left: int, wickets_left: int) -> list[float]:
    rrr = min(36.0, 6.0 * runs_needed / max(balls_left, 1))
    w = float(wickets_left)
    lb = math.log1p(balls_left)
    return [1.0, rrr, w, rrr * w / 10, lb, runs_needed / 100, w * lb / 10]


def first_features(runs: int, balls: int, wickets: int) -> list[float]:
    frac = balls / 120
    rate = (runs / 100) / (frac + 0.05)
    x = float(wickets)
    return [1.0, runs / 100, x, frac, rate * frac, x * (1 - frac), x * x / 10]


def sigmoid(z: float) -> float:
    return 1 / (1 + math.exp(-max(-35.0, min(35.0, z))))


def states_of(match: dict) -> tuple[str, list[tuple], list[tuple]] | None:
    """(gender, first-innings rows, chase rows) or None when the match doesn't fit. A row is (features key, won)."""
    info = match.get("info", {})
    if info.get("overs") != 20 or info.get("match_type") not in ("T20", "IT20"):
        return None
    outcome = info.get("outcome", {})
    winner = outcome.get("winner")
    if not winner or "method" in outcome or "eliminator" in outcome:
        return None
    innings = [i for i in match.get("innings", []) if not i.get("super_over")]
    if len(innings) != 2:
        return None
    if any(i.get("target", {}).get("overs", 20) != 20 for i in innings):
        return None
    first, chase = [], []
    target = None
    for number, inn in enumerate(innings):
        won = int(inn.get("team") == winner)
        runs = wickets = balls = 0
        rows = first if number == 0 else chase
        if number == 1:
            target = inn.get("target", {}).get("runs") or (first_total + 1)
            rows.append(((target, 120, 10), won))
        else:
            rows.append(((0, 0, 0), won))
        for over in inn.get("overs", []):
            for ball in over.get("deliveries", []):
                runs += ball.get("runs", {}).get("total", 0)
                extras = ball.get("extras", {})
                if "wides" not in extras and "noballs" not in extras:
                    balls += 1
                wickets += sum(1 for w in ball.get("wickets", []) if w.get("kind") not in NOT_OUT)
                if number == 0:
                    if balls < 120 and wickets < 10:
                        rows.append(((runs, balls, wickets), won))
                else:
                    need, left, wleft = target - runs, 120 - balls, 10 - wickets
                    if need > 0 and left > 0 and wleft > 0:
                        rows.append(((need, left, wleft), won))
        if number == 0:
            first_total = runs
    return info.get("gender", "male"), first, chase


def fit(rows: dict[tuple, list[int]], features, iterations: int = 25) -> list[float]:
    """Weighted logistic regression by IRLS (Newton), with a small ridge for stability. rows: key -> [wins, n]."""
    data = [(features(*key), wins, n) for key, (wins, n) in rows.items()]
    k = len(data[0][0])
    beta = [0.0] * k
    for _ in range(iterations):
        grad = [0.0] * k
        hess = [[0.0] * k for _ in range(k)]
        for x, wins, n in data:
            p = sigmoid(sum(b * xi for b, xi in zip(beta, x)))
            g = wins - n * p
            h = n * p * (1 - p)
            for i in range(k):
                grad[i] += g * x[i]
                hi = h * x[i]
                row = hess[i]
                for j in range(i, k):
                    row[j] += hi * x[j]
        for i in range(k):
            hess[i][i] += 1e-3
            for j in range(i):
                hess[i][j] = hess[j][i]
        step = solve(hess, grad)
        beta = [b + s for b, s in zip(beta, step)]
        if max(abs(s) for s in step) < 1e-7:
            break
    return beta


def solve(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[piv] = m[piv], m[col]
        for r in range(n):
            if r != col and m[col][col]:
                f = m[r][col] / m[col][col]
                m[r] = [x - f * y for x, y in zip(m[r], m[col])]
    return [m[i][n] / m[i][i] for i in range(n)]


def evaluate(rows: list[tuple], beta: list[float], features) -> dict:
    loss = brier = 0.0
    buckets = defaultdict(lambda: [0, 0.0, 0])
    for key, won in rows:
        p = min(max(sigmoid(sum(b * x for b, x in zip(beta, features(*key)))), 1e-6), 1 - 1e-6)
        loss -= math.log(p if won else 1 - p)
        brier += (p - won) ** 2
        bucket = buckets[min(9, int(p * 10))]
        bucket[0] += 1
        bucket[1] += p
        bucket[2] += won
    n = len(rows)
    table = {f"{b * 10}-{b * 10 + 10}%": f"model {s / c:.3f} real {w / c:.3f} ({c})" for b, (c, s, w) in sorted(buckets.items())}
    return {"states": n, "logloss": round(loss / n, 4), "brier": round(brier / n, 4), "calibration": table}


def main() -> None:
    folder, out = Path(sys.argv[1]), Path(sys.argv[2])
    random.seed(7)
    train = {g: {"first": defaultdict(lambda: [0, 0]), "chase": defaultdict(lambda: [0, 0])} for g in ("male", "female")}
    test = {g: {"first": [], "chase": []} for g in ("male", "female")}
    used = defaultdict(int)
    seen = set()
    for archive in sorted(folder.glob("*_json.zip")):
        with zipfile.ZipFile(archive) as z:
            for name in z.namelist():
                if not name.endswith(".json") or name in seen:
                    continue
                seen.add(name)
                try:
                    match = json.loads(z.read(name))
                except ValueError:
                    continue
                found = states_of(match)
                if not found:
                    continue
                gender, first, chase = found
                used[(archive.name, gender)] += 1
                held_out = random.random() < 0.15
                for kind, rows in (("first", first), ("chase", chase)):
                    if held_out:
                        test[gender][kind].extend(rows)
                    else:
                        for key, won in rows:
                            cell = train[gender][kind][key]
                            cell[0] += won
                            cell[1] += 1
    model = {"source": "cricsheet.org ball-by-ball, 20-over matches with a result", "matches": {}, "models": {}}
    for (archive, gender), count in sorted(used.items()):
        model["matches"][f"{archive} {gender}"] = count
    for gender in ("male", "female"):
        model["models"][gender] = {}
        for kind, features in (("first", first_features), ("chase", chase_features)):
            beta = fit(train[gender][kind], features)
            report = evaluate(test[gender][kind], beta, features)
            model["models"][gender][kind] = {"beta": beta, "holdout": report}
            print(gender, kind, json.dumps(report, indent=1))
    out.write_text(json.dumps(model, indent=1), encoding="utf-8")
    print("matches used:", dict(model["matches"]))
    print("saved", out)


if __name__ == "__main__":
    main()
