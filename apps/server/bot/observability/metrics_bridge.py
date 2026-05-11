"""Bridge Pipecat MetricsFrame events into OpenTelemetry spans + Prometheus + Langfuse.

Pipecat emits MetricsFrame with per-stage TTFB and token counts.
This processor intercepts those frames and records them as:
  - OTel spans  → visible in Jaeger (hierarchical STT → LLM → TTS per turn)
  - Prometheus counters/histograms → visible in Grafana dashboard
  - Langfuse generations → visible in Langfuse (prompt/token/cost analytics)

Usage:
    tracker = LangfuseSessionTracker(session_id, ...)   # optional, pass None to disable
    bridge = MetricsBridge(session_id="abc123", entrypoint="browser", langfuse_tracker=tracker)
    pipeline = Pipeline([..., bridge, ...])
"""

import uuid

from opentelemetry.trace import SpanKind
from pipecat.frames.frames import MetricsFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from bot.observability.metrics import (
    llm_tokens_in,
    llm_tokens_out,
    ttfb_histogram,
    tts_chars_out,
)
from bot.observability.otel_setup import get_tracer

tracer = get_tracer("medivoice.metrics")


class MetricsBridge(FrameProcessor):
    """Pass-through processor: MetricsFrame → OTel spans + Prometheus + Langfuse."""

    def __init__(
        self,
        session_id: str = "default",
        entrypoint: str = "browser",
        langfuse_tracker=None,
    ) -> None:
        super().__init__()
        self._session_id = session_id
        self._entrypoint = entrypoint
        self._langfuse = langfuse_tracker  # LangfuseSessionTracker | None

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, MetricsFrame):
            self._record_metrics(frame)

        await self.push_frame(frame, direction)

    def _record_metrics(self, frame: MetricsFrame) -> None:
        turn_id = str(uuid.uuid4())

        # Accumulate LLM data across metrics in this frame to log one Langfuse generation
        llm_tokens_in_count = 0
        llm_tokens_out_count = 0
        llm_ttfb_ms: float | None = None
        llm_processing_ms: float | None = None

        for metric in frame.data:
            processor = getattr(metric, "processor", "unknown")
            if "deepgram" in processor.lower() or "stt" in processor.lower():
                service, provider, model = "stt", "deepgram", "nova-3"
            elif (
                "anthropic" in processor.lower()
                or "openai" in processor.lower()
                or "google" in processor.lower()
                or "llm" in processor.lower()
            ):
                service, provider = "llm", processor.lower().split("llm")[0].strip()
                model = getattr(metric, "model", "unknown")
            elif "cartesia" in processor.lower() or "tts" in processor.lower():
                service, provider, model = "tts", "cartesia", "sonic-2"
            else:
                service, provider, model = "pipeline", processor, "unknown"

            # ── OTel span ────────────────────────────────────────────────────
            with tracer.start_as_current_span(f"turn.{service}", kind=SpanKind.INTERNAL) as span:
                span.set_attribute("turn.id", turn_id)
                span.set_attribute("session.id", self._session_id)
                span.set_attribute("service", service)
                span.set_attribute("provider", provider)
                span.set_attribute("model", model)

                if hasattr(metric, "ttfb") and metric.ttfb is not None:
                    span.set_attribute("ttfb_ms", round(metric.ttfb * 1000, 2))
                if hasattr(metric, "prompt_tokens") and metric.prompt_tokens:
                    span.set_attribute("tokens_in", metric.prompt_tokens)
                if hasattr(metric, "completion_tokens") and metric.completion_tokens:
                    span.set_attribute("tokens_out", metric.completion_tokens)
                if hasattr(metric, "characters") and metric.characters:
                    span.set_attribute("chars_out", metric.characters)
                if hasattr(metric, "processing_time") and metric.processing_time is not None:
                    span.set_attribute("processing_ms", round(metric.processing_time * 1000, 2))

            # ── Prometheus ───────────────────────────────────────────────────
            if hasattr(metric, "ttfb") and metric.ttfb is not None:
                ttfb_histogram.labels(
                    service=service,
                    provider=provider,
                    entrypoint=self._entrypoint,
                ).observe(metric.ttfb * 1000)

            if service == "llm":
                tokens_in = getattr(metric, "prompt_tokens", 0) or 0
                tokens_out = getattr(metric, "completion_tokens", 0) or 0
                if tokens_in:
                    llm_tokens_in.labels(provider=provider, model=model).inc(tokens_in)
                if tokens_out:
                    llm_tokens_out.labels(provider=provider, model=model).inc(tokens_out)
                # Accumulate for Langfuse
                llm_tokens_in_count += tokens_in
                llm_tokens_out_count += tokens_out
                if hasattr(metric, "ttfb") and metric.ttfb is not None:
                    llm_ttfb_ms = round(metric.ttfb * 1000, 2)
                if hasattr(metric, "processing_time") and metric.processing_time is not None:
                    llm_processing_ms = round(metric.processing_time * 1000, 2)

            if service == "tts":
                chars = getattr(metric, "characters", 0) or 0
                if chars:
                    tts_chars_out.labels(provider=provider).inc(chars)

        # ── Langfuse generation (once per frame, LLM only) ───────────────────
        if self._langfuse and llm_tokens_in_count > 0:
            self._langfuse.log_llm_generation(
                tokens_in=llm_tokens_in_count,
                tokens_out=llm_tokens_out_count,
                ttfb_ms=llm_ttfb_ms,
                processing_ms=llm_processing_ms,
            )
