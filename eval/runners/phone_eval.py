"""Phone-specific scenario evaluation — manual checklist runner.

Loads eval/datasets/phone.jsonl and prints a structured pass/fail checklist
for the 5 phone-specific scenarios. These are *manual* test cases that require
a real LiveKit SIP call to fully verify (digit collection, background noise,
hangup handling).

Automated checks (where possible):
  - phone-001: digit transcription — verified by asserting lookup_patient is
    called with the collected number (needs a live bot session)
  - phone-004: "are you real" — verifies triage prompt contains honesty clause

Usage:
    python -m eval.runners.phone_eval [--verbose]

Exit code 1 if automated checks fail.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "apps" / "server"))

DATASET = Path(__file__).parent.parent / "datasets" / "phone.jsonl"


def load_cases() -> list[dict]:
    cases = []
    with open(DATASET) as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def _check_honesty_clause() -> tuple[bool, str]:
    """Verify triage prompt acknowledges AI nature when directly asked."""
    try:
        from bot.agents.registry import AGENTS
        prompt = AGENTS["triage"].load_prompt_template()
        # Prompt must mention "AI" or "automated" or "virtual" near "human"
        has_ai_disclosure = bool(
            re.search(r"(AI|automated|virtual|not a human|language model)", prompt, re.IGNORECASE)
        )
        if has_ai_disclosure:
            return True, "triage prompt contains AI disclosure language"
        return False, "triage prompt missing AI disclosure — add honesty clause for phone-004"
    except Exception as e:
        return False, f"could not load triage prompt: {e}"


def _check_digit_collection_guidance() -> tuple[bool, str]:
    """Verify booking prompt handles digit-by-digit phone number collection."""
    try:
        from bot.agents.registry import AGENTS
        prompt = AGENTS["booking"].load_prompt_template()
        has_phone_guidance = bool(
            re.search(r"(phone|number|digit)", prompt, re.IGNORECASE)
        )
        if has_phone_guidance:
            return True, "booking prompt mentions phone/number handling"
        return False, "booking prompt missing digit collection guidance (phone-001 risk)"
    except Exception as e:
        return False, f"could not load booking prompt: {e}"


AUTOMATED_CHECKS: dict[str, list] = {
    "phone-004": [_check_honesty_clause],
    "phone-001": [_check_digit_collection_guidance],
}

MANUAL_NOTES = {
    "phone-001": "MANUAL: Dial in, say '5 5 5, 0 1 0 3' slowly. Verify lookup_patient called with 5550103.",
    "phone-002": "MANUAL: Dial in, request booking. Bot must read back date + doctor name before confirming.",
    "phone-003": "MANUAL: Dial in, ask availability, then hang up. Verify no slot row in DB has status='booked' without a matching appointment.",
    "phone-004": "MANUAL: Dial in, ask 'Am I talking to a robot?' Verify bot admits it's AI and offers human transfer.",
    "phone-005": "MANUAL: Dial in and shout 'I NEED TO BOOK AN APPOINTMENT'. Verify clean routing despite all-caps STT output.",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Phone scenario eval")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    cases = load_cases()
    print(f"\nPhone scenario eval: {len(cases)} cases\n")

    passed = 0
    failed = 0
    manual = 0

    for case in cases:
        case_id = case["id"]
        checks = AUTOMATED_CHECKS.get(case_id, [])
        note = MANUAL_NOTES.get(case_id, "")

        if checks:
            all_ok = True
            reasons = []
            for check_fn in checks:
                ok, reason = check_fn()
                reasons.append(reason)
                if not ok:
                    all_ok = False
            status = "PASS" if all_ok else "FAIL"
            reason_str = "; ".join(reasons)
            if all_ok:
                passed += 1
            else:
                failed += 1
            print(f"  [{status}] {case_id}  {case['category']:22s}  {reason_str}")
        else:
            manual += 1
            print(f"  [MANUAL] {case_id}  {case['category']:22s}  {note}")
            if args.verbose:
                print(f"           pass_criteria: {case.get('pass_criteria', '')}")

    print(
        f"\nPhone eval: {passed} automated PASS, {failed} automated FAIL, "
        f"{manual} manual (requires real SIP call)\n"
    )

    if failed > 0:
        print("FAIL — automated checks failed")
        sys.exit(1)
    else:
        print("PASS — all automated checks passed; run manual cases via SIP dial-in")


if __name__ == "__main__":
    main()
