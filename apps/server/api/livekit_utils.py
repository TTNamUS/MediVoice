"""Shared LiveKit room and token helpers."""

from config import Settings
from livekit import api as livekit_api


async def create_livekit_room(
    settings: Settings,
    room_name: str,
    *,
    max_participants: int = 2,
    empty_timeout: int = 300,
) -> str:
    """Create a LiveKit room and return its name."""
    lk = livekit_api.LiveKitAPI(
        url=settings.livekit_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    )
    try:
        room = await lk.room.create_room(
            livekit_api.CreateRoomRequest(
                name=room_name,
                empty_timeout=empty_timeout,
                max_participants=max_participants,
            )
        )
        return room.name
    finally:
        await lk.aclose()


def create_livekit_token(
    settings: Settings,
    *,
    room_name: str,
    identity: str,
    name: str,
    can_publish: bool = True,
    can_subscribe: bool = True,
) -> str:
    """Generate a short-lived LiveKit access token for one room."""
    token = livekit_api.AccessToken(
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    )
    token.with_identity(identity)
    token.with_name(name)
    token.with_grants(
        livekit_api.VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=can_publish,
            can_subscribe=can_subscribe,
        )
    )
    return token.to_jwt()
