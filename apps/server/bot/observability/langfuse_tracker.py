"""Per-session Langfuse trace manager — Langfuse v4 API.

v4 uses OTel context propagation. The correct pattern:

    async with tracker.session_context():
        # all observations inside here are correctly parented
        tracker.log_llm_generation(...)
        tracker.log_handoff(...)

The pipeline wraps the bot task in session_context() so every LLM turn
and handoff is a child of the root session span with session_id attached.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from bot.observability.langfuse_setup import get_langfuse

logger = logging.getLogger(__name__)


class LangfuseSessionTracker:
    """Owns the Langfuse trace for a single voice session."""

    def __init__(
        self,
        session_id: str,
        initial_agent: str = "triage",
        llm_provider: str = "anthropic",
        llm_model: str = "unknown",
    ) -> None:
        self._session_id = session_id
        self._current_agent = initial_agent
        self._llm_model = llm_model
        self._llm_provider = llm_provider
        # Active root span — set inside session_context(), None outside
        self._root_span: Any = None

    @asynccontextmanager
    async def session_context(self) -> AsyncIterator[None]:
        """Async context manager that wraps the entire voice call.

        Enter this context before the pipeline runs so that all
        log_llm_generation / log_handoff calls happen inside it.
        """
        lf = get_langfuse()
        if not lf:
            yield
            return

        from langfuse import propagate_attributes

        try:
            # 1. Open the root session span
            with lf.start_as_current_observation(
                name="voice-session",
                as_type="agent",
                metadata={
                    "session_id": self._session_id,
                    "initial_agent": self._current_agent,
                    "llm_provider": self._llm_provider,
                    "llm_model": self._llm_model,
                },
            ) as root_span:
                self._root_span = root_span
                # 2. Propagate session_id to all child spans
                with propagate_attributes(
                    session_id=self._session_id,
                    trace_name="voice-session",
                ):
                    logger.debug("Langfuse session started session_id=%s", self._session_id)
                    yield
        except Exception as e:
            logger.debug("Langfuse session_context error: %s", e)
            yield
        finally:
            self._root_span = None

    # ── Called by ConversationLogger — actual prompt + completion text ───────────

    def log_turn(self, *, user_text: str, assistant_text: str) -> None:
        """Log one full conversation turn with input/output text to Langfuse."""
        lf = get_langfuse()
        if not lf or not self._root_span:
            return
        try:
            with self._root_span.start_as_current_observation(
                name=f"turn ({self._current_agent})",
                as_type="generation",
                model=self._llm_model,
                input=[
                    {"role": "user", "content": user_text},
                ],
                output=assistant_text,
                metadata={"agent": self._current_agent},
            ):
                pass
        except Exception as e:
            logger.debug("Langfuse log_turn failed: %s", e)

    # ── Called by MetricsBridge — token counts + latency ─────────────────────

    def log_llm_generation(
        self,
        *,
        tokens_in: int = 0,
        tokens_out: int = 0,
        ttfb_ms: float | None = None,
        processing_ms: float | None = None,
    ) -> None:
        lf = get_langfuse()
        if not lf or not self._root_span:
            return
        try:
            metadata: dict[str, Any] = {"agent": self._current_agent}
            if ttfb_ms is not None:
                metadata["ttfb_ms"] = ttfb_ms
            if processing_ms is not None:
                metadata["processing_ms"] = processing_ms

            with self._root_span.start_as_current_observation(
                name=f"llm-turn ({self._current_agent})",
                as_type="generation",
                model=self._llm_model,
                usage_details={"input": tokens_in, "output": tokens_out},
                metadata=metadata,
            ):
                pass
        except Exception as e:
            logger.debug("Langfuse generation log failed: %s", e)

    # ── Called by AgentOrchestrator on every agent transfer ───────────────────

    def log_handoff(self, from_agent: str, to_agent: str, reason: str) -> None:
        lf = get_langfuse()
        if not lf or not self._root_span:
            return
        try:
            self._root_span.create_event(
                name="agent-handoff",
                metadata={"from": from_agent, "to": to_agent, "reason": reason},
            )
            self._current_agent = to_agent
        except Exception as e:
            logger.debug("Langfuse handoff log failed: %s", e)

    def update_agent(self, agent: str) -> None:
        self._current_agent = agent

    # ── Called by ConversationLogger — tool invocations ───────────────────────

    def log_tool_start(
        self,
        *,
        function_name: str,
        arguments: dict,
        tool_call_id: str | None = None,
    ) -> None:
        lf = get_langfuse()
        if not lf or not self._root_span:
            return
        try:
            meta: dict = {
                "agent": self._current_agent,
                "function_name": function_name,
                "arguments": arguments,
            }
            if tool_call_id:
                meta["tool_call_id"] = tool_call_id
            self._root_span.create_event(name="tool-start", metadata=meta)
        except Exception as e:
            logger.debug("Langfuse log_tool_start failed: %s", e)

    def log_tool_result(
        self,
        *,
        function_name: str,
        result: object,
        tool_call_id: str | None = None,
    ) -> None:
        lf = get_langfuse()
        if not lf or not self._root_span:
            return
        try:
            meta: dict = {
                "agent": self._current_agent,
                "function_name": function_name,
                "result": str(result)[:2000],  # cap large results
            }
            if tool_call_id:
                meta["tool_call_id"] = tool_call_id
            self._root_span.create_event(name="tool-result", metadata=meta)
        except Exception as e:
            logger.debug("Langfuse log_tool_result failed: %s", e)

    # ── Called by ConversationLogger — Claude extended thinking ───────────────

    def log_thinking(self, text: str) -> None:
        lf = get_langfuse()
        if not lf or not self._root_span:
            return
        try:
            with self._root_span.start_as_current_observation(
                name=f"thinking ({self._current_agent})",
                as_type="generation",
                model=self._llm_model,
                input=[{"role": "system", "content": "<thinking>"}],
                output=text,
                metadata={"agent": self._current_agent, "type": "internal_reasoning"},
            ):
                pass
        except Exception as e:
            logger.debug("Langfuse log_thinking failed: %s", e)

    # ── Called by ConversationLogger — generic pipeline events ───────────────

    def log_event(self, event_name: str, metadata: dict | None = None) -> None:
        lf = get_langfuse()
        if not lf or not self._root_span:
            return
        try:
            meta = {"agent": self._current_agent, **(metadata or {})}
            self._root_span.create_event(name=event_name, metadata=meta)
        except Exception as e:
            logger.debug("Langfuse log_event(%s) failed: %s", event_name, e)
