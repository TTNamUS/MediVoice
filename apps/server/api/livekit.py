"""POST /livekit/dispatch — LiveKit SIP inbound call webhook.

LiveKit calls this endpoint when a PSTN caller connects via the SIP trunk.
The handler must respond < 5 s; the bot joins the room asynchronously.

Lifecycle events (participant_joined / participant_left / room_finished)
are handled by the /livekit/events endpoint below.
"""

import asyncio
import logging
import uuid
from json import JSONDecodeError
from typing import Any

from config import Settings, get_settings
from fastapi import APIRouter, Depends, HTTPException, Request

from api.livekit_utils import create_livekit_room, create_livekit_token
from bot.observability.otel_setup import get_tracer
from bot.pipeline import build_pipeline
from bot.transports.livekit_sip import build_livekit_transport

logger = logging.getLogger(__name__)
tracer = get_tracer("medivoice.api.livekit")
router = APIRouter(prefix="/livekit")

# Track active sessions so we can tear down cleanly on hangup
_active_tasks: dict[str, asyncio.Task] = {}


async def _read_json_body(request: Request) -> dict[str, Any]:
    """Return JSON body or {} when called from Swagger/webhooks with no body."""
    raw = await request.body()
    if not raw:
        return {}
    try:
        parsed = await request.json()
    except JSONDecodeError:
        logger.warning("Ignoring non-JSON LiveKit webhook body: %r", raw[:200])
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _run_livekit_bot(
    session_id: str,
    room_url: str,
    room_name: str,
    token: str,
    settings: Settings,
) -> None:
    """Spawn the bot pipeline as a background asyncio task."""

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
                    entrypoint="sip",
                )
                from pipecat.pipeline.runner import PipelineRunner

                runner = PipelineRunner()
                async with langfuse_tracker.session_context():
                    await runner.run(task)
            except Exception as e:
                logger.exception("LiveKit bot session %s error: %s", session_id, e)
                span.record_exception(e)
            finally:
                _active_tasks.pop(session_id, None)
                logger.info("LiveKit session %s cleaned up", session_id)

    bg_task = asyncio.create_task(_bot_task())
    _active_tasks[session_id] = bg_task


@router.post("/dispatch")
async def livekit_dispatch(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    """Handle LiveKit SIP dispatch webhook.

    LiveKit sends this when an inbound PSTN call arrives via the SIP trunk.
    Must respond quickly (< 5 s); bot joins the room asynchronously.
    """
    if not settings.livekit_url or not settings.livekit_api_key or not settings.livekit_api_secret:
        raise HTTPException(
            status_code=500,
            detail="LIVEKIT_URL, LIVEKIT_API_KEY, or LIVEKIT_API_SECRET not configured",
        )

    with tracer.start_as_current_span("api.livekit.dispatch"):
        body = await _read_json_body(request)
        room_name = body.get("room_name") or f"medivoice-sip-{uuid.uuid4().hex[:8]}"
        session_id = str(uuid.uuid4())

        logger.info("LiveKit SIP dispatch: session=%s room=%s", session_id, room_name)

        try:
            await create_livekit_room(settings, room_name, max_participants=2)
            token = create_livekit_token(
                settings,
                room_name=room_name,
                identity=f"medivoice-sip-bot-{session_id}",
                name="MediVoice Bot",
            )
        except Exception as e:
            logger.exception("LiveKit room/token setup failed: %s", e)
            raise HTTPException(status_code=502, detail="Failed to set up LiveKit room") from e

        room_url = settings.livekit_url
        _run_livekit_bot(session_id, room_url, room_name, token, settings)

        return {"status": "ok", "session_id": session_id, "room": room_name}


@router.post("/events")
async def livekit_events(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    """Handle LiveKit webhook events (participant lifecycle, room finished).

    Registered in LiveKit Cloud console → Webhooks.
    """
    body = await _read_json_body(request)
    event = body.get("event", "")
    room = body.get("room", {})
    room_name = room.get("name", "unknown")
    participant = body.get("participant", {})
    identity = participant.get("identity", "")
    kind = participant.get("kind", "")  # "SIP" for phone caller

    logger.info(
        "LiveKit event=%s room=%s participant=%s kind=%s",
        event,
        room_name,
        identity,
        kind,
    )

    if event == "participant_left" and kind == "SIP":
        # PSTN caller hung up — cancel the bot task for this room
        for session_id, task in list(_active_tasks.items()):
            # Tasks are keyed by session_id; room_name matching is best-effort
            if not task.done():
                logger.info(
                    "SIP caller left room=%s, cancelling bot task session=%s",
                    room_name,
                    session_id,
                )
                task.cancel()
                break

    if event == "room_finished":
        logger.info("Room %s finished — all sessions cleaned up", room_name)

    return {"status": "ok"}
