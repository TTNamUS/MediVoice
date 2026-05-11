"""POST /connect - create a LiveKit room for the browser WebRTC client."""

import asyncio
import logging
import uuid

from config import Settings, get_settings
from fastapi import APIRouter, Depends, HTTPException

from api.livekit_utils import create_livekit_room, create_livekit_token
from bot.observability.otel_setup import get_tracer
from bot.pipeline import build_pipeline
from bot.transports.livekit_sip import build_livekit_transport

logger = logging.getLogger(__name__)
tracer = get_tracer("medivoice.api.connect")
router = APIRouter()


def _require_livekit(settings: Settings) -> None:
    if not settings.livekit_url or not settings.livekit_api_key or not settings.livekit_api_secret:
        raise HTTPException(
            status_code=500,
            detail="LIVEKIT_URL, LIVEKIT_API_KEY, or LIVEKIT_API_SECRET not set",
        )


def _run_bot(
    session_id: str,
    room_url: str,
    room_name: str,
    token: str,
    settings: Settings,
) -> None:
    """Run the bot pipeline in a background asyncio task."""

    async def _bot_task() -> None:
        with tracer.start_as_current_span("bot.session") as span:
            span.set_attribute("session.id", session_id)
            span.set_attribute("transport", "livekit")
            span.set_attribute("livekit.room", room_name)
            try:
                transport = build_livekit_transport(room_url, token, room_name)
                _pipeline, task, _orchestrator, langfuse_tracker = await build_pipeline(
                    transport,
                    settings,
                    session_id,
                    transport_type="livekit",
                    entrypoint="browser",
                )

                from pipecat.pipeline.runner import PipelineRunner

                runner = PipelineRunner()
                async with langfuse_tracker.session_context():
                    await runner.run(task)
            except Exception as e:
                logger.exception("Bot session %s error: %s", session_id, e)
                span.record_exception(e)

    asyncio.create_task(_bot_task())


@router.post("/connect")
async def connect(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    """Return LiveKit connection credentials for the browser client."""
    _require_livekit(settings)

    with tracer.start_as_current_span("api.connect") as span:
        session_id = str(uuid.uuid4())
        room_name = f"medivoice-web-{uuid.uuid4().hex[:8]}"
        span.set_attribute("transport", "livekit")
        span.set_attribute("session.id", session_id)
        span.set_attribute("livekit.room", room_name)

        try:
            await create_livekit_room(settings, room_name, max_participants=2)
            bot_token = create_livekit_token(
                settings,
                room_name=room_name,
                identity=f"medivoice-bot-{session_id}",
                name="MediVoice Bot",
            )
            user_token = create_livekit_token(
                settings,
                room_name=room_name,
                identity=f"medivoice-user-{session_id}",
                name="Clinic Visitor",
            )
        except Exception as e:
            logger.exception("LiveKit room/token setup failed: %s", e)
            raise HTTPException(
                status_code=502,
                detail="Failed to create LiveKit room",
            ) from e

        _run_bot(session_id, settings.livekit_url, room_name, bot_token, settings)

        return {
            "url": settings.livekit_url,
            "token": user_token,
            "room_name": room_name,
            "session_id": session_id,
        }
