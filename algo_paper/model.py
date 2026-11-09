"""The trained win-probability model (model.json from train_model.py) and the market blend.

p_bat_first: the chance that the side batting first wins, from the score alone.
Blend: the market knows team strength, pitch and players, the model only the score. At the first clean read of a
match the gap between them (in log-odds) is taken as that knowledge and carried forward, fading as the balls left in
the match run out. What is left is how the price should move with each ball: a paper trade is a bet that the market
moved too far, or not far enough.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from .train_model import chase_features, first_features, sigmoid

MODEL = json.loads((Path(__file__).resolve().parent / "model.json").read_text(encoding="utf-8"))["models"]


def logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def _dot(beta: list[float], x: list[float]) -> float:
    return sum(b * xi for b, xi in zip(beta, x))


def p_bat_first(gender: str, innings: int, runs: int, balls: int, wickets: int, target: int | None) -> float:
    """innings 1: the batting side's score now; innings 2: the chase (target = runs to win)."""
    models = MODEL["female" if gender == "female" else "male"]
    if innings == 1:
        return sigmoid(_dot(models["first"]["beta"], first_features(runs, balls, wickets)))
    need, left, wleft = (target or 0) - runs, 120 - balls, 10 - wickets
    if need <= 0:
        return 0.0
    if left <= 0 or wleft <= 0:
        return 1.0
    return 1 - sigmoid(_dot(models["chase"]["beta"], chase_features(need, left, wleft)))


def balls_left_in_match(innings: int, balls: int) -> int:
    return max(0, (240 - balls) if innings == 1 else (120 - balls))


def blended(p_model: float, offset: float, left_now: int, left_at_anchor: int) -> float:
    """The model's chance with the market's view of the sides added, fading as the match runs out."""
    if p_model in (0.0, 1.0):
        return p_model
    weight = left_now / left_at_anchor if left_at_anchor else 0.0
    return sigmoid(logit(p_model) + offset * weight)
