"""Scoreboard: run every injectable incident through the agent and compare with the expected answer.

Run from the repo root:  python oncall/scripts/scoreboard.py [--wait 120] [--settle 300] [--only a,b]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_local import run_scenario  # noqa: E402
from scenarios import SCENARIOS  # noqa: E402

from oncall.config import settings  # noqa: E402

COLUMNS = ["scenario", "expected", "got", "confidence", "escalate", "evidence", "result"]
WIDE = ("scenario", "expected", "got")


def skipped(name: str, scenario: dict) -> dict:
    return {"scenario": name, "expected": f"{scenario['service']}/{scenario['fault_class']}", "got": "not injectable",
            "confidence": "-", "escalate": "-", "evidence": "-", "result": "SKIP"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait", type=int, default=120)
    parser.add_argument("--settle", type=int, default=300, help="seconds to let old faults leave the metric window")
    parser.add_argument("--only", default="")
    args = parser.parse_args()
    names = [n for n in SCENARIOS if not args.only or n in args.only.split(",")]
    rows = [skipped(n, SCENARIOS[n]) if SCENARIOS[n]["inject"] is None else run_scenario(n, SCENARIOS[n], args.wait, args.settle)
            for n in names]

    def cell(row: dict, column: str) -> str:
        return f"{row[column]:<28}" if column in WIDE else f"{row[column]:<10}"

    print("\n" + " | ".join(cell({c: c for c in COLUMNS}, c) for c in COLUMNS))
    for row in rows:
        print(" | ".join(cell(row, c) for c in COLUMNS))
    scored = [r for r in rows if r["result"] != "SKIP"]
    passed = sum(r["result"] == "PASS" for r in scored)
    print(f"\n{passed}/{len(scored)} passed, {len(rows) - len(scored)} skipped")
    out = Path(settings.RUNS_DIR) / "scoreboard.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"saved {out}")


if __name__ == "__main__":
    main()
