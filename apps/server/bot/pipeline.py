"""Core Pipecat pipeline factory for MediVoice — multi-agent edition.

INVARIANT: this factory is the single source of truth for the voice pipeline.
Both LiveKit browser WebRTC and LiveKit SIP phone paths call it with the same
signature. A bug fix here fixes both transport paths simultaneously.

Usage:
    pipeline, task, orchestrator = await build_pipeline(transport, settings)
    await task.run()
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any, Literal

from config import Settings
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.base_transport import BaseTransport

from bot.agents.orchestrator import (
    TRANSFER_BACK_TOOL,
    TRANSFER_TO_HUMAN_TOOL,
    TRANSFER_TO_TOOL,
    AgentOrchestrator,
)
from bot.agents.registry import AGENTS, get_tools_schema, register_tool_definition
from bot.observability.conversation_logger import ConversationLogger
from bot.observability.langfuse_tracker import LangfuseSessionTracker
from bot.observability.metrics_bridge import MetricsBridge
from bot.observability.otel_setup import get_tracer
from bot.tools.appointments import (
    BOOK_APPOINTMENT_TOOL,
    CHECK_AVAILABILITY_TOOL,
    book_appointment,
    check_availability,
)
from bot.tools.billing import LOOKUP_INVOICE_TOOL, lookup_invoice
from bot.tools.patient_lookup import LOOKUP_PATIENT_TOOL, lookup_patient
from bot.tools.rag_search import SEARCH_KB_TOOL, search_clinic_kb
from bot.tools.sql_query import QUERY_APPOINTMENTS_TOOL, query_appointments_natural

logger = logging.getLogger(__name__)
tracer = get_tracer("medivoice.pipeline")


# Register all tool schemas so the registry can build per-agent tool lists
def _register_all_tools() -> None:
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


_register_all_tools()


def _as_pipecat_tool_handler(
    tool_func: Callable[..., Awaitable[Any]],
) -> Callable[[FunctionCallParams], Awaitable[None]]:
    """Adapt app tool functions to Pipecat's FunctionCallParams API."""

    async def _handler(params: FunctionCallParams) -> None:
        result = await tool_func(**dict(params.arguments))
        await params.result_callback(result)

    return _handler


def _build_llm(settings: Settings):
    """Return the appropriate LLM service based on LLM_PROVIDER."""
    provider = settings.llm_provider
    api_key = settings.active_llm_api_key()
    model = settings.active_llm_model()

    if provider == "anthropic":
        from pipecat.services.anthropic.llm import AnthropicLLMService

        return AnthropicLLMService(
            api_key=api_key,
            model=model,
            enable_prompt_caching_beta=True,
        )
    if provider == "openai":
        from pipecat.services.openai.llm import OpenAILLMService

        return OpenAILLMService(api_key=api_key, model=model)
    if provider == "gemini":
        from pipecat.services.google.llm import GoogleLLMService

        return GoogleLLMService(api_key=api_key, model=model)
    raise ValueError(f"Unknown LLM provider: {provider}")


def _triage_system_prompt() -> str:
    return (
        AGENTS["triage"]
        .load_prompt_template()
        .format(
            current_date=date.today().isoformat(),
        )
    )


