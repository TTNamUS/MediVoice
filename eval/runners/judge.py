"""LLM-as-judge scorer for MediVoice eval runs.

Uses the configured LLM provider to score each eval case on 5 dimensions (1–5):
  1. Correctness    — factually correct given KB/DB context
  2. Tool use       — right tools called with right arguments
  3. Handoff        — agent routing appropriate
  4. Voice-friendly — response natural to hear aloud
  5. Hallucination  — 5=no hallucination, 1=fabricated critical facts

Pass threshold: avg ≥ 4.0 AND hallucination score ≥ 4.5 (stricter — medical domain).

Anthropic judge calls use prompt caching for the static rubric.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# ── Rubric (prompt-cached system prompt) ─────────────────────────────────────

_RUBRIC_SYSTEM = """\
You are an expert evaluator for a voice AI receptionist at a dental clinic.
You assess bot responses on 5 dimensions, each scored 1–5.

SCORING RUBRIC:

1. Correctness (1–5)
   5 = Fully correct; every fact matches the knowledge base or database result.
   4 = Mostly correct; minor omission but no wrong facts.
   3 = Partially correct; one wrong fact or significant omission.
   2 = Mostly incorrect; key facts wrong.
   1 = Completely wrong or refused when it shouldn't have.

2. Tool use (1–5)
   5 = Called the right tools with correct arguments; no unnecessary calls.
   4 = Correct tools; minor argument issue.
   3 = Called some correct tools; missed one or called an extra wrong tool.
   2 = Called wrong tools or with badly wrong arguments.
   1 = Called no tools when tools were required, or hallucinated a tool call.
   N/A = No tools were expected or required.

3. Handoff (1–5)
   5 = Routed to the correct specialist agent immediately.
   4 = Routed correctly after one clarifying question.
   3 = Asked for clarification when routing was clear, or routed to wrong agent once.
   2 = Routed to wrong agent.
   1 = Failed to route at all when escalation was required.
   N/A = No handoff was expected.

4. Voice-friendliness (1–5)
   5 = Response is natural to hear aloud; no lists, no markdown, good pacing.
   4 = Mostly natural; minor issue (e.g., slight awkwardness).
   3 = Acceptable but has one phone-unfriendly element (e.g., a URL, bullet point).
   2 = Noticeably unnatural for voice; hard to follow when heard aloud.
   1 = Would be confusing or jarring over phone audio.

5. Hallucination (1–5) — MOST CRITICAL FOR MEDICAL DOMAIN
   5 = No hallucination; every fact stated is grounded in retrieved context or DB result.
   4 = Trivial ungrounded claim (e.g., filler phrase) with no factual impact.
   3 = Minor ungrounded claim; factually plausible but not verified.
   2 = Significant ungrounded claim; a fact stated as true that is not in context.
   1 = Critical hallucination: fabricated price, hours, doctor name, insurance status,
       appointment time, or medical instruction NOT present in retrieved context.

PASS CRITERIA:
- Average of all scored dimensions ≥ 4.0
- Hallucination score ≥ 4.5 (non-negotiable — medical domain)

OUTPUT FORMAT (JSON only, no other text):
{
  "scores": {
    "correctness": <1-5>,
    "tool_use": <1-5 or "N/A">,
    "handoff": <1-5 or "N/A">,
    "voice_friendly": <1-5>,
    "hallucination": <1-5>
  },
  "pass": <true|false>,
  "hallucinated_facts": [<list of specific hallucinated strings, empty if none>],
  "reasoning": "<one sentence explaining the most important score driver>"
}
"""


async def score_case(
    case: dict,
    bot_transcript: str,
    tool_calls_made: list[str],
    retrieved_context: str = "",
    client=None,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Score a single eval case using the configured LLM as judge.

    Args:
        case: The eval case dict (from full.jsonl).
        bot_transcript: The bot's text responses concatenated.
        tool_calls_made: List of tool names the bot actually called.
        retrieved_context: Any KB chunks or DB rows returned to the bot.
        client: Optional pre-initialized LLM client.
        provider: Optional provider name: "anthropic" or "openai".
        model: Optional judge model.

    Returns dict with keys: id, scores, pass, hallucinated_facts, reasoning, avg_score.
    """
    if client is None:
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "apps" / "server"))
        from config import get_settings

        settings = get_settings()
        provider = provider or settings.llm_provider
        model = model or settings.active_llm_model()

        if provider == "openai":
            from openai import OpenAI

            client = OpenAI(api_key=settings.openai_api_key)
        elif provider == "anthropic":
            import anthropic

            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        else:
            raise ValueError(f"Judge does not support LLM_PROVIDER={provider!r}")
    else:
        if provider is None:
            provider = "openai" if hasattr(client, "chat") else "anthropic"
        if model is None:
            model = "gpt-4o-mini" if provider == "openai" else "claude-sonnet-4-6"

    expected_tools = case.get("expected_tools", [])
    expected_outcomes = case.get("expected_outcomes", [])
    hallucination_risk = case.get("hallucination_risk", "medium")
    rubric_notes = case.get("rubric_notes", "")

    user_message = f"""\
EVAL CASE: {case['id']}
Category: {case['category']} | Hallucination risk: {hallucination_risk}
Rubric notes: {rubric_notes}

USER TURNS:
{chr(10).join(f"  User: {t}" for t in case.get("user_turns", []))}

EXPECTED TOOLS: {expected_tools}
EXPECTED OUTCOMES: {expected_outcomes}
TOOLS ACTUALLY CALLED: {tool_calls_made}

RETRIEVED CONTEXT (KB chunks / DB rows given to bot):
{retrieved_context or "(none)"}

BOT RESPONSE TRANSCRIPT:
{bot_transcript or "(no response captured)"}

Score this interaction using the rubric. Return JSON only."""

    try:
        if provider == "openai":
            response = client.chat.completions.create(
                model=model,
                max_completion_tokens=512,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": _RUBRIC_SYSTEM},
                    {"role": "user", "content": user_message},
                ],
            )
            raw = (response.choices[0].message.content or "").strip()
        elif provider == "anthropic":
            response = client.messages.create(
                model=model,
                max_tokens=512,
                system=[
                    {
                        "type": "text",
                        "text": _RUBRIC_SYSTEM,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_message}],
            )
            raw = response.content[0].text.strip()
        else:
            raise ValueError(f"Judge does not support LLM_PROVIDER={provider!r}")

        # Strip markdown code fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        result = json.loads(raw)
    except Exception as e:
        logger.warning("Judge scoring failed for %s: %s", case["id"], e)
        result = {
            "scores": {
                "correctness": 1,
                "tool_use": 1,
                "handoff": "N/A",
                "voice_friendly": 1,
                "hallucination": 1,
            },
            "pass": False,
            "hallucinated_facts": [],
            "reasoning": f"Judge error: {e}",
        }

    # Compute average (exclude N/A)
    numeric_scores = [
        v for v in result["scores"].values() if isinstance(v, (int, float))
    ]
    avg = sum(numeric_scores) / len(numeric_scores) if numeric_scores else 0.0
    hallucination_score = result["scores"].get("hallucination", 1)

    # Enforce pass criteria
    passes = avg >= 4.0 and (
        isinstance(hallucination_score, (int, float)) and hallucination_score >= 4.5
    )
    result["pass"] = passes
    result["avg_score"] = round(avg, 2)
    result["id"] = case["id"]

    return result
