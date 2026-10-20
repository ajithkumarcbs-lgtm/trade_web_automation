"""Checks how the AI reads typical instructions (tests/parser_cases.json). Calls OpenAI (about $0.03 a run).

    python -m tools.check_parser            # each message once
    python -m tools.check_parser --runs 3   # each message 3 times: the AI's answers can vary between calls

Run it after changing OPENAI_MODEL or the AI instructions. Exit code 1 if any case fails.
In parser_cases.json an expected value can be a list of accepted values, e.g. "selection": ["Broska", ""]
for a book set where an empty name is fine because the bot then uses its latest open trade.
"""
import argparse
import asyncio
import json
import sys

from tradebot.config import ROOT, load_settings
from tradebot.models import as_bet
from tradebot.names import NAME_MATCH_MIN, name_score
from tradebot.signal_parser import make_parser

CASES = ROOT / "tests" / "parser_cases.json"


def matches(field: str, got, want) -> bool:
    if isinstance(want, list):
        return any(matches(field, got, w) for w in want)
    if field == "selection":
        return name_score(want, got) >= NAME_MATCH_MIN if want else not got.strip(" ?")
    if isinstance(want, (int, float)) and got is not None:
        return abs(float(got) - want) < 1e-6
    return got == want


def problems_with(parsed, expected: list[dict]) -> list[str]:
    problems = []
    if len(parsed.signals) != len(expected):
        problems.append(f"{len(parsed.signals)} instruction(s), want {len(expected)}")
    for sig, expect in zip(parsed.signals, expected):
        problems += [f"{field}={getattr(sig, field)!r} (want {want!r})"
                     for field, want in expect.items() if not matches(field, getattr(sig, field), want)]
    return problems


async def main(runs: int) -> int:
    settings = load_settings()
    settings.require("groq_api_key" if settings.ai_provider == "groq" else "openai_api_key")
    parser = make_parser(settings)
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    failed_cases = 0
    for case in cases:
        failures = []
        for _ in range(runs):
            parsed = await parser.parse(case["text"], None, case["context"])
            problems = problems_with(parsed, case["expect"])
            if problems:
                failures.append((parsed, problems))
        failed_cases += bool(failures)
        label = "PASS" if not failures else f"FAIL {len(failures)}/{runs}"
        shown = failures[0][0] if failures else parsed
        got = "; ".join(as_bet(s).describe() for s in shown.signals) or "no instruction"
        print(f"{label:9} {case['text']!r:40} -> {got}")
        for problem in failures[0][1] if failures else []:
            print(f"          {problem}")
    print(f"\n{len(cases) - failed_cases}/{len(cases)} cases passed every run ({runs} run(s), {parser.name})")
    return 1 if failed_cases else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    sys.exit(asyncio.run(main(ap.parse_args().runs)))
