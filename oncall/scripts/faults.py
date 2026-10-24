"""Inject and reset the 10 practice incidents.

Run from the repo root:
  python oncall/scripts/faults.py list
  python oncall/scripts/faults.py inject payment_flag
  python oncall/scripts/faults.py status
  python oncall/scripts/faults.py reset
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scenarios import SCENARIOS, faultctl  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(prog="faults")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("status")
    sub.add_parser("reset")
    inject = sub.add_parser("inject")
    inject.add_argument("name", choices=list(SCENARIOS))
    args = parser.parse_args()

    if args.cmd == "list":
        for name, s in SCENARIOS.items():
            how = " ".join(s["inject"][1:]) if s["inject"] else "not injectable (needs tc, ask a mentor)"
            print(f"{name:22} {how:45} fix: {s['fix']}")
    elif args.cmd == "inject":
        inject_args = SCENARIOS[args.name]["inject"]
        if inject_args is None:
            raise SystemExit(f"{args.name} cannot be injected with faultctl")
        print(faultctl(*inject_args))
    else:
        print(faultctl(args.cmd))


if __name__ == "__main__":
    main()
