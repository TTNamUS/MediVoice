"""Smoke tests for pipeline construction and multi-provider LLM config."""

import asyncio
from unittest.mock import MagicMock, patch

from config import Settings


def _mock_settings(**overrides) -> Settings:
    defaults = dict(
        anthropic_api_key="sk-ant-test",
        deepgram_api_key="dg-test",
        cartesia_api_key="ca-test",
        cartesia_voice_id="test-voice-id",
        livekit_url="wss://test.livekit.cloud",
        livekit_api_key="lk-test",
        livekit_api_secret="lk-secret-test",
        otel_exporter_otlp_endpoint="http://localhost:4319",
        otel_service_name="medivoice-test",
        app_env="test",
    )
    defaults.update(overrides)
    return Settings(**defaults)


# ── Settings: validate_critical ───────────────────────────────────────────────


def test_validate_critical_anthropic_passes() -> None:
    settings = _mock_settings(llm_provider="anthropic")
    assert settings.validate_critical() == []


def test_validate_critical_openai_passes() -> None:
    settings = _mock_settings(llm_provider="openai", openai_api_key="sk-openai-test")
    assert settings.validate_critical() == []


def test_validate_critical_gemini_passes() -> None:
    settings = _mock_settings(llm_provider="gemini", gemini_api_key="AIza-test")
    assert settings.validate_critical() == []


def test_validate_critical_fails_missing_provider_key() -> None:
    # openai provider selected but no openai key
    settings = _mock_settings(llm_provider="openai", openai_api_key="")
    missing = settings.validate_critical()
    assert "OPENAI_API_KEY" in missing


def test_validate_critical_fails_missing_service_keys() -> None:
    settings = Settings(
        anthropic_api_key="sk-ant-test",
        deepgram_api_key="",
        cartesia_api_key="",
        livekit_url="",
        livekit_api_key="",
        livekit_api_secret="",
    )
    missing = settings.validate_critical()
    assert "DEEPGRAM_API_KEY" in missing
    assert "CARTESIA_API_KEY" in missing
    assert "LIVEKIT_URL" in missing
    assert "LIVEKIT_API_KEY" in missing
    assert "LIVEKIT_API_SECRET" in missing


def test_validate_critical_does_not_require_inactive_provider_key() -> None:
    # Anthropic selected; OpenAI key blank — should not fail
    settings = _mock_settings(llm_provider="anthropic", openai_api_key="")
    assert settings.validate_critical() == []


# ── Settings: helpers ─────────────────────────────────────────────────────────


def test_active_llm_api_key_routes_correctly() -> None:
    s = _mock_settings(llm_provider="openai", openai_api_key="sk-openai-test")
    assert s.active_llm_api_key() == "sk-openai-test"

    s2 = _mock_settings(llm_provider="gemini", gemini_api_key="AIza-test")
    assert s2.active_llm_api_key() == "AIza-test"

    s3 = _mock_settings(llm_provider="anthropic")
    assert s3.active_llm_api_key() == "sk-ant-test"


def test_active_llm_model_returns_default() -> None:
    s = _mock_settings(llm_provider="anthropic")
    assert "claude" in s.active_llm_model()

    s2 = _mock_settings(llm_provider="openai")
    assert "gpt" in s2.active_llm_model()

    s3 = _mock_settings(llm_provider="gemini")
    assert "gemini" in s3.active_llm_model()


def test_active_llm_model_respects_override() -> None:
    s = _mock_settings(llm_provider="openai", openai_model="gpt-4o")
    assert s.active_llm_model() == "gpt-4o"


def test_is_production() -> None:
    assert _mock_settings(app_env="production").is_production() is True
    assert _mock_settings(app_env="development").is_production() is False


# ── Pipeline construction ─────────────────────────────────────────────────────


def _run_build_pipeline(settings: Settings, mock_llm_cls, mock_module: str) -> None:
    from bot.observability.otel_setup import setup_tracing

    setup_tracing("medivoice-test", "http://localhost:4319", "test")

    mock_transport = MagicMock()
    mock_transport.input.return_value = MagicMock()
    mock_transport.output.return_value = MagicMock()

    mock_llm = MagicMock()
    mock_llm.create_context_aggregator.return_value = MagicMock()
    mock_llm_cls.return_value = mock_llm

    async def _run():
        from bot.pipeline import build_pipeline

        pipeline, task, orchestrator, langfuse_tracker = await build_pipeline(
            mock_transport, settings, "test-session"
        )
        assert pipeline is not None
        assert task is not None
        assert orchestrator is not None
        assert langfuse_tracker is not None

    asyncio.run(_run())


@patch("bot.pipeline.DeepgramSTTService")
@patch("bot.pipeline.CartesiaTTSService")
@patch("bot.pipeline.Pipeline")
@patch("bot.pipeline.PipelineTask")
@patch("pipecat.services.anthropic.llm.AnthropicLLMService")
def test_build_pipeline_anthropic(mock_ant, mock_task, mock_pipe, mock_tts, mock_stt) -> None:
    settings = _mock_settings(llm_provider="anthropic")
    _run_build_pipeline(settings, mock_ant, "pipecat.services.anthropic.llm.AnthropicLLMService")


@patch("bot.pipeline.DeepgramSTTService")
@patch("bot.pipeline.CartesiaTTSService")
@patch("bot.pipeline.Pipeline")
@patch("bot.pipeline.PipelineTask")
@patch("pipecat.services.openai.llm.OpenAILLMService")
def test_build_pipeline_openai(mock_oai, mock_task, mock_pipe, mock_tts, mock_stt) -> None:
    settings = _mock_settings(llm_provider="openai", openai_api_key="sk-openai-test")
    _run_build_pipeline(settings, mock_oai, "pipecat.services.openai.llm.OpenAILLMService")


@patch("bot.pipeline.DeepgramSTTService")
@patch("bot.pipeline.CartesiaTTSService")
@patch("bot.pipeline.Pipeline")
@patch("bot.pipeline.PipelineTask")
@patch("pipecat.services.google.llm.GoogleLLMService")
def test_build_pipeline_gemini(mock_gem, mock_task, mock_pipe, mock_tts, mock_stt) -> None:
    settings = _mock_settings(llm_provider="gemini", gemini_api_key="AIza-test")
    _run_build_pipeline(settings, mock_gem, "pipecat.services.google.GoogleLLMService")
