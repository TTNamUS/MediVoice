"""LiveKit transport factory with clinic-specific defaults.

Used by both browser WebRTC sessions and SIP dial-in calls. Both paths call
the same build_pipeline() factory; only the room creation entry point differs.
"""

import logging

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.transports.livekit.transport import LiveKitParams, LiveKitTransport

logger = logging.getLogger(__name__)


def build_livekit_transport(
    room_url: str,
    token: str,
    room_name: str,
) -> LiveKitTransport:
    """Return a LiveKitTransport configured for full-duplex voice audio."""
    return LiveKitTransport(
        url=room_url,
        token=token,
        room_name=room_name,
        params=LiveKitParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_enabled=True,
            vad_analyzer=SileroVADAnalyzer(),
        ),
    )
