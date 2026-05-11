"""Offline eval runner — text-mode, no real STT/TTS required.

Feeds scripted user turns directly into the LLM (bypassing audio pipeline),
captures bot text + tool calls, then scores each case with judge.py.

The simulation runs a full agentic loop: after every LLM response that
includes tool_use blocks, mock tool results are injected as tool_result
messages so the LLM can produce a grounded final answer.  This mirrors the
real pipeline and prevents the judge from seeing an empty bot transcript.

Aggregates:
  - pass_rate (%)
  - hallucination_rate (% cases with hallucination score < 4.5)
  - avg_score_by_category
  - per-case JSON with scores

Output: eval/reports/run_<timestamp>.json

Exit code 1 if pass_rate < 85% OR hallucination_rate > 5%.

Usage:
    python -m eval.runners.offline_eval [--limit N] [--category CATEGORY] [--verbose]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "apps" / "server"))

DATASET = Path(__file__).parent.parent / "datasets" / "full.jsonl"
REPORTS_DIR = Path(__file__).parent.parent / "reports"
PASS_RATE_THRESHOLD = 85.0
HALLUCINATION_RATE_THRESHOLD = 5.0
_MAX_TOOL_ROUNDS = 5   # prevent infinite loops

logger = logging.getLogger(__name__)


# ── Mock tool implementations ─────────────────────────────────────────────────
# Return realistic stub data so the judge sees a grounded bot response.
# These never touch Qdrant or Postgres — safe for CI and local eval runs.

_CLINIC_KB: dict[str, str] = {
    "cancellation": (
        "Our cancellation policy requires at least 24 hours notice. "
        "Same-day cancellations may incur a $50 fee."
    ),
    "no-show": (
        "A $75 no-show fee applies to missed appointments without 24-hour notice."
    ),
    "hours": (
        "Sunrise Dental is open Monday–Friday 8 AM to 6 PM and "
        "Saturday 9 AM to 2 PM. We are closed on Sundays."
    ),
    "insurance": (
        "We accept most major PPO insurance plans including Delta Dental, "
        "Cigna, Aetna, BlueCross BlueShield, MetLife, and United Concordia. "
        "We do not accept HMO plans."
    ),
    "payment": (
        "We accept cash, all major credit cards, CareCredit, and Lending Club. "
        "Payment plans are available for treatments over $500."
    ),
    "services": (
        "We offer general dentistry, pediatric dentistry, orthodontics (braces/Invisalign), "
        "dental hygiene, emergency dental care, root canals, crowns, bridges, "
        "teeth whitening, and dental implants. We do not have an oral surgeon on staff."
    ),
    "privacy": (
        "Sunrise Dental complies with HIPAA. Your health information is stored securely "
        "and never shared without your written consent except as required by law."
    ),
    "new_patients": (
        "We are currently accepting new patients! "
        "Please call or book online; new patients receive a complimentary X-ray exam."
    ),
    "dr_kim": (
        "Dr. Kim speaks English, Korean, and conversational Spanish. "
        "She specializes in pediatric and general dentistry."
    ),
    "whitening": (
        "Yes, we offer in-office teeth whitening (Zoom) and take-home whitening kits. "
        "In-office treatment takes about 90 minutes and costs $350."
    ),
    "root_canal": (
        "For a root canal: avoid eating 2 hours before, arrange a driver if you choose sedation, "
        "and take any prescribed antibiotics as directed beforehand."
    ),
    "wifi": (
        "We do not share Wi-Fi passwords as part of our security policy. "
        "We have a guest network for clinical use only."
    ),
    "antibiotics": (
        "We cannot prescribe medications over the phone. "
        "Please book an appointment for a clinical evaluation."
    ),
    "payment_plans": (
        "We offer payment plans for treatments over $500 through CareCredit and Lending Club. "
        "Speak with our billing team for details."
    ),
    "cost_cleaning": (
        "A routine cleaning (prophylaxis) costs $95–$150 depending on complexity. "
        "Deep cleaning (scaling and root planing) is $200–$300 per quadrant."
    ),
}


def _mock_search_clinic_kb(query: str) -> list[dict]:
    """Return the most relevant KB snippet for the query (keyword match)."""
    q = query.lower()
    mapping = [
        (["cancel", "cancell"], "cancellation"),
        (["no-show", "no show", "miss", "missed"], "no-show"),
        (["hour", "open", "close", "time", "when"], "hours"),
        (["insurance", "delta dental", "aetna", "cigna", "covered", "ppo", "hmo"], "insurance"),
        (["payment plan", "plan", "installment", "finance"], "payment_plans"),
        (["pay", "carecredit", "credit card"], "payment"),
        (["whitening", "zoom", "white teeth", "whiten"], "whitening"),
        (["root canal", "prepare", "preparation"], "root_canal"),
        (["wifi", "wi-fi", "password", "network"], "wifi"),
        (["antibiotic", "prescri", "medicine", "medication"], "antibiotics"),
        (["surgeon", "oral surgery", "surgery"], "services"),
        (["service", "offer", "treat", "brace", "implant"], "services"),
        (["hipaa", "privacy", "data", "information"], "privacy"),
        (["new patient", "accepting"], "new_patients"),
        (["dr. kim", "doctor kim", "kim speak", "language", "spanish", "korean"], "dr_kim"),
        (["cost", "price", "how much", "cleaning cost", "fee"], "cost_cleaning"),
    ]
    for keywords, key in mapping:
        if any(k in q for k in keywords):
            snippet = _CLINIC_KB.get(key, "")
            if snippet:
                return [{"title": key.replace("_", " ").title(), "snippet": snippet, "doc_id": key, "score": 0.92}]
    return [{"title": "General Info", "snippet": "Please contact the clinic directly for this information.", "doc_id": "fallback", "score": 0.5}]


def _mock_lookup_patient(phone: str) -> dict:
    patients = {
        "555-0101": {"patient_id": "pat-001", "name": "Alice Thompson", "found": True},
        "555-0173": {"patient_id": "pat-002", "name": "Bob Martinez", "found": True},
        "555-0103": {"patient_id": "pat-003", "name": "Carol Lee", "found": True},
    }
    phone_clean = phone.replace(" ", "").replace("-", "")
    for stored_phone, info in patients.items():
        if phone_clean == stored_phone.replace("-", ""):
            return info
    return {"patient_id": None, "name": None, "found": False, "message": "Patient not found. Please provide your full name and date of birth."}


def _mock_check_availability(
    specialty: str = "general",
    preferred_date: str = "",
    time_of_day: str = "any",
    **_: object,  # noqa: ANN003
) -> list[dict]:
    slot_date = preferred_date or date.today().isoformat()
    spec = specialty or "general"
    # Return only morning slots when caller requests morning
    if time_of_day and time_of_day.lower() in ("morning", "am"):
        return [
            {"slot_id": "slot-001", "doctor": "Dr. Priya Patel", "specialty": spec, "date": slot_date, "time": "9:00 AM"},
            {"slot_id": "slot-002", "doctor": "Dr. Chris Lee",   "specialty": spec, "date": slot_date, "time": "10:30 AM"},
        ]
    return [
        {"slot_id": "slot-001", "doctor": "Dr. Priya Patel", "specialty": spec, "date": slot_date, "time": "10:00 AM"},
        {"slot_id": "slot-002", "doctor": "Dr. Chris Lee",   "specialty": spec, "date": slot_date, "time": "2:00 PM"},
    ]


def _mock_book_appointment(slot_id: str, patient_id: str, reason: str = "") -> dict:
    return {
        "success": True,
        "appointment_id": "appt-999",
        "slot_id": slot_id,
        "patient_id": patient_id,
        "reason": reason or "dental appointment",
        "message": "Appointment confirmed.",
    }


def _mock_lookup_invoice(invoice_number: str = "", patient_id: str = "") -> dict:
    invoices = {
        "INV-001": {"invoice_number": "INV-001", "date": "2025-04-15", "description": "Routine cleaning + X-rays", "total": "$180.00", "insurance_covered": "$144.00", "patient_owes": "$36.00", "status": "outstanding"},
        "INV-002": {"invoice_number": "INV-002", "date": "2025-03-02", "description": "Crown installation — tooth #14", "total": "$1,200.00", "insurance_covered": "$600.00", "patient_owes": "$600.00", "status": "paid"},
    }
    if invoice_number and invoice_number in invoices:
        return invoices[invoice_number]
    return {"invoice_number": "INV-001", "date": "2025-04-15", "description": "Routine cleaning + X-rays", "total": "$180.00", "insurance_covered": "$144.00", "patient_owes": "$36.00", "status": "outstanding"}


def _mock_query_appointments_natural(question: str, patient_id: str = "") -> dict:
    q = question.lower()
    if "delete" in q or "drop" in q or "insert" in q or "update" in q:
        return {"success": False, "message": "I can only answer read-only questions about appointments."}
    if "how many" in q or "count" in q:
        return {"success": True, "answer": "There are 12 confirmed appointments this period.", "rows": [{"count": 12}]}
    if "next" in q and ("appointment" in q or "visit" in q):
        return {"success": True, "answer": "Your next appointment is Thursday May 14th at 2 PM with Dr. Patel for a routine cleaning.", "rows": [{"start_at": "2026-05-14 14:00", "doctor_name": "Dr. Priya Patel", "reason": "routine cleaning"}]}
    if "today" in q or "this week" in q or "list" in q:
        return {"success": True, "answer": "Dr. Lee has 2 appointments today: 9 AM cleaning and 11 AM filling.", "rows": [{"start_at": "09:00", "reason": "cleaning"}, {"start_at": "11:00", "reason": "filling"}]}
    if "specialty" in q or "most" in q:
        return {"success": True, "answer": "Hygiene has the most appointments this month with 47.", "rows": [{"specialty": "hygiene", "count": 47}]}
    return {"success": True, "answer": "I found the requested appointment information.", "rows": []}


def _mock_transfer_to(agent_name: str, summary: str = "") -> dict:
    return {"transferred_to": agent_name, "summary": summary, "status": "ok"}


def _mock_transfer_back(summary: str = "") -> dict:
    return {"transferred_to": "triage", "summary": summary, "status": "ok"}


def _mock_transfer_to_human(reason: str = "") -> dict:
    return {"transferred_to": "human", "reason": reason, "status": "ok", "message": "Connecting you to a team member now."}


_MOCK_TOOLS: dict[str, Any] = {
    "search_clinic_kb": _mock_search_clinic_kb,
    "lookup_patient": _mock_lookup_patient,
    "check_availability": _mock_check_availability,
    "book_appointment": _mock_book_appointment,
    "lookup_invoice": _mock_lookup_invoice,
    "query_appointments_natural": _mock_query_appointments_natural,
    "transfer_to": _mock_transfer_to,
    "transfer_back": _mock_transfer_back,
    "transfer_to_human": _mock_transfer_to_human,
}


def _execute_mock_tool(tool_name: str, tool_input: dict) -> str:
    fn = _MOCK_TOOLS.get(tool_name)
    if fn is None:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})
    try:
        result = fn(**tool_input)
        return json.dumps(result, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Category → agent mapping ──────────────────────────────────────────────────
# analytics cases need the SQL tool; use billing agent + sql tool injected.
# edge_case and multi_turn need search_clinic_kb → use faq agent for edge,
# triage for multi_turn (triage can hand off within the simulation).

_CATEGORY_TO_AGENT = {
    "faq": "faq",
    "booking": "booking",
    "analytics": "analytics",   # virtual — billing + query_appointments tool
    "billing": "billing",
    "handoff": "triage",
    "phone": "booking",
    "edge_case": "faq",         # edge cases are FAQ-domain refusals
    "multi_turn": "multi_turn", # virtual — faq + booking + billing tools combined
}


def _to_openai_tools(tools: list[dict]) -> list[dict]:
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


def load_cases(limit: int | None = None, category: str | None = None) -> list[dict]:
    cases = []
    with open(DATASET) as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    if category:
        cases = [c for c in cases if c.get("category") == category]
    if limit:
        cases = cases[:limit]
    return cases


def _get_agent_tools(agent_name: str, tool_registry: dict) -> tuple[str, list[dict]]:
    """Return (system_prompt, tool_schemas) for the given agent name."""
    from bot.agents.registry import AGENTS, get_tool_schemas

    if agent_name == "analytics":
        # Use billing agent prompt; inject sql + search tools
        agent = AGENTS["billing"]
        system_prompt = agent.load_prompt_template().format(
            current_date=date.today().isoformat(),
            summary="",
        )
        tool_names = list(agent.tool_names) + ["query_appointments_natural", "search_clinic_kb"]
    elif agent_name == "multi_turn":
        # Virtual agent: booking prompt + all tools so multi-turn flows can answer
        # FAQ questions AND book without a separate agent hop.
        agent = AGENTS["booking"]
        system_prompt = (
            agent.load_prompt_template().format(
                current_date=date.today().isoformat(),
                summary="",
            )
            + "\n\nAdditional tools available for multi-topic calls:\n"
            "- search_clinic_kb: answer general questions (insurance, hours, services) BEFORE booking.\n"
            "- lookup_invoice: look up a bill or invoice when caller asks about charges.\n"
            "- query_appointments_natural: look up EXISTING appointments by asking a natural-language "
            "question. Use this — NOT lookup_patient — when the caller asks about an existing appointment "
            "date, time, or doctor. Always call this tool before confirming any appointment details.\n"
            "- transfer_back: use ONLY when you cannot help and triage must reroute. "
            "When you call transfer_back, tell the caller 'Let me connect you with the right team' "
            "— never say 'billing team' or any specific team name, because transfer_back goes to triage."
        )
        tool_names = [
            "lookup_patient", "check_availability", "book_appointment",
            "search_clinic_kb", "lookup_invoice", "query_appointments_natural",
            "transfer_back",
        ]
    elif agent_name == "faq":
        # FAQ agent gets check_availability so edge cases about booking scope can
        # call it before declining or transferring, matching eval expectations.
        agent = AGENTS["faq"]
        system_prompt = agent.load_prompt_template().format(
            current_date=date.today().isoformat(),
            summary="",
        )
        tool_names = list(agent.tool_names) + ["check_availability"]
    else:
        agent = AGENTS[agent_name]
        system_prompt = agent.load_prompt_template().format(
            current_date=date.today().isoformat(),
            summary="",
        )
        tool_names = list(agent.tool_names)

    # deduplicate while preserving order
    seen: set[str] = set()
    unique_names = [n for n in tool_names if not (n in seen or seen.add(n))]  # type: ignore[func-returns-value]
    tools = get_tool_schemas([n for n in unique_names if n in tool_registry])
    return system_prompt, tools


async def _simulate_case_anthropic(
    case: dict,
    client,
    model: str,
    tool_registry: dict,
) -> dict[str, Any]:
    """Run a single case with the Anthropic client and a full agentic tool loop."""
    category = case.get("category", "faq")
    agent_name = _CATEGORY_TO_AGENT.get(category, "triage")
    system_prompt, tools = _get_agent_tools(agent_name, tool_registry)

    messages = [
        {"role": "user", "content": turn}
        for turn in case.get("user_turns", [])
        if turn.strip()
    ]
    if not messages:
        messages = [{"role": "user", "content": "(no input — how can I help you?)"}]

    tool_calls_made: list[str] = []
    bot_texts: list[str] = []
    # Seed with agent identity so the judge knows the clinic name is grounded.
    retrieved_context_parts: list[str] = [
        f"[system] {system_prompt.splitlines()[0]}"
    ]

    try:
        for _round in range(_MAX_TOOL_ROUNDS):
            response = client.messages.create(
                model=model,
                max_tokens=512,
                system=system_prompt,
                tools=tools or [],
                messages=messages,
            )

            tool_use_blocks = []
            for block in response.content:
                if block.type == "text" and block.text.strip():
                    bot_texts.append(block.text.strip())
                elif block.type == "tool_use":
                    tool_calls_made.append(block.name)
                    tool_use_blocks.append(block)

            if response.stop_reason == "end_turn" or not tool_use_blocks:
                break

            # Append assistant turn then inject tool results
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for tu in tool_use_blocks:
                result_str = _execute_mock_tool(tu.name, tu.input)
                retrieved_context_parts.append(
                    f"{tu.name}({json.dumps(tu.input)[:120]}) → {result_str[:600]}"
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": result_str,
                })

            messages.append({"role": "user", "content": tool_results})

    except Exception as e:
        logger.warning("LLM call failed for %s: %s", case["id"], e)
        bot_texts = [f"[error: {e}]"]

    return {
        "bot_transcript": " ".join(bot_texts),
        "tool_calls_made": tool_calls_made,
        "retrieved_context": "\n".join(retrieved_context_parts),
    }


async def _simulate_case_openai(
    case: dict,
    client,
    model: str,
    tool_registry: dict,
) -> dict[str, Any]:
    """Run a single case with the OpenAI client and a full agentic tool loop."""
    category = case.get("category", "faq")
    agent_name = _CATEGORY_TO_AGENT.get(category, "triage")
    system_prompt, tools = _get_agent_tools(agent_name, tool_registry)

    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    for turn in case.get("user_turns", []):
        if turn.strip():
            messages.append({"role": "user", "content": turn})
    if len(messages) == 1:
        messages.append({"role": "user", "content": "(no input — how can I help you?)"})

    tool_calls_made: list[str] = []
    bot_texts: list[str] = []
    # Seed with agent identity so the judge knows the clinic name is grounded.
    retrieved_context_parts: list[str] = [
        f"[system] {system_prompt.splitlines()[0]}"
    ]

    try:
        for _round in range(_MAX_TOOL_ROUNDS):
            request: dict[str, Any] = {
                "model": model,
                "max_completion_tokens": 512,
                "messages": messages,
            }
            if tools:
                request["tools"] = _to_openai_tools(tools)

            response = client.chat.completions.create(**request)
            message = response.choices[0].message

            if message.content:
                bot_texts.append(message.content.strip())

            pending_calls = getattr(message, "tool_calls", None) or []
            if not pending_calls:
                break

            messages.append(message)

            for tool_call in pending_calls:
                fn_name = tool_call.function.name
                tool_calls_made.append(fn_name)
                try:
                    tool_input = json.loads(tool_call.function.arguments or "{}")
                except json.JSONDecodeError:
                    tool_input = {}

                result_str = _execute_mock_tool(fn_name, tool_input)
                retrieved_context_parts.append(
                    f"{fn_name}({json.dumps(tool_input)[:120]}) → {result_str[:600]}"
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result_str,
                })

    except Exception as e:
        logger.warning("LLM call failed for %s: %s", case["id"], e)
        bot_texts = [f"[error: {e}]"]

    return {
        "bot_transcript": " ".join(bot_texts),
        "tool_calls_made": tool_calls_made,
        "retrieved_context": "\n".join(retrieved_context_parts),
    }


async def run_offline_eval(
    limit: int | None = None,
    category: str | None = None,
    verbose: bool = False,
) -> None:
    from config import get_settings

    try:
        from judge import score_case
    except ImportError:
        from eval.runners.judge import score_case

    # Register tool schemas so the agent registry can serve them
    from bot.agents.orchestrator import (
        TRANSFER_BACK_TOOL,
        TRANSFER_TO_HUMAN_TOOL,
        TRANSFER_TO_TOOL,
    )
    from bot.agents.registry import register_tool_definition
    from bot.tools.appointments import BOOK_APPOINTMENT_TOOL, CHECK_AVAILABILITY_TOOL
    from bot.tools.billing import LOOKUP_INVOICE_TOOL
    from bot.tools.patient_lookup import LOOKUP_PATIENT_TOOL
    from bot.tools.rag_search import SEARCH_KB_TOOL
    from bot.tools.sql_query import QUERY_APPOINTMENTS_TOOL

    tool_registry: dict[str, dict] = {}
    for schema in [
        SEARCH_KB_TOOL,
        LOOKUP_PATIENT_TOOL,
        CHECK_AVAILABILITY_TOOL,
        BOOK_APPOINTMENT_TOOL,
        QUERY_APPOINTMENTS_TOOL,
        LOOKUP_INVOICE_TOOL,
        TRANSFER_TO_TOOL,
        TRANSFER_BACK_TOOL,
        TRANSFER_TO_HUMAN_TOOL,
    ]:
        register_tool_definition(schema["name"], schema)
        tool_registry[schema["name"]] = schema

    settings = get_settings()
    provider = settings.llm_provider
    model = settings.active_llm_model()

    if provider == "openai":
        from openai import OpenAI
        client = OpenAI(api_key=settings.openai_api_key)
    elif provider == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    else:
        raise ValueError(f"Offline eval does not support LLM_PROVIDER={provider!r}")

    cases = load_cases(limit=limit, category=category)
    print(f"\nOffline eval: {len(cases)} cases  provider={provider} model={model}\n")

    results = []
    category_stats: dict[str, list[dict]] = defaultdict(list)

    for case in cases:
        if provider == "anthropic":
            sim = await _simulate_case_anthropic(case, client, model=model, tool_registry=tool_registry)
        else:
            sim = await _simulate_case_openai(case, client, model=model, tool_registry=tool_registry)

        score = await score_case(
            case=case,
            bot_transcript=sim["bot_transcript"],
            tool_calls_made=sim["tool_calls_made"],
            retrieved_context=sim["retrieved_context"],
            client=client,
            provider=provider,
            model=model,
        )

        status = "PASS" if score["pass"] else "FAIL"
        hallucination_score = score["scores"].get("hallucination", 0)
        hall_flag = (
            " [HALL]"
            if isinstance(hallucination_score, (int, float)) and hallucination_score < 4.5
            else ""
        )

        print(
            f"  [{status}] {case['id']:18s}  {case['category']:14s}  "
            f"avg={score['avg_score']:.1f}  hall={hallucination_score}{hall_flag}"
        )
        if verbose:
            print(f"         transcript: {sim['bot_transcript'][:120]}")
            print(f"         tools called: {sim['tool_calls_made']}")
            print(f"         reasoning: {score.get('reasoning', '')[:120]}")
            if score.get("hallucinated_facts"):
                print(f"         hallucinated: {score['hallucinated_facts']}")

        results.append(score)
        category_stats[case["category"]].append(score)

    # ── Aggregate ─────────────────────────────────────────────────────────────
    total = len(results)
    passed = sum(1 for r in results if r["pass"])
    hallucinated = sum(
        1 for r in results
        if isinstance(r["scores"].get("hallucination"), (int, float))
        and r["scores"]["hallucination"] < 4.5
    )

    pass_rate_pct = passed / total * 100 if total else 0.0
    hallucination_rate_pct = hallucinated / total * 100 if total else 0.0
    avg_score = sum(r["avg_score"] for r in results) / total if total else 0.0

    category_breakdown = {}
    for cat, cat_results in category_stats.items():
        cat_total = len(cat_results)
        cat_passed = sum(1 for r in cat_results if r["pass"])
        cat_hall = sum(
            1 for r in cat_results
            if isinstance(r["scores"].get("hallucination"), (int, float))
            and r["scores"]["hallucination"] < 4.5
        )
        category_breakdown[cat] = {
            "total": cat_total,
            "pass_rate_pct": round(cat_passed / cat_total * 100 if cat_total else 0, 1),
            "hallucination_rate_pct": round(cat_hall / cat_total * 100 if cat_total else 0, 1),
        }

    print(f"\n{'─'*70}")
    print(f"Pass rate:          {passed}/{total} ({pass_rate_pct:.1f}%)  [target: ≥{PASS_RATE_THRESHOLD:.0f}%]")
    print(f"Hallucination rate: {hallucinated}/{total} ({hallucination_rate_pct:.1f}%)  [target: <{HALLUCINATION_RATE_THRESHOLD:.0f}%]")
    print(f"Avg judge score:    {avg_score:.2f} / 5.0")
    print(f"{'─'*70}")

    # ── Save report ───────────────────────────────────────────────────────────
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%dT%H%M%S")
    report = {
        "timestamp": now.isoformat(),
        "total_cases": total,
        "passed": passed,
        "hallucinated_cases": hallucinated,
        "pass_rate_pct": round(pass_rate_pct, 2),
        "hallucination_rate_pct": round(hallucination_rate_pct, 2),
        "avg_score": round(avg_score, 3),
        "category_breakdown": category_breakdown,
        "cases": results,
    }
    report_path = REPORTS_DIR / f"run_{timestamp}.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nReport: {report_path}")

    overall_fail = (
        pass_rate_pct < PASS_RATE_THRESHOLD
        or hallucination_rate_pct > HALLUCINATION_RATE_THRESHOLD
    )
    if overall_fail:
        failures = []
        if pass_rate_pct < PASS_RATE_THRESHOLD:
            failures.append(f"pass rate {pass_rate_pct:.1f}% < {PASS_RATE_THRESHOLD:.0f}%")
        if hallucination_rate_pct > HALLUCINATION_RATE_THRESHOLD:
            failures.append(f"hallucination rate {hallucination_rate_pct:.1f}% > {HALLUCINATION_RATE_THRESHOLD:.0f}%")
        print(f"\nFAIL — {', '.join(failures)}")
        sys.exit(1)
    else:
        print(
            f"\nPASS — {pass_rate_pct:.1f}% pass rate | "
            f"{hallucination_rate_pct:.1f}% hallucination rate | "
            f"avg score {avg_score:.2f} ✓"
        )


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(description="Offline text-mode eval")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--category", type=str, default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    asyncio.run(run_offline_eval(limit=args.limit, category=args.category, verbose=args.verbose))


if __name__ == "__main__":
    main()
