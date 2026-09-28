"""Handles Twilio Media Stream WebSocket and bridges audio to Azure Voice Live API."""

import asyncio
import audioop
import base64
import hashlib
import hmac
import json
import logging
import time

from app.handler.voicelive_media_handler import VoiceLiveMediaHandler

logger = logging.getLogger(__name__)

# Twilio sends mulaw 8000Hz; Voice Live expects PCM 24000Hz 16-bit mono.
TWILIO_SAMPLE_RATE = 8000
VOICELIVE_SAMPLE_RATE = 24000
_TOKEN_TTL = 60


class TwilioMediaHandler(VoiceLiveMediaHandler):
    """Bridges Twilio Media Stream WebSocket to Azure Voice Live API.

    Handles mulaw/PCM conversion, rate resampling, and Twilio protocol.
    """

    def __init__(self, config):
        super().__init__(config)
        self.auth_token = config.get("TWILIO_AUTH_TOKEN", "")
        self.twilio_ws = None
        self.stream_sid = None
        self.call_sid = None
        self.called_number = None  # set by authenticate_and_start() from the signed token
        self.session = None  # TwilioCallSession, attached by /twilio/ws once the route resolves (UT01b)
        self._ratecv_state_in = None
        self._ratecv_state_out = None

    @property
    def log_context(self) -> str:
        return self.session.log_context if self.session is not None else ""

    # ------------------------------------------------------------------
    # Call lifecycle (UT01b) — every end goes through TwilioCallSession.request_end()
    # ------------------------------------------------------------------

    async def connect_voicelive(self):
        """Connect to Voice Live, bounded by VOICE_LIVE_CONNECT_TIMEOUT_SECONDS (the base has no timeout).

        A failure or timeout ends the call. Voice Live itself is closed only by /twilio/ws's finally,
        after run_call_loop has cancelled and awaited this task.
        """
        if self.session is None:
            await super().connect_voicelive()
            return
        if self.session.terminated_reason is not None:
            return  # the call already ended: don't open (and bill) a Voice Live session for it
        try:
            await asyncio.wait_for(super().connect_voicelive(), self.session.voice_live_connect_timeout)
        except Exception:
            if self.session.terminated_reason is None:
                logger.exception("voicelive_connect_failed %s", self.log_context)
                self.session.request_end("voicelive_connect_failed", self.session.fallback_message)
            raise

    async def on_voicelive_ended(self):
        """Voice Live dropped mid-call (B3): end the call instead of leaving the caller in silence.

        The base hook closes client_ws, which this handler never sets (it uses twilio_ws).
        """
        if self.session is None:
            await super().on_voicelive_ended()
            return
        logger.warning("[TwilioMediaHandler] Voice Live disconnected, ending call %s", self.log_context)
        self.session.request_end("voicelive_dropped", self.session.fallback_message)

    # on_call_cap / on_idle are awaited by run_call_loop with no timeout (Q-008). They must stay
    # non-blocking: request_end() is synchronous and schedules the socket close as its own task.
    async def on_call_cap(self):
        if self.session is not None:
            self.session.request_end("call_cap", self.session.goodbye_message)

    async def on_idle(self):
        if self.session is not None:
            self.session.request_end("idle", self.session.fallback_message)

    async def on_response_unrecoverable(self):
        """Q-036 / D-038: repeated failed Voice Live responses end the call instead of leaving silence."""
        if self.session is not None:
            self.session.request_end("response_failed", self.session.fallback_message)

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _verify_ws_token(self, token: str, called_number: str) -> bool:
        """Verify a WebSocket token is valid, not expired, and bound to this called number."""
        if not self.auth_token or not isinstance(token, str) or not token:
            return False
        if not isinstance(called_number, str) or not called_number:
            return False
        parts = token.split(".", 1)
        if len(parts) != 2:
            return False
        timestamp_str, sig = parts
        # Non-ASCII input would make compare_digest/encode() raise instead of rejecting cleanly.
        if not sig.isascii() or not called_number.isascii():
            return False
        try:
            timestamp = int(timestamp_str)
        except ValueError:
            return False
        if time.time() - timestamp > _TOKEN_TTL:
            return False
        expected = hmac.new(
            self.auth_token.encode(), f"{timestamp_str}.{called_number}".encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(sig, expected)

    async def authenticate_and_start(self) -> bool:
        """Wait for the Twilio 'start' message and validate the embedded token.

        Returns True if authenticated, False if rejected (WebSocket already closed).
        """
        while True:
            try:
                msg = await asyncio.wait_for(self.twilio_ws.receive(), timeout=30)
            except TimeoutError:
                logger.warning("[TwilioMediaHandler] Timed out waiting for start message")
                await self.twilio_ws.close(4408, "Timeout")
                return False
            except Exception:
                logger.info("[TwilioMediaHandler] WebSocket closed before start message")
                return False

            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                logger.warning("[TwilioMediaHandler] Non-JSON message before start")
                await self.twilio_ws.close(4400, "Bad Request")
                return False

            event = data.get("event") if isinstance(data, dict) else None

            if event == "connected":
                logger.info("[TwilioMediaHandler] Twilio connected: protocol=%s", data.get("protocol"))
                continue

            if event == "start":
                start = data.get("start")
                custom_params = start.get("customParameters") if isinstance(start, dict) else None
                if not isinstance(custom_params, dict):
                    custom_params = {}
                token = custom_params.get("token", "")
                called_number = custom_params.get("calledNumber", "")
                if not self._verify_ws_token(token, called_number):
                    logger.warning("[TwilioMediaHandler] Invalid or expired stream token")
                    await self.twilio_ws.close(4403, "Forbidden")
                    return False
                # The token binds the called number, so it is now trusted for route resolution.
                self.called_number = called_number
                # Process the start message
                await self.on_message(msg)
                return True

            # Unexpected message before start
            logger.warning("[TwilioMediaHandler] Unexpected message before start: %s", event)
            await self.twilio_ws.close(4400, "Bad Request")
            return False

    # ------------------------------------------------------------------
    # Voice Live hooks
    # ------------------------------------------------------------------

    async def on_speech_started(self):
        """Barge-in: clear Twilio playback and TTS buffer."""
        await self._send_clear_to_twilio()
        if self._ambient_mixer is not None:
            async with self._tts_buffer_lock:
                self._tts_output_buffer.clear()
                self._tts_playback_started = False

    async def on_transcript_done(self, transcript: str):
        """No-op — Twilio has no transcript channel."""
        pass

    # ------------------------------------------------------------------
    # Audio output to client — PCM 24kHz → mulaw 8kHz → Twilio
    # ------------------------------------------------------------------

    async def _send_audio_to_client(self, audio_bytes: bytes):
        """Convert PCM 24kHz to mulaw 8kHz and send to Twilio."""
        if not self.twilio_ws or not self.stream_sid:
            return

        pcm_8k, self._ratecv_state_out = audioop.ratecv(
            audio_bytes, 2, 1, VOICELIVE_SAMPLE_RATE, TWILIO_SAMPLE_RATE, self._ratecv_state_out
        )

        mulaw_bytes = audioop.lin2ulaw(pcm_8k, 2)
        mulaw_b64 = base64.b64encode(mulaw_bytes).decode("ascii")

        msg = {
            "event": "media",
            "streamSid": self.stream_sid,
            "media": {"payload": mulaw_b64},
        }
        try:
            await self.twilio_ws.send(json.dumps(msg))
        except Exception as e:
            logger.debug("[TwilioMediaHandler] Audio send failed: %s", e)

    # ------------------------------------------------------------------
    # Twilio message handling
    # ------------------------------------------------------------------

    async def on_message(self, message: str):
        """Process one incoming Twilio WebSocket message."""
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            logger.warning("[TwilioMediaHandler] Non-JSON message received")
            return

        event = data.get("event")

        match event:
            case "connected":
                logger.info("[TwilioMediaHandler] Twilio connected: protocol=%s", data.get("protocol"))

            case "start":
                self.stream_sid = data.get("streamSid")
                start_info = data.get("start", {})
                self.call_sid = start_info.get("callSid")
                logger.info(
                    "[TwilioMediaHandler] Stream started: sid=%s, call=%s, format=%s",
                    self.stream_sid,
                    self.call_sid,
                    start_info.get("mediaFormat"),
                )

            case "media":
                media = data.get("media", {})
                payload = media.get("payload", "")
                if payload:
                    mulaw_bytes = base64.b64decode(payload)
                    await self.handle_audio(mulaw_bytes)

            case "stop":
                logger.info("[TwilioMediaHandler] Stream stopped: sid=%s", self.stream_sid)

            case "dtmf":
                digit = data.get("dtmf", {}).get("digit")
                logger.info("[TwilioMediaHandler] DTMF received: %s", digit)

            case "mark":
                mark_name = data.get("mark", {}).get("name")
                logger.debug("[TwilioMediaHandler] Mark received: %s", mark_name)

            case _:
                logger.debug("[TwilioMediaHandler] Unknown event: %s", event)

    # ------------------------------------------------------------------
    # Inbound audio — mulaw 8kHz → PCM 24kHz
    # ------------------------------------------------------------------

    def _receive_audio_from_client(self, data) -> tuple:
        """Convert Twilio mulaw/8kHz bytes to PCM 24kHz."""
        pcm_8k = audioop.ulaw2lin(data, 2)
        pcm_24k, self._ratecv_state_in = audioop.ratecv(
            pcm_8k, 2, 1, TWILIO_SAMPLE_RATE, VOICELIVE_SAMPLE_RATE, self._ratecv_state_in
        )
        return pcm_24k, len(pcm_24k)

    async def _send_clear_to_twilio(self):
        """Sends a clear message to Twilio to stop current audio playback."""
        if not self.twilio_ws or not self.stream_sid:
            return
        self._ratecv_state_out = None
        msg = {"event": "clear", "streamSid": self.stream_sid}
        await self.twilio_ws.send(json.dumps(msg))
