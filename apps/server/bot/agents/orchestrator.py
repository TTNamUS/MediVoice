"""Multi-agent orchestrator for MediVoice.

Manages agent state (current_agent, running_summary) and implements
transfer_to / transfer_back by hot-swapping the LLM context messages
and tool list without restarting the pipeline.

Design notes:
- The pipeline stays up; only the system prompt and tool list change.
- Each handoff injects a synthetic assistant turn so the caller hears a
  brief transition phrase (also rendered via TTS naturally).
- Summary is capped at ~150 words to prevent context bloat.
- OTel spans record every handoff with from/to/summary_len/reason.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

from loguru import logger
from pipecat.processors.aggregators.llm_context import LLMContext

from bot.agents.registry import AGENTS, AgentDefinition, get_tools_schema
from bot.observability.metrics import agent_turns
from bot.observability.otel_setup import get_tracer

tracer = get_tracer("medivoice.orchestrator")

_SUMMARY_MAX_WORDS = 150
_TRANSITION_PHRASES: dict[str, str] = {
    "booking": "Let me connect you with our booking specialist.",
    "faq": "I'll connect you with someone who can answer that.",
    "billing": "Let me transfer you to our billing team.",
    "human": "I'm transferring you to a team member right away.",
    "triage": "Let me route your call properly.",
}


def _trim_summary(summary: str) -> str:
    words = summary.split()
    if len(words) > _SUMMARY_MAX_WORDS:
        return " ".join(words[:_SUMMARY_MAX_WORDS]) + "…"
    return summary


class AgentOrchestrator:
    """Owns agent state for a single call session."""

    def __init__(
        self,
        llm_service: Any,
        context: LLMContext,
        session_id: str = "default",
        langfuse_tracker: Any | None = None,
    ) -> None:
        self._llm = llm_service
        self._context = context
        self._session_id = session_id
        self._current_agent: str = "triage"
        self._running_summary: str = ""
        self._langfuse = langfuse_tracker
        # Callback invoked after a handoff so the pipeline can push a
        # synthetic TTS frame. Set by pipeline.py after construction.
        self._on_handoff: Any | None = None

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def current_agent(self) -> str:
        return self._current_agent

    @property
    def running_summary(self) -> str:
        return self._running_summary

    def set_handoff_callback(self, callback: Any) -> None:
        self._on_handoff = callback

    async def transfer_to(self, agent_name: str, summary: str = "") -> dict:
        """Tool handler: switch to a specialist agent."""
        return await self._do_transfer(
            target=agent_name,
            summary=summary,
            reason="transfer_to",
        )

    async def transfer_back(self, summary: str = "") -> dict:
        """Tool handler: return to triage from a specialist."""
        return await self._do_transfer(
            target="triage",
            summary=summary,
            reason="transfer_back",
        )

    async def transfer_to_human(self, reason: str = "") -> dict:
        """Tool handler: escalate to live agent (stub)."""
        with tracer.start_as_current_span("agent.handoff") as span:
            span.set_attribute("from", self._current_agent)
            span.set_attribute("to", "human")
            span.set_attribute("reason", "transfer_to_human")
            span.set_attribute("session.id", self._session_id)
        logger.info(
            "session=%s handoff %s → human (reason=%s)",
            self._session_id,
            self._current_agent,
            reason,
        )
        return {
            "transferred": True,
            "to": "human",
            "message": "Transferring you to a team member now. Please hold.",
        }

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _do_transfer(self, target: str, summary: str, reason: str) -> dict:
        from_agent = self._current_agent

        with tracer.start_as_current_span("agent.handoff") as span:
            span.set_attribute("from", from_agent)
            span.set_attribute("to", target)
            span.set_attribute("reason", reason)
            span.set_attribute("summary_len", len(summary.split()))
            span.set_attribute("session.id", self._session_id)

            t0 = time.perf_counter()

            if target not in AGENTS and target != "human":
                logger.warning("Unknown agent '%s', staying on triage", target)
                target = "triage"

            # Update summary
            if summary:
                self._running_summary = _trim_summary(
                    (self._running_summary + " " + summary).strip()
                )

            # Swap agent
            self._current_agent = target
            agent_def = AGENTS.get(target)
            if agent_def:
                self._apply_agent(agent_def)

            latency_ms = (time.perf_counter() - t0) * 1000
            span.set_attribute("latency_ms", round(latency_ms, 1))
            agent_turns.labels(agent=target).inc()

        if self._langfuse:
            self._langfuse.log_handoff(from_agent=from_agent, to_agent=target, reason=reason)

        logger.info(
            "session=%s handoff %s → %s summary=%d words",
            self._session_id,
            from_agent,
            target,
            len(self._running_summary.split()),
        )

        phrase = _TRANSITION_PHRASES.get(target, "One moment.")
        # Embed agent marker for the UI badge (stripped from TTS by the pipeline)
        marker = f"[[AGENT:{target}]]"
        return {
            "transferred": True,
            "to": target,
            "message": f"{marker} {phrase}",
        }

    def _apply_agent(self, agent_def: AgentDefinition) -> None:
        """Hot-swap system prompt and tool list on the active LLM context."""
        system_prompt = agent_def.load_prompt_template().format(
            current_date=date.today().isoformat(),
            summary=self._running_summary or "No prior context.",
        )

        msgs = self._context.messages
        if msgs and msgs[0].get("role") == "system":
            msgs[0]["content"] = system_prompt
        else:
            msgs.insert(0, {"role": "system", "content": system_prompt})

        self._context.set_messages(_trim_context(msgs))
        self._context.set_tools(get_tools_schema(agent_def.tool_names))


def _trim_context(messages: list[dict]) -> list[dict]:
    """Keep system message + last N non-system turns to cap context size."""
    _MAX_TURNS = 6  # 3 user + 3 assistant turns kept after handoff
    system = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]
    return system + non_system[-_MAX_TURNS:]


# ── Handoff tool schemas ──────────────────────────────────────────────────────

TRANSFER_TO_TOOL = {
    "name": "transfer_to",
    "description": (
        "Transfer the caller to a specialist agent. Call this as soon as the caller's "
        "intent is clear. Agents: 'booking' (appointments), 'faq' (clinic info), "
        "'billing' (invoices/payments), 'human' (live agent escalation)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "agent_name": {
                "type": "string",
                "enum": ["booking", "faq", "billing", "human"],
                "description": "Target agent name",
            },
            "summary": {
                "type": "string",
                "description": "One sentence: who the caller is and what they need.",
            },
        },
        "required": ["agent_name", "summary"],
    },
}

TRANSFER_BACK_TOOL = {
    "name": "transfer_back",
    "description": (
        "Return to the triage agent when the caller's intent has shifted outside "
        "your specialty, or when the current task is complete and the caller has "
        "a new request."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "One sentence summarizing what was handled and the new intent.",
            },
        },
        "required": ["summary"],
    },
}

TRANSFER_TO_HUMAN_TOOL = {
    "name": "transfer_to_human",
    "description": "Escalate the call to a live human agent.",
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Brief reason for escalation.",
            },
        },
        "required": [],
    },
}