async def build_pipeline(
    transport: BaseTransport,
    settings: Settings,
    session_id: str = "default",
    transport_type: Literal["livekit"] = "livekit",
    entrypoint: Literal["browser", "sip"] = "browser",
) -> tuple[Pipeline, PipelineTask, "AgentOrchestrator", "LangfuseSessionTracker"]:
    """Build the Pipecat pipeline with multi-agent orchestration.

    transport_type is used only for OTel tagging — pipeline logic is identical
    for LiveKit browser WebRTC and LiveKit SIP.

    Returns (pipeline, task, orchestrator).
    """
    with tracer.start_as_current_span("pipeline.build") as span:
        span.set_attribute("session.id", session_id)
        span.set_attribute("transport", transport_type)
        span.set_attribute("entrypoint", entrypoint)

        # ── STT ────────────────────────────────────────────────────────────────
        stt = DeepgramSTTService(
            api_key=settings.deepgram_api_key,
            model="nova-3",
            language="en-US",
        )

        # ── LLM — start as triage agent ────────────────────────────────────────
        system_prompt = _triage_system_prompt()
        context = LLMContext(
            messages=[{"role": "system", "content": system_prompt}],
            tools=get_tools_schema(AGENTS["triage"].tool_names),
        )

        user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
        )

        llm = _build_llm(settings)

        span.set_attribute("llm.provider", settings.llm_provider)
        span.set_attribute("llm.model", settings.active_llm_model())

        # ── Langfuse per-session tracker ───────────────────────────────────────
        langfuse_tracker = LangfuseSessionTracker(
            session_id=session_id,
            initial_agent="triage",
            llm_provider=settings.llm_provider,
            llm_model=settings.active_llm_model(),
        )

        # ── Orchestrator ───────────────────────────────────────────────────────
        orchestrator = AgentOrchestrator(
            llm,
            context,
            session_id=session_id,
            langfuse_tracker=langfuse_tracker,
        )

        # Wrap tool handlers that need orchestrator state
        async def _transfer_to(agent_name: str, summary: str = "") -> dict:
            return await orchestrator.transfer_to(agent_name, summary)

        async def _transfer_back(summary: str = "") -> dict:
            return await orchestrator.transfer_back(summary)

        async def _transfer_to_human(reason: str = "") -> dict:
            return await orchestrator.transfer_to_human(reason)

        # ── Register all tools on LLM ──────────────────────────────────────────
        # All tools registered so any agent can call them after a context swap
        llm.register_function(
            "search_clinic_kb",
            _as_pipecat_tool_handler(search_clinic_kb),
        )
        llm.register_function("lookup_patient", _as_pipecat_tool_handler(lookup_patient))
        llm.register_function(
            "check_availability",
            _as_pipecat_tool_handler(check_availability),
        )
        llm.register_function(
            "book_appointment",
            _as_pipecat_tool_handler(book_appointment),
        )
        llm.register_function(
            "query_appointments_natural",
            _as_pipecat_tool_handler(query_appointments_natural),
        )
        llm.register_function("lookup_invoice", _as_pipecat_tool_handler(lookup_invoice))
        llm.register_function("transfer_to", _as_pipecat_tool_handler(_transfer_to))
        llm.register_function("transfer_back", _as_pipecat_tool_handler(_transfer_back))
        llm.register_function(
            "transfer_to_human",
            _as_pipecat_tool_handler(_transfer_to_human),
        )

        # ── TTS ────────────────────────────────────────────────────────────────
        tts = CartesiaTTSService(
            api_key=settings.cartesia_api_key,
            voice_id=settings.cartesia_voice_id,
            model="sonic-2",
        )

        # ── OTel metrics bridge ────────────────────────────────────────────────
        metrics_bridge = MetricsBridge(
            session_id=session_id,
            entrypoint=entrypoint,
            langfuse_tracker=langfuse_tracker,
        )

        # ── Langfuse conversation logger (prompt + completion text) ────────────
        conversation_logger = ConversationLogger(langfuse_tracker=langfuse_tracker)

        # ── Pipeline ───────────────────────────────────────────────────────────
        pipeline = Pipeline(
            [
                transport.input(),
                stt,
                user_aggregator,
                llm,
                tts,
                metrics_bridge,
                transport.output(),
                assistant_aggregator,
                conversation_logger,
            ]
        )

        task = PipelineTask(
            pipeline,
            params=PipelineParams(
                allow_interruptions=True,
                enable_metrics=True,
                enable_usage_metrics=True,
                report_only_initial_ttfb=False,
            ),
        )

        span.set_attribute("pipeline.stages", 9)
        span.set_attribute("initial_agent", "triage")
        span.set_attribute("transport", transport_type)
        span.set_attribute("entrypoint", entrypoint)
        logger.info(
            "Multi-agent pipeline built session=%s transport=%s entrypoint=%s",
            session_id,
            transport_type,
            entrypoint,
        )

    return pipeline, task, orchestrator, langfuse_tracker
