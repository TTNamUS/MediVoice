"""Handoff routing evaluation — text mode (no real voice pipeline needed).

Simulates the triage agent's routing decision by calling the configured LLM
with the triage system prompt and asserting the expected transfer_to call.

Usage:
    python -m eval.runners.handoff_eval [--limit N] [--verbose]

Exit code 1 if fewer than 80% of cases pass (target: 13/15).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "apps" / "server"))

DATASET = Path(__file__).parent.parent / "datasets" / "handoff.jsonl"
PASS_THRESHOLD = 0.80


def _to_openai_tools(tools: list[dict]) -> list[dict]:
    """Convert Anthropic-style app tool schemas to OpenAI chat function tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
            },
        }
        for tool in tools
    ]


def _build_client(settings) -> tuple[Any, str, str]:
    provider = settings.llm_provider
    model = settings.active_llm_model()

    if provider == "openai":
        from openai import OpenAI

        return OpenAI(api_key=settings.openai_api_key), provider, model
    if provider == "anthropic":
        import anthropic

        return anthropic.Anthropic(api_key=settings.anthropic_api_key), provider, model
    raise ValueError(f"Handoff eval does not support LLM_PROVIDER={provider!r}")


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


async def _simulate_triage(
    user_text: str,
    client,
    provider: str,
    model: str,
) -> dict:
    """Call the configured LLM as triage and extract any transfer_to tool call."""
    from datetime import date

    from bot.agents.orchestrator import TRANSFER_TO_TOOL
    from bot.agents.registry import AGENTS

    system_prompt = AGENTS["triage"].load_prompt_template().format(
        current_date=date.today().isoformat(),
    )

    if provider == "openai":
        response = client.chat.completions.create(
            model=model,
            max_completion_tokens=256,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            tools=_to_openai_tools([TRANSFER_TO_TOOL]),
        )
        message = response.choices[0].message
        for tool_call in getattr(message, "tool_calls", None) or []:
            if tool_call.function.name != "transfer_to":
                continue
            try:
                args = json.loads(tool_call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            return {
                "transferred": True,
                "agent_name": args.get("agent_name", ""),
                "summary": args.get("summary", ""),
            }
    elif provider == "anthropic":
        response = client.messages.create(
            model=model,
            max_tokens=256,
            system=system_prompt,
            tools=[TRANSFER_TO_TOOL],
            messages=[{"role": "user", "content": user_text}],
        )

        # Extract tool use block if present
        for block in response.content:
            if block.type == "tool_use" and block.name == "transfer_to":
                return {
                    "transferred": True,
                    "agent_name": block.input.get("agent_name", ""),
                    "summary": block.input.get("summary", ""),
                }
    else:
        raise ValueError(f"Handoff eval does not support LLM_PROVIDER={provider!r}")

    return {"transferred": False, "agent_name": "", "summary": ""}


async def _run_case(
    case: dict,
    verbose: bool,
    client,
    provider: str,
    model: str,
) -> tuple[bool, str]:
    category = case.get("category", "")
    turns = case.get("turns", [])
    expected_route = case.get("expected_route", [])
    clarify_required = case.get("clarify_required", False)
    summary_must_contain = case.get("summary_must_contain", [])

    if not turns:
        return False, "no turns defined"

    # Use first user turn for simulation
    first_user = next((t["text"] for t in turns if t["role"] == "user"), "")
    if not first_user:
        return False, "no user turn found"

    # For ambiguous cases, we expect triage NOT to immediately transfer
    if clarify_required:
        result = await _simulate_triage(first_user, client, provider, model)
        if result["transferred"]:
            return False, f"expected clarification but got immediate transfer to '{result['agent_name']}'"
        return True, "correctly asked for clarification"

    # For mid-flow and multi-turn cases, only test the first transfer
    if len(expected_route) < 2:
        return True, "skipped (no expected transfer)"

    expected_target = expected_route[1]  # second element = first specialist
    if expected_target in ("triage",):
        return True, "skipped (no transfer expected from first turn)"

    result = await _simulate_triage(first_user, client, provider, model)

    if not result["transferred"]:
        return False, f"expected transfer to '{expected_target}' but no transfer made"

    if result["agent_name"] != expected_target:
        return False, f"expected transfer to '{expected_target}' but got '{result['agent_name']}'"

    # Check summary carryover
    for keyword in summary_must_contain:
        if keyword.lower() not in result["summary"].lower():
            return False, f"summary missing keyword '{keyword}': {result['summary'][:80]}"

    if verbose:
        print(f"    summary: {result['summary'][:80]}")
    return True, f"correctly routed to '{result['agent_name']}'"


async def run_handoff_eval(limit: int | None = None, verbose: bool = False) -> None:
    from config import get_settings

    client, provider, model = _build_client(get_settings())
    cases = load_cases(limit=limit)
    print(f"\nHandoff eval: {len(cases)} cases  provider={provider} model={model}\n")

    passed = 0
    skipped = 0
    failed = 0
    results = []

    for case in cases:
        ok, reason = await _run_case(
            case,
            verbose=verbose,
            client=client,
            provider=provider,
            model=model,
        )
        if "skipped" in reason.lower():
            skipped += 1
            status = "SKIP"
        elif ok:
            passed += 1
            status = "PASS"
        else:
            failed += 1
            status = "FAIL"
        results.append({"id": case["id"], "status": status, "reason": reason})
        print(f"  [{status}] {case['id']:10s}  {case['category']:22s}  {reason}")

    evaluated = passed + failed
    pct = passed / evaluated * 100 if evaluated else 0
    print(f"\nHandoff eval: {passed}/{evaluated} passed ({pct:.0f}%)  |  {skipped} skipped\n")

    if evaluated > 0 and pct < PASS_THRESHOLD * 100:
        print(f"FAIL — handoff eval below threshold {PASS_THRESHOLD * 100:.0f}% (got {pct:.0f}%)")
        sys.exit(1)
    else:
        print(f"PASS — handoff eval at {pct:.0f}% >= {PASS_THRESHOLD * 100:.0f}%")


def main() -> None:
    parser = argparse.ArgumentParser(description="Handoff routing eval")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    asyncio.run(run_handoff_eval(limit=args.limit, verbose=args.verbose))


if __name__ == "__main__":
    main()
