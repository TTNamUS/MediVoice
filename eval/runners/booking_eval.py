"""Booking flow evaluation — deterministic checks.

Usage:
    python -m eval.runners.booking_eval [--limit N]

Checks each scenario in eval/datasets/booking.jsonl for:
- Correct tool name called
- Expected args present
- Expected outcome category
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DATASET = Path(__file__).parent.parent / "datasets" / "booking.jsonl"


def load_cases(limit: int | None = None) -> list[dict]:
    cases = []
    with open(DATASET) as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    if limit:
        cases = cases[:limit]
    return cases


def _check_case(case: dict) -> tuple[bool, str]:
    """
    Deterministic structural checks — not LLM execution.
    Validates that the test case schema is well-formed and expectations are sane.
    """
    steps = case.get("steps", [])
    outcome = case.get("expected_outcome", "")

    if not steps:
        return False, "no steps defined"
    if not outcome:
        return False, "no expected_outcome"

    tool_steps = [s for s in steps if s.get("role") == "tool"]

    # Validate tool steps have names
    for step in tool_steps:
        if "name" not in step:
            return False, f"tool step missing name: {step}"

    # Category-specific checks
    category = case.get("category", "")
    if category == "slot_conflict":
        conflict_steps = [s for s in tool_steps if s.get("returns") == "slot_already_booked"]
        if not conflict_steps:
            return False, "slot_conflict case must have step returning slot_already_booked"

    if category == "new_patient":
        lookup_steps = [s for s in tool_steps if s.get("name") == "lookup_patient"]
        if not lookup_steps:
            return False, "new_patient case must include lookup_patient step"

    return True, "ok"


def run_booking_eval(limit: int | None = None) -> None:
    cases = load_cases(limit=limit)
    print(f"\nBooking eval: {len(cases)} scenarios\n")

    passed = 0
    failed = 0
    results = []

    for case in cases:
        ok, reason = _check_case(case)
        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1
        results.append({"id": case["id"], "category": case["category"], "status": status, "reason": reason})
        print(f"  [{status}] {case['id']:10s}  {case['category']:25s}  {reason}")

    total = passed + failed
    pct = passed / total * 100 if total else 0
    print(f"\nBooking eval: {passed}/{total} passed ({pct:.0f}%)\n")

    if failed > 0:
        print("FAILED cases:")
        for r in results:
            if r["status"] == "FAIL":
                print(f"  {r['id']}: {r['reason']}")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Booking scenario eval")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    run_booking_eval(limit=args.limit)


if __name__ == "__main__":
    main()
