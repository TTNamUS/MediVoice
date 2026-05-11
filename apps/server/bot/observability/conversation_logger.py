"""Pipecat processor that captures conversation events and logs them to Langfuse.

Sits in the pipeline after the assistant aggregator, intercepting:
  - TranscriptionFrame          → user's spoken words (STT output)
  - LLMTextFrame                → AI response text chunks (accumulated per turn)
  - LLMFullResponseEndFrame     → AI turn complete; flush text to Langfuse
  - LLMThoughtTextFrame         → Claude internal reasoning (accumulated)
  - LLMThoughtEndFrame          → flush accumulated thinking block
  - FunctionCallsStartedFrame   → tool invocation started (name + args)
  - FunctionCallResultFrame     → tool completed (name + result)
  - VADUserStartedSpeakingFrame → caller started talking (VAD event)
  - VADUserStoppedSpeakingFrame → caller stopped talking
  - InterruptionFrame           → caller barged in while bot was speaking
  - BotStartedSpeakingFrame     → bot TTS began
  - BotStoppedSpeakingFrame     → bot TTS finished
  - ErrorFrame                  → pipeline error (message + fatal flag)

Token counts and latency come from MetricsBridge (MetricsFrame) separately.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMTextFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger(__name__)

# Optional imports — not all Pipecat builds expose these
try:
    from pipecat.frames.frames import (
        LLMThoughtEndFrame,
        LLMThoughtTextFrame,
    )

    _HAS_THOUGHT_FRAMES = True
except ImportError:
    _HAS_THOUGHT_FRAMES = False

try:
    from pipecat.frames.frames import (
        FunctionCallResultFrame,
        FunctionCallsStartedFrame,
    )

    _HAS_FUNCTION_FRAMES = True
except ImportError:
    _HAS_FUNCTION_FRAMES = False

try:
    from pipecat.frames.frames import (
        VADUserStartedSpeakingFrame,
        VADUserStoppedSpeakingFrame,
    )

    _HAS_VAD_FRAMES = True
except ImportError:
    _HAS_VAD_FRAMES = False


class ConversationLogger(FrameProcessor):
    """Pass-through processor that logs conversation events to Langfuse."""

    def __init__(self, langfuse_tracker: Any) -> None:
        super().__init__()
        self._tracker = langfuse_tracker
        self._pending_user_text: str = ""
        self._pending_assistant_chunks: list[str] = []
        self._pending_thought_chunks: list[str] = []
        self._bot_speech_start: float | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        # ── User speech ─────────────────────────────────────────────────────────
        if isinstance(frame, TranscriptionFrame) and frame.finalized:
            self._pending_user_text = frame.text
            logger.debug("ConversationLogger: user said %r", frame.text[:80])

        # ── AI response text chunks ─────────────────────────────────────────────
        elif isinstance(frame, LLMTextFrame):
            self._pending_assistant_chunks.append(frame.text)

        # ── AI turn complete ────────────────────────────────────────────────────
        elif isinstance(frame, LLMFullResponseEndFrame):
            assistant_text = "".join(self._pending_assistant_chunks).strip()
            if assistant_text:
                self._tracker.log_turn(
                    user_text=self._pending_user_text,
                    assistant_text=assistant_text,
                )
            self._pending_user_text = ""
            self._pending_assistant_chunks = []

        # ── Claude thinking/reasoning blocks ────────────────────────────────────
        elif _HAS_THOUGHT_FRAMES and isinstance(frame, LLMThoughtTextFrame):
            self._pending_thought_chunks.append(frame.text)

        elif _HAS_THOUGHT_FRAMES and isinstance(frame, LLMThoughtEndFrame):
            thought_text = "".join(self._pending_thought_chunks).strip()
            if thought_text:
                self._tracker.log_thinking(thought_text)
            self._pending_thought_chunks = []

        # ── Tool calls ──────────────────────────────────────────────────────────
        elif _HAS_FUNCTION_FRAMES and isinstance(frame, FunctionCallsStartedFrame):
            for call in getattr(frame, "function_calls", []):
                name = getattr(call, "function_name", "unknown")
                args = getattr(call, "arguments", {})
                tool_call_id = getattr(call, "tool_call_id", None)
                logger.debug("ConversationLogger: tool start %s args=%s", name, args)
                self._tracker.log_tool_start(
                    function_name=name,
                    arguments=args,
                    tool_call_id=tool_call_id,
                )

        elif _HAS_FUNCTION_FRAMES and isinstance(frame, FunctionCallResultFrame):
            name = getattr(frame, "function_name", "unknown")
            result = getattr(frame, "result", None)
            tool_call_id = getattr(frame, "tool_call_id", None)
            logger.debug("ConversationLogger: tool result %s", name)
            self._tracker.log_tool_result(
                function_name=name,
                result=result,
                tool_call_id=tool_call_id,
            )

        # ── VAD events ──────────────────────────────────────────────────────────
        elif _HAS_VAD_FRAMES and isinstance(frame, VADUserStartedSpeakingFrame):
            self._tracker.log_event("vad_user_started_speaking")

        elif _HAS_VAD_FRAMES and isinstance(frame, VADUserStoppedSpeakingFrame):
            self._tracker.log_event("vad_user_stopped_speaking")

        # ── Interruptions ───────────────────────────────────────────────────────
        elif isinstance(frame, InterruptionFrame):
            self._tracker.log_event("interruption", metadata={"agent_was_speaking": True})

        # ── Bot speech timing ───────────────────────────────────────────────────
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speech_start = time.monotonic()
            self._tracker.log_event("bot_started_speaking")

        elif isinstance(frame, BotStoppedSpeakingFrame):
            duration_ms: float | None = None
            if self._bot_speech_start is not None:
                duration_ms = round((time.monotonic() - self._bot_speech_start) * 1000, 1)
                self._bot_speech_start = None
            self._tracker.log_event(
                "bot_stopped_speaking",
                metadata={"duration_ms": duration_ms} if duration_ms is not None else {},
            )

        # ── Pipeline errors ─────────────────────────────────────────────────────
        elif isinstance(frame, ErrorFrame):
            error_msg = getattr(frame, "error", str(frame))
            fatal = getattr(frame, "fatal", False)
            logger.warning("ConversationLogger: pipeline error fatal=%s: %s", fatal, error_msg)
            self._tracker.log_event(
                "pipeline_error",
                metadata={"error": error_msg, "fatal": fatal},
            )

        await self.push_frame(frame, direction)
