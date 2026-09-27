"""Handles ACS (Azure Communication Services) clients via JSON-wrapped audio."""

import asyncio
import base64
import json
import logging

from app.handler.voicelive_media_handler import DEFAULT_CHUNK_SIZE, VoiceLiveMediaHandler
from app.providers.acs.call_session import close_voicelive

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
        self._late_close: asyncio.Task | None = None

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
        if self.session.terminated_reason is not None:
            return  # the call already ended: don't open (and bill) a Voice Live session for it
        failed_first = False
        late_close = None
        try:
            await asyncio.wait_for(super().connect_voicelive(), self.session.settings.voice_live_connect_timeout)
        except Exception:
            if self.session.terminated_reason is None:
                failed_first = True
                logger.exception("voicelive_connect_failed %s", self.log_context)
                self.session.request_end("voicelive_connect_failed", self.session.settings.fallback_message)
            else:
                logger.info("voicelive_connect_aborted reason=%s %s", self.session.terminated_reason, self.log_context)
            raise
        finally:
            # If the call ended (elsewhere) while we were connecting, the session's memoized close may
            # already have run against the half-open handler, so it can't have closed a connection that
            # completed afterwards. Close again ourselves, in a task run_call_loop's cancel can't cut short.
            if not failed_first and self.session.terminated_reason is not None:
                late_close = self._start_late_close()
        if late_close is not None:
            await asyncio.shield(late_close)

    def _start_late_close(self) -> asyncio.Task:
        if self._late_close is None:
            self._late_close = asyncio.get_running_loop().create_task(self._close_late_connection())
        return self._late_close

    async def _close_late_connection(self) -> None:
        close = self.session.ensure_voicelive_closed()
        if close is not None:
            await asyncio.wait([close])  # let the session's close finish first; never raises
        await close_voicelive(self, self.session.settings.close_timeout, self.log_context)

    async def cleanup(self):
        # The base cleanup closes the connection only through _conn_ctx. A cleanup that ran while the
        # SDK handshake was in flight found nothing to close yet and cleared _conn_ctx, so a connection
        # that arrived afterwards is orphaned: close it directly (VoiceLiveConnection.close()).
        orphan = self.conn if self._conn_ctx is None else None
        await super().cleanup()
        if orphan is not None and self.conn is orphan:
            self.conn = None
            try:
                await orphan.close()
            except Exception:
                logger.warning("voicelive orphan close failed %s", self.log_context, exc_info=True)

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
        if self.client_ws is None or (self.session is not None and self.session.disconnected):
            return  # no media socket, or the caller already hung up: nothing to stop
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
