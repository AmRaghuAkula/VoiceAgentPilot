"""Handles ACS (Azure Communication Services) clients via JSON-wrapped audio."""

import asyncio
import base64
import json
import logging

from app.handler.voicelive_media_handler import DEFAULT_CHUNK_SIZE, VoiceLiveMediaHandler

logger = logging.getLogger(__name__)


class ACSMediaHandler(VoiceLiveMediaHandler):
    """Bridges ACS Call Automation WebSocket to Voice Live.

    Overrides only the JSON wrapping/unwrapping; ambient mixing and Voice Live
    connection are inherited from the base class.
    """

    def __init__(self, config, session=None):
        super().__init__(config, route=session.route if session is not None else None)
        self.session = session
        self._pending_sends: set[asyncio.Task] = set()

    @property
    def log_context(self) -> str:
        return self.session.log_context if self.session is not None else ""

    # ------------------------------------------------------------------
    # Call lifecycle — every end goes through CallSession.request_end()
    # ------------------------------------------------------------------

    async def connect_voicelive(self):
        if self.session is None:
            await super().connect_voicelive()
            return
        try:
            await asyncio.wait_for(super().connect_voicelive(), self.session.settings.voice_live_connect_timeout)
        except Exception:
            logger.exception("voicelive_connect_failed %s", self.log_context)
            self.session.request_end("voicelive_connect_failed", self.session.settings.fallback_message)
            raise
        if self.session.terminated_reason is not None:
            self.session.ensure_voicelive_closed()

    async def on_voicelive_ended(self):
        if self.session is None:
            await super().on_voicelive_ended()
            return
        self.session.request_end("voicelive_dropped", self.session.settings.fallback_message)

    # on_call_cap / on_idle are awaited by run_call_loop with no timeout (Q-008). They must stay
    # non-blocking: request_end() is synchronous and spawns the play/hang-up as a session-owned task.
    async def on_call_cap(self):
        if self.session is not None:
            self.session.request_end("call_cap", self.session.settings.goodbye_message)

    async def on_idle(self):
        if self.session is not None:
            self.session.request_end("idle", self.session.settings.fallback_message)

    def stop_forwarding_agent_audio(self) -> None:
        super().stop_forwarding_agent_audio()
        if self.client_ws is None:
            return
        stop = json.dumps({"Kind": "StopAudio", "AudioData": None, "StopAudio": {}})
        task = asyncio.get_running_loop().create_task(self.send_message(stop))
        self._pending_sends.add(task)
        task.add_done_callback(self._pending_sends.discard)

    # ------------------------------------------------------------------
    # Audio output — wrap in ACS JSON protocol
    # ------------------------------------------------------------------

    async def _send_audio_to_client(self, audio_bytes: bytes):
        """Wrap audio in ACS AudioData JSON format before sending."""
        audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
        data = {
            "Kind": "AudioData",
            "AudioData": {"Data": audio_b64},
            "StopAudio": None,
        }
        await self.send_message(json.dumps(data))

    # ------------------------------------------------------------------
    # Inbound audio — parse ACS JSON protocol
    # ------------------------------------------------------------------

    def _receive_audio_from_client(self, data) -> tuple:
        """Parse ACS JSON and extract PCM audio bytes."""
        try:
            msg = json.loads(data)
            if msg.get("kind") == "AudioData":
                audio_data = msg.get("audioData", {})
                incoming_data = audio_data.get("data", "")

                if incoming_data:
                    pcm_bytes = base64.b64decode(incoming_data)
                    chunk_size = len(pcm_bytes)
                else:
                    pcm_bytes = None
                    chunk_size = DEFAULT_CHUNK_SIZE

                if audio_data.get("silent", True):
                    return None, chunk_size
                return pcm_bytes, chunk_size
        except Exception:
            logger.exception("[ACSMediaHandler] Error parsing ACS audio")
        return None, DEFAULT_CHUNK_SIZE
