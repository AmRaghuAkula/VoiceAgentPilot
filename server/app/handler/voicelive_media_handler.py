"""Base handler for Azure Voice Live API connections using the official SDK.

Provides the shared Voice Live connection, event processing, web client
audio handling with ambient mixing, and cleanup logic. Telephony subclasses
override on_message() and hook methods to implement protocol-specific behavior.
"""

import asyncio
import base64
import json
import logging
import time
import uuid
from collections import deque
from types import MappingProxyType
from typing import Optional, Union

import numpy as np
from azure.core.credentials import AzureKeyCredential
from azure.identity.aio import DefaultAzureCredential, ManagedIdentityCredential
from azure.ai.voicelive.aio import connect as voicelive_connect
from azure.ai.voicelive.models import (
    AudioEchoCancellation,
    AudioNoiseReduction,
    AzureSemanticVad,
    AzureStandardVoice,
    InputAudioFormat,
    InputTextContentPart,
    Modality,
    OutputAudioFormat,
    RequestSession,
    ResponseStatus,
    ServerEventType,
    SystemMessageItem,
    UserMessageItem,
)

from .ambient_mixer import AmbientMixer

# Data type for WebSocket messages (str or bytes) sent to client
Data = Union[str, bytes]

logger = logging.getLogger(__name__)

# Default chunk size in bytes (100ms of audio at 24kHz, 16-bit mono)
DEFAULT_CHUNK_SIZE = 4800  # 24000 samples/sec * 0.1 sec * 2 bytes

# Q-036 / D-038: on a failed response the bridge adds this neutral, factual marker as a conversation
# item and retries once. It is not a scripted line: the agent still authors what is said (D-004).
FAILED_RESPONSE_MARKER = "[caller audio was not understood]"
# Q-063 / D-048: agent mode rejects a response.create() on an empty conversation, so before the
# connect-time greeting the bridge adds this neutral, factual system item. It states what happened,
# not what to say: the agent's own instructions still author the greeting (D-004).
CALL_CONNECTED_MARKER = "[call connected]"
# Consecutive failed responses tolerated before the call is ended (one retry, then end).
MAX_CONSECUTIVE_FAILED_RESPONSES = 2
# Q-070 / D-050: agent mode can emit response.created and then never a response.done (of any status)
# for that response, with no error or warning event; every later caller turn is then ignored. A
# response with no progress (no response.* event for it) for this long is treated as a failed response
# and goes through the same D-038/D-048 recovery. On the live calls, normal responses finished in about
# 2-4 s and stuck ones never finished (one call had 18+ s of dead air before the caller hung up). 15 s
# is about 4-7x the normal latency and still recovers well inside a caller's patience. Streaming
# progress (audio or transcript deltas) resets the deadline, so a long spoken answer is never cut off.
RESPONSE_TIMEOUT_SECONDS = 15.0
# While Voice Live reports a tool call in progress for the watched response (MCP, function, web or
# file search), the next progress event may legitimately be a long way off, and re-running a tool
# with side effects (e.g. a booking) would be worse than waiting. The deadline is stretched to this
# for that one gap only; the next ordinary progress event brings it back to RESPONSE_TIMEOUT_SECONDS.
TOOL_CALL_TIMEOUT_SECONDS = 45.0
_TOOL_CALL_EVENTS = frozenset({
    ServerEventType.RESPONSE_FUNCTION_CALL_ARGUMENTS_DONE.value,
    ServerEventType.RESPONSE_MCP_CALL_ARGUMENTS_DONE.value,
    ServerEventType.RESPONSE_MCP_CALL_IN_PROGRESS.value,
    ServerEventType.RESPONSE_WEB_SEARCH_CALL_SEARCHING.value,
    ServerEventType.RESPONSE_WEB_SEARCH_CALL_IN_PROGRESS.value,
    ServerEventType.RESPONSE_FILE_SEARCH_CALL_SEARCHING.value,
    ServerEventType.RESPONSE_FILE_SEARCH_CALL_IN_PROGRESS.value,
})
# A tool call ends with one of these; the ordinary RESPONSE_TIMEOUT_SECONDS applies again after it.
_TOOL_CALL_END_EVENTS = frozenset({
    ServerEventType.RESPONSE_MCP_CALL_COMPLETED.value,
    ServerEventType.RESPONSE_MCP_CALL_FAILED.value,
    ServerEventType.RESPONSE_WEB_SEARCH_CALL_COMPLETED.value,
    ServerEventType.RESPONSE_FILE_SEARCH_CALL_COMPLETED.value,
    ServerEventType.RESPONSE_OUTPUT_ITEM_DONE.value,
})
# After cancelling a stuck response, wait at most this long for Voice Live's response.done for it
# before sending the retry, so the retry is not rejected because the old response still looks active.
CANCEL_ACK_TIMEOUT_SECONDS = 2.0
# Stands in for "the response the bridge's own timed-out retry should start" until its
# response.created arrives, so a retry Voice Live silently ignores is itself caught (D-050).
_PENDING_RETRY = object()


def _deep_thaw(value):
    """Recursively convert BridgeConfig's frozen structures back to plain
    JSON-serializable types (MappingProxyType -> dict, tuple -> list).

    BridgeConfig's _deep_freeze() freezes nested dicts/lists too, so a
    shallow dict(...) copy of a frozen mapping still has frozen values
    nested inside it, which json.dumps() cannot serialize.
    """
    if isinstance(value, MappingProxyType):
        return {k: _deep_thaw(v) for k, v in value.items()}
    if isinstance(value, dict):
        return {k: _deep_thaw(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_deep_thaw(v) for v in value]
    return value


class VoiceLiveMediaHandler:
    """Handles the connection to Azure Voice Live API and web clients.

    Uses the azure-ai-voicelive SDK for typed session config, event handling,
    and audio streaming. Provides web client audio handling (raw PCM + ambient
    mixing) by default. Telephony subclasses override on_message() and hooks
    for their specific protocols.
    """

    def __init__(self, config, route=None):
        self.endpoint = config["AZURE_VOICE_LIVE_ENDPOINT"]
        self.model = config["VOICE_LIVE_MODEL"]
        self.api_key = config["AZURE_VOICE_LIVE_API_KEY"]
        self.client_id = config["AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID"]
        self.route = route
        self.api_version = config.get("VOICE_LIVE_API_VERSION")
        self.interim_response = config.get("INTERIM_RESPONSE")
        self.session_id = None
        self.conversation_id = None
        self._forward_agent_audio = True
        self.conn = None
        self._conn_ctx = None  # async context manager from SDK connect()
        self._credential = None  # kept alive for token refresh
        self._receiver_task = None
        self._voicelive_connected = False  # True while Voice Live WS is healthy
        self._consecutive_failed_responses = 0  # Q-036: reset by a completed response
        self._caller_turn_seen = False  # Q-036: no marker on a failure before the caller has spoken
        # Q-070 / D-050 response watchdog: at most one timer task, for the one in-flight response.
        self._watchdog_task: Optional[asyncio.Task] = None
        self._watched_response_id = None
        self._watchdog_deadline = 0.0
        self._tool_call_open = False  # the watched response reported a tool call not yet ended
        self._recovery_tasks: set = set()  # timers that fired and are running the recovery
        self._timed_out_response_ids = deque(maxlen=8)  # late events for these are ignored
        self._cancel_ack = None  # (response_id, asyncio.Event) while waiting for a cancel to land
        self._retry_event_id = None  # event_id of the timeout path's pending retry response.create

        # Client WebSocket
        self.client_ws = None

        # TTS output buffering for continuous ambient mixing
        self._tts_output_buffer = bytearray()
        self._tts_buffer_lock = asyncio.Lock()
        self._max_buffer_size = 480000  # 10 seconds of audio
        self._buffer_warning_logged = False
        self._tts_playback_started = False
        self._min_buffer_to_start = 9600  # 200ms buffer before starting TTS playback

        # Ambient mixer initialization
        self._ambient_mixer: Optional[AmbientMixer] = None
        ambient_preset = config.get("AMBIENT_PRESET", "none")
        if ambient_preset and ambient_preset != "none":
            try:
                self._ambient_mixer = AmbientMixer(preset=ambient_preset)
            except Exception as e:
                logger.error(f"Failed to initialize AmbientMixer: {e}")

    @property
    def log_context(self) -> str:
        return ""

    def _session_config(self) -> RequestSession:
        if self.route is not None:
            return self._agent_session_config()
        return self._model_session_config()

    def _agent_session_config(self) -> RequestSession:
        fields = {"input_audio_format": "pcm16", "output_audio_format": "pcm16"}
        if self.interim_response:
            fields["interim_response"] = _deep_thaw(self.interim_response)
        return RequestSession(fields)

    def _model_session_config(self) -> RequestSession:
        """Return the typed session configuration for Voice Live."""
        return RequestSession(
            modalities=[Modality.TEXT, Modality.AUDIO],
            instructions="You are a helpful AI assistant responding in natural, engaging language.",
            turn_detection=AzureSemanticVad(),
            input_audio_format=InputAudioFormat.PCM16,
            output_audio_format=OutputAudioFormat.PCM16,
            input_audio_noise_reduction=AudioNoiseReduction(type="azure_deep_noise_suppression"),
            input_audio_echo_cancellation=AudioEchoCancellation(),
            voice=AzureStandardVoice(name="en-US-Aria:DragonHDLatestNeural", temperature=0.8),
        )

    # ------------------------------------------------------------------
    # Voice Live connection
    # ------------------------------------------------------------------

    async def connect_voicelive(self):
        """Connect to Azure Voice Live API using the SDK (agent mode when a route is set)."""
        t0 = time.perf_counter()

        if self.route is not None:
            self._credential = DefaultAzureCredential(managed_identity_client_id=self.client_id or None)
            connect_kwargs = {
                "endpoint": self.endpoint,
                "credential": self._credential,
                "agent_name": self.route.agent,
                "project_name": self.route.project,
            }
            if self.route.is_unpinned:
                # D-049: omit agent_version entirely -- azure-ai-voicelive 1.3.0 then sends no
                # agent-version query param and Voice Live resolves the latest saved version.
                # Never pass the literal "latest" through to the service.
                logger.info(
                    "[VoiceLive] Agent mode project=%s agent=%s version=latest (unpinned, D-049) %s",
                    self.route.project, self.route.agent, self.log_context,
                )
            else:
                connect_kwargs["agent_version"] = self.route.version
                logger.info(
                    "[VoiceLive] Agent mode project=%s agent=%s version=%s %s",
                    self.route.project, self.route.agent, self.route.version, self.log_context,
                )
        else:
            if self.client_id:
                self._credential = ManagedIdentityCredential(client_id=self.client_id)
                credential = self._credential
            else:
                credential = AzureKeyCredential(self.api_key)
            connect_kwargs = {"endpoint": self.endpoint, "credential": credential, "model": self.model.strip()}

        if self.api_version:
            connect_kwargs["api_version"] = self.api_version

        t1 = time.perf_counter()
        logger.info("[VoiceLive] Credential prepared in %.2fs", t1 - t0)

        self._conn_ctx = voicelive_connect(**connect_kwargs)
        self.conn = await self._conn_ctx.__aenter__()

        t2 = time.perf_counter()
        logger.info("[VoiceLive] SDK connected in %.2fs (total %.2fs)", t2 - t1, t2 - t0)

        await self.conn.session.update(session=self._session_config())
        if self.route is not None:
            await self._add_call_connected_item()
            logger.info("[VoiceLive] greeting_input item=call_connected %s", self.log_context)
        await self.conn.response.create()

        self._voicelive_connected = True
        self._receiver_task = asyncio.create_task(self._receiver_loop())

    async def _add_call_connected_item(self) -> None:
        """Q-063 / D-048: give agent mode the one input item it needs before an opening response."""
        await self.conn.conversation.item.create(
            item=SystemMessageItem(content=[InputTextContentPart(text=CALL_CONNECTED_MARKER)])
        )

    async def send_audio(self, audio_b64: str):
        """Send PCM 24kHz 16-bit mono audio (base64) to Voice Live."""
        if not self._voicelive_connected:
            return
        await self.conn.input_audio_buffer.append(audio=audio_b64)

    async def _receiver_loop(self):
        """Receives typed events from Voice Live and dispatches to hook methods."""
        cancelled = False
        try:
            async for event in self.conn:
                event_type = event.type
                self._note_response_progress(event_type, event)

                match event_type:
                    case ServerEventType.SESSION_CREATED:
                        self.session_id = event.session.id if hasattr(event, "session") else None
                        logger.info("[VoiceLive] Session ID: %s %s", self.session_id, self.log_context)

                    case ServerEventType.SESSION_UPDATED:
                        logger.info("[VoiceLive] Session updated")

                    case ServerEventType.INPUT_AUDIO_BUFFER_CLEARED:
                        logger.debug("[VoiceLive] Input audio buffer cleared")

                    case ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
                        logger.info(
                            "[VoiceLive] Speech started at %s ms",
                            event.audio_start_ms,
                        )
                        self._caller_turn_seen = True
                        await self.on_speech_started()

                    case ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STOPPED:
                        logger.info("[VoiceLive] Speech stopped")

                    case ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
                        self._caller_turn_seen = True
                        transcript = event.transcript
                        logger.debug("[VoiceLive] User: %s", transcript)

                    case ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_FAILED:
                        logger.warning(
                            "[VoiceLive] Transcription error: %s", event.error if hasattr(event, "error") else "unknown"
                        )

                    case ServerEventType.RESPONSE_AUDIO_DELTA:
                        delta = event.delta
                        # D-050: a response the watchdog gave up on must not talk over its retry.
                        if delta and not self._is_timed_out(event):
                            await self.on_audio_delta(delta)

                    case ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE:
                        transcript = event.transcript
                        logger.debug("[VoiceLive] AI: %s", transcript)
                        if not self._is_timed_out(event):  # the caller never heard it (D-050)
                            await self.on_transcript_done(transcript)

                    case ServerEventType.RESPONSE_CREATED:
                        response_id = getattr(getattr(event, "response", None), "id", None)
                        logger.debug("[VoiceLive] Response created: id=%s", response_id)
                        if response_id is None:
                            # Unwatchable: its response.done could never be matched to disarm it.
                            logger.warning("[VoiceLive] response_created_without_id not watched %s",
                                           self.log_context)
                        else:
                            self._arm_response_watchdog(response_id)

                    case ServerEventType.RESPONSE_DONE:
                        response = getattr(event, "response", None)
                        response_id = getattr(response, "id", None)
                        logger.info("[VoiceLive] Response done: id=%s", response_id)
                        # D-050 race rule: whichever of the timer and this event claims the response
                        # first wins. Both claims are synchronous, so exactly one side acts.
                        timed_out = response_id is not None and response_id in self._timed_out_response_ids
                        if not timed_out:
                            self._disarm_response_watchdog(response_id)
                        conversation_id = getattr(response, "conversation_id", None)
                        if conversation_id and conversation_id != self.conversation_id:
                            self.conversation_id = conversation_id
                            logger.info(
                                "[VoiceLive] conversation_id=%s session_id=%s %s",
                                conversation_id, self.session_id, self.log_context,
                            )
                        if timed_out:
                            logger.warning(
                                "[VoiceLive] response_done_after_timeout id=%s status=%s ignored %s",
                                response_id, getattr(response, "status", None), self.log_context,
                            )
                            if self._cancel_ack is not None and self._cancel_ack[0] == response_id:
                                self._cancel_ack[1].set()
                        else:
                            await self._on_response_status(response)

                    case ServerEventType.ERROR:
                        logger.error("[VoiceLive] Error: %s", event.error)
                        if self._is_retry_rejection(event):
                            # D-050: the timed-out response's retry was rejected (e.g. the stuck
                            # response still looks active). Don't wait out another full timeout.
                            self._on_retry_rejected()

                    case _:
                        logger.debug("[VoiceLive] Event: %s", event_type)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            logger.exception("[VoiceLive] Receiver loop error")
        finally:
            self._voicelive_connected = False
            # No event can resolve a response any more: stop the timer and any recovery in flight
            # (it would only send on a dead connection). cleanup() still awaits what is cancelled here.
            self._cancel_watchdog_and_recovery()
            if not cancelled:
                try:
                    await self.on_voicelive_ended()
                except Exception:
                    logger.exception("[VoiceLive] on_voicelive_ended hook raised")

    async def _on_response_status(self, response) -> None:
        """Q-036 / D-038: never leave the caller in silence after a failed response.

        completed -> reset the failure counter. cancelled (barge-in) / incomplete / anything else ->
        no action. failed -> WARNING log, then either one retry (neutral marker item + plain
        response.create(), no instruction overrides) or, on a repeat failure, end the call through
        on_response_unrecoverable() (the subclass's request_end() path, D-005). If the caller has
        not spoken yet (the connect-time greeting failed), the caller-audio marker would be false;
        in agent mode the retry instead re-adds the neutral call-connected item (Q-063 / D-048),
        defensively, so the retry never depends on the connect-time item having been accepted.
        """
        status = getattr(response, "status", None)
        if status == ResponseStatus.COMPLETED:
            self._consecutive_failed_responses = 0
            return
        if status != ResponseStatus.FAILED:
            return

        self._consecutive_failed_responses += 1
        attempt = self._consecutive_failed_responses
        error = getattr(getattr(response, "status_details", None), "error", None)
        if isinstance(error, dict):
            code, err_type = error.get("code"), error.get("type")
        else:
            code, err_type = getattr(error, "code", None), getattr(error, "type", None)
        logger.warning(
            "[VoiceLive] response_failed id=%s code=%s type=%s attempt=%d %s",
            getattr(response, "id", None), code, err_type, attempt, self.log_context,
        )
        await self._retry_or_end_call(attempt)

    async def _add_recovery_marker(self) -> str:
        """Add the neutral marker item a recovery retry needs, and return its log name.

        D-038: once the caller has spoken, the caller-audio marker. D-048: before any caller turn in
        agent mode, the call-connected item (agent mode needs one input item). Otherwise nothing.
        """
        if self._caller_turn_seen:
            await self.conn.conversation.item.create(
                item=UserMessageItem(content=[InputTextContentPart(text=FAILED_RESPONSE_MARKER)])
            )
            return "caller_audio"
        if self.route is not None:
            await self._add_call_connected_item()
            return "call_connected"
        return "none"

    async def _retry_or_end_call(self, attempt: int, *, watch_retry: bool = False) -> None:
        """Shared D-038/D-048 recovery: one retry (neutral marker + plain response.create(), no
        instruction overrides), or end the call through on_response_unrecoverable() once retries are
        exhausted or the retry itself cannot be sent.

        watch_retry (D-050 timeout path only): arm the watchdog for the retry before sending it, so a
        retry that Voice Live never starts is also caught and ends the call instead of leaving silence.
        It is armed before response.create() so the retry's own response.created always replaces it.
        The timeout path's retry is also skipped if, while it was awaiting, Voice Live started a new
        response on its own (it is then being watched: it is already answering the caller, and a
        second response.create() would only collide with it), or the failure count moved (a
        response completed, or the FAILED path already handled a later failure).
        """
        if attempt < MAX_CONSECUTIVE_FAILED_RESPONSES:
            try:
                if watch_retry and self._retry_superseded(attempt):
                    return
                marker = await self._add_recovery_marker()
                if watch_retry:
                    if self._retry_superseded(attempt):
                        return
                    self._arm_response_watchdog(_PENDING_RETRY)
                    # Tag the retry so an error Voice Live sends back for it can be told apart from
                    # any other error (the error's event_id names the client event that caused it).
                    self._retry_event_id = f"retry-{uuid.uuid4().hex}"
                    await self.conn.response.create(event_id=self._retry_event_id)
                else:
                    await self.conn.response.create()
                logger.warning(
                    "[VoiceLive] response_retry attempt=%d marker=%s %s", attempt, marker, self.log_context,
                )
                return
            except Exception:
                if watch_retry and self._watched_response_id is _PENDING_RETRY:
                    self._cancel_response_watchdog()
                logger.exception("[VoiceLive] response_retry_failed attempt=%d %s", attempt, self.log_context)

        logger.error("[VoiceLive] response_unrecoverable attempts=%d ending call %s", attempt, self.log_context)
        try:
            await self.on_response_unrecoverable()
        except Exception:
            logger.exception("[VoiceLive] on_response_unrecoverable hook raised %s", self.log_context)

    def _retry_superseded(self, attempt: int) -> bool:
        if self._watchdog_task is not None:
            reason = "new_response_started"
        elif self._consecutive_failed_responses != attempt:
            reason = "failure_count_changed"
        else:
            return False
        logger.warning(
            "[VoiceLive] response_retry_skipped attempt=%d reason=%s %s", attempt, reason, self.log_context,
        )
        return True

    # ------------------------------------------------------------------
    # Q-070 / D-050: response watchdog
    # ------------------------------------------------------------------
    #
    # A Voice Live response can be created and then never resolved, with no event at all, so the
    # D-038 recovery in _on_response_status() is never reached. The watchdog notices the absence of
    # progress instead and runs the same recovery. It adds no new caller-facing behavior: the retry
    # uses the same neutral markers as D-038/D-048, and the agent still authors everything the caller
    # hears (D-004). Everything below runs on the event loop, and the claim steps have no await, so
    # the timer and a late response.done can never both act on one response.

    def _arm_response_watchdog(self, response_id) -> None:
        """Start the timer for a newly created response, replacing any previous timer.

        Voice Live runs one response at a time, so a new response.created normally means the
        previous one resolved. If one arrives while another is still watched, the new one is the
        one to watch: its timer replaces the old (the old task is cancelled, never leaked).
        """
        self._cancel_response_watchdog()
        self._watched_response_id = response_id
        self._tool_call_open = False
        self._watchdog_deadline = asyncio.get_running_loop().time() + RESPONSE_TIMEOUT_SECONDS
        task = asyncio.create_task(self._response_watchdog(response_id))
        task.add_done_callback(self._on_watchdog_task_done)
        self._watchdog_task = task

    def _disarm_response_watchdog(self, response_id) -> None:
        """The watched response resolved: stop its timer. Only an exact id match counts; the
        pending-retry timer is satisfied only by the retry's own response.created (which re-arms)."""
        if self._watchdog_task is None or response_id is None:
            return
        if self._watched_response_id == response_id:
            self._cancel_response_watchdog()

    def _cancel_response_watchdog(self) -> None:
        task, self._watchdog_task = self._watchdog_task, None
        self._watched_response_id = None
        self._tool_call_open = False
        self._retry_event_id = None
        if task is not None and not task.done():
            task.cancel()

    def _cancel_watchdog_and_recovery(self) -> None:
        """Synchronous teardown (receiver exit, force_close); cleanup() awaits what this cancels."""
        self._cancel_response_watchdog()
        for task in self._recovery_tasks:
            task.cancel()

    def _is_retry_rejection(self, event) -> bool:
        """An ERROR answering the pending retry's own response.create (matched by event_id)."""
        if self._watched_response_id is not _PENDING_RETRY or self._retry_event_id is None:
            return False
        error = getattr(event, "error", None)
        event_id = error.get("event_id") if isinstance(error, dict) else getattr(error, "event_id", None)
        return event_id == self._retry_event_id

    def _is_timed_out(self, event) -> bool:
        response_id = getattr(event, "response_id", None)
        return response_id is not None and response_id in self._timed_out_response_ids

    def _note_response_progress(self, event_type, event) -> None:
        """Any response.* event for the watched response shows it is alive: push the deadline out.

        A tool-call event stretches the deadline to TOOL_CALL_TIMEOUT_SECONDS, and it stays stretched
        (ordinary events, e.g. interim filler audio, can only extend it) until the tool call ends.
        """
        if self._watchdog_task is None or self._watched_response_id is _PENDING_RETRY:
            return  # nothing to credit, or the retry has not started: progress belongs to the old one
        name = getattr(event_type, "value", event_type)
        if not isinstance(name, str) or not name.startswith("response."):
            return
        if name in (ServerEventType.RESPONSE_CREATED.value, ServerEventType.RESPONSE_DONE.value):
            return
        # Some progress events (e.g. mcp_call.in_progress) carry no response_id; with one response
        # at a time they can only belong to the watched one.
        response_id = getattr(event, "response_id", None)
        watched = self._watched_response_id
        if response_id is not None and watched is not None and response_id != watched:
            return
        now = asyncio.get_running_loop().time()
        if name in _TOOL_CALL_EVENTS:
            self._tool_call_open = True
            self._watchdog_deadline = now + TOOL_CALL_TIMEOUT_SECONDS
        elif self._tool_call_open and name not in _TOOL_CALL_END_EVENTS:
            self._watchdog_deadline = max(self._watchdog_deadline, now + RESPONSE_TIMEOUT_SECONDS)
        else:
            self._tool_call_open = False
            self._watchdog_deadline = now + RESPONSE_TIMEOUT_SECONDS

    async def _response_watchdog(self, response_id) -> None:
        loop = asyncio.get_running_loop()
        # Sleep in steps of at most RESPONSE_TIMEOUT_SECONDS: a progress event can pull a stretched
        # tool-call deadline back in, and the new deadline is never earlier than the end of a step.
        while (remaining := self._watchdog_deadline - loop.time()) > 0:
            await asyncio.sleep(min(remaining, RESPONSE_TIMEOUT_SECONDS))
        me = asyncio.current_task()
        if self._watchdog_task is not me:
            return  # replaced or disarmed while waking up: the other side already claimed it
        # Claim the response for the timer (no await until the claim is recorded).
        self._watchdog_task = None  # detach first, so the cancel below does not cancel this task
        self._cancel_response_watchdog()
        if response_id is not None and response_id is not _PENDING_RETRY:
            self._timed_out_response_ids.append(response_id)
        self._recovery_tasks.add(me)
        await self._on_response_stuck(response_id, reason="no_progress")

    def _on_retry_rejected(self) -> None:
        """Voice Live answered the timed-out response's retry with an error instead of starting it:
        claim the pending-retry watch now and run the same recovery the timer would (next attempt,
        which ends the call once MAX_CONSECUTIVE_FAILED_RESPONSES is reached)."""
        self._cancel_response_watchdog()
        task = asyncio.create_task(self._on_response_stuck(_PENDING_RETRY, reason="retry_rejected"))
        self._recovery_tasks.add(task)
        task.add_done_callback(self._on_watchdog_task_done)

    def _on_watchdog_task_done(self, task: asyncio.Task) -> None:
        self._recovery_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()  # retrieved, so it is never reported as "never retrieved"
        if exc is not None:
            logger.error("[VoiceLive] response watchdog raised %s", self.log_context,
                         exc_info=(type(exc), exc, exc.__traceback__))

    async def _on_response_stuck(self, response_id, *, reason: str) -> None:
        """D-050: a response with no progress in time (reason=no_progress), or a timed-out
        response's retry that Voice Live rejected (reason=retry_rejected), counts as a failed response."""
        self._consecutive_failed_responses += 1
        attempt = self._consecutive_failed_responses
        shown_id = "pending_retry" if response_id is _PENDING_RETRY else response_id
        logger.warning(
            "[VoiceLive] response_stuck id=%s reason=%s attempt=%d %s",
            shown_id, reason, attempt, self.log_context,
        )
        if attempt < MAX_CONSECUTIVE_FAILED_RESPONSES and response_id not in (None, _PENDING_RETRY):
            await self._cancel_stuck_response(response_id)
        await self._retry_or_end_call(attempt, watch_retry=True)

    async def _cancel_stuck_response(self, response_id) -> None:
        """Best effort: ask Voice Live to drop the stuck response, and give it up to
        CANCEL_ACK_TIMEOUT_SECONDS to confirm (its response.done), so the retry is not rejected
        because the old response still looks active. That response.done is otherwise ignored (the
        id is in _timed_out_response_ids). No confirmation is fine: the retry goes ahead anyway.
        """
        ack = asyncio.Event()
        self._cancel_ack = (response_id, ack)
        try:
            await self.conn.response.cancel(response_id=response_id)
            await asyncio.wait_for(ack.wait(), CANCEL_ACK_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.info("[VoiceLive] response_stuck_cancel_unconfirmed id=%s %s", response_id, self.log_context)
        except Exception as exc:
            logger.warning(
                "[VoiceLive] response_stuck_cancel_failed id=%s error=%s %s",
                response_id, type(exc).__name__, self.log_context,
            )
        finally:
            if self._cancel_ack is not None and self._cancel_ack[1] is ack:
                self._cancel_ack = None

    async def _stop_response_watchdog_tasks(self) -> None:
        """Cancel and await the timer and any running recovery (cleanup path).

        Loops because a recovery cancelled mid-retry may have armed a pending-retry timer.
        """
        me = asyncio.current_task()
        while True:
            tasks = [
                t for t in (self._watchdog_task, *self._recovery_tasks)
                if t is not None and t is not me and not t.done()
            ]
            self._cancel_response_watchdog()
            if not tasks:
                break
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        # Everything left is finished (its done-callback may not have run yet) or is this task.
        self._recovery_tasks.intersection_update({me})

    async def on_response_unrecoverable(self):
        """Voice Live responses keep failing: end the call. No-op for the web client; telephony
        subclasses override with their request_end() path (D-005). Must not block."""
        return None

    async def on_voicelive_ended(self):
        """Voice Live dropped unexpectedly: close the client WebSocket so the caller-side loop exits."""
        if self.client_ws:
            try:
                logger.warning("[VoiceLive] Voice Live disconnected — closing client WebSocket")
                await self.client_ws.close(1001)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Client WebSocket
    # ------------------------------------------------------------------

    async def init_websocket(self, socket):
        """Sets up the client WebSocket."""
        self.client_ws = socket

    async def send_message(self, message: Data):
        """Sends data back to client WebSocket."""
        try:
            await self.client_ws.send(message)
        except Exception:
            logger.exception("[VoiceLive] Failed to send message to client")

    # ------------------------------------------------------------------
    # Hooks — web client implementations (override in telephony subclasses)
    # ------------------------------------------------------------------

    async def on_speech_started(self):
        """Barge-in: send StopAudio to client and clear TTS buffer."""
        stop_audio_data = {"Kind": "StopAudio", "AudioData": None, "StopAudio": {}}
        await self.send_message(json.dumps(stop_audio_data))

        if self._ambient_mixer is not None:
            async with self._tts_buffer_lock:
                self._tts_output_buffer.clear()
                self._tts_playback_started = False

    async def on_audio_delta(self, audio_bytes: bytes):
        """Handle audio from Voice Live — buffer for ambient or send directly."""
        if not self._forward_agent_audio:
            return
        if self._ambient_mixer is not None and self._ambient_mixer.is_enabled():
            async with self._tts_buffer_lock:
                self._tts_output_buffer.extend(audio_bytes)
                if len(self._tts_output_buffer) > self._max_buffer_size:
                    if not self._buffer_warning_logged:
                        logger.warning(
                            f"TTS buffer large: {len(self._tts_output_buffer)} bytes. "
                            "Speech may be delayed but will not be cut."
                        )
                        self._buffer_warning_logged = True
                elif self._buffer_warning_logged and len(self._tts_output_buffer) < self._max_buffer_size // 2:
                    self._buffer_warning_logged = False
        else:
            await self._send_audio_to_client(audio_bytes)

    async def on_transcript_done(self, transcript: str):
        """Forward transcript to client."""
        await self.send_message(
            json.dumps({"Kind": "Transcription", "Text": transcript})
        )

    # ------------------------------------------------------------------
    # Lifecycle hooks — no-ops here; telephony subclasses override
    # ------------------------------------------------------------------

    async def on_call_cap(self):
        return None

    async def on_idle(self):
        return None

    def stop_forwarding_agent_audio(self) -> None:
        """Stop sending further agent audio to the client and drop what's already buffered."""
        self._forward_agent_audio = False
        self._tts_output_buffer.clear()
        self._tts_playback_started = False

    def force_close(self) -> None:
        """Abort the Voice Live socket without waiting for a close handshake."""
        self._cancel_watchdog_and_recovery()
        if self._receiver_task:
            self._receiver_task.cancel()
        ws = getattr(self.conn, "_connection", None)
        response = getattr(ws, "_response", None)
        if response is not None:
            response.close()
        self._voicelive_connected = False

    # ------------------------------------------------------------------
    # Audio output to client
    # ------------------------------------------------------------------

    async def _send_audio_to_client(self, audio_bytes: bytes):
        """Send audio bytes to the client. Override in subclasses for wrapping."""
        await self.send_message(audio_bytes)

    # ------------------------------------------------------------------
    # Inbound audio from client
    # ------------------------------------------------------------------

    def _receive_audio_from_client(self, data) -> tuple:
        """Convert client audio to PCM 24kHz. Override for format conversion.

        Returns (pcm_bytes | None, chunk_size). Return None for silent frames.
        """
        return data, len(data)

    async def on_message(self, msg):
        """Process one incoming WebSocket message. Override in subclasses for protocol handling."""
        await self.handle_audio(msg)

    async def handle_audio(self, data):
        """Process inbound audio: convert, mix ambient, forward to Voice Live."""
        pcm_bytes, chunk_size = self._receive_audio_from_client(data)
        await self._send_continuous_audio(chunk_size)
        if pcm_bytes:
            audio_b64 = base64.b64encode(pcm_bytes).decode("ascii")
            await self.send_audio(audio_b64)

    # ------------------------------------------------------------------
    # Ambient mixing
    # ------------------------------------------------------------------

    async def _send_continuous_audio(self, chunk_size: int) -> None:
        """Send continuous audio (ambient + TTS if available) back to client."""
        if self._ambient_mixer is None or not self._ambient_mixer.is_enabled():
            return

        try:
            async with self._tts_buffer_lock:
                buffer_len = len(self._tts_output_buffer)
                ambient_bytes = self._ambient_mixer.get_ambient_only_chunk(chunk_size)

                should_play_tts = False
                if self._tts_playback_started:
                    if buffer_len >= chunk_size:
                        should_play_tts = True
                    elif buffer_len > 0:
                        should_play_tts = True
                    else:
                        self._tts_playback_started = False
                else:
                    if buffer_len >= self._min_buffer_to_start:
                        self._tts_playback_started = True
                        should_play_tts = True

                if should_play_tts and buffer_len >= chunk_size:
                    tts_chunk = bytes(self._tts_output_buffer[:chunk_size])
                    del self._tts_output_buffer[:chunk_size]

                    ambient = np.frombuffer(ambient_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                    tts = np.frombuffer(tts_chunk, dtype=np.int16).astype(np.float32) / 32768.0
                    mixed = np.clip(ambient + tts, -0.95, 0.95)
                    output_bytes = (mixed * 32767).astype(np.int16).tobytes()

                elif should_play_tts and buffer_len > 0:
                    tts_chunk = bytes(self._tts_output_buffer[:])
                    self._tts_output_buffer.clear()
                    self._tts_playback_started = False

                    ambient = np.frombuffer(ambient_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                    tts_samples = len(tts_chunk) // 2
                    tts = np.frombuffer(tts_chunk, dtype=np.int16).astype(np.float32) / 32768.0
                    ambient[:tts_samples] += tts
                    mixed = np.clip(ambient, -0.95, 0.95)
                    output_bytes = (mixed * 32767).astype(np.int16).tobytes()

                else:
                    output_bytes = ambient_bytes

            await self._send_audio_to_client(output_bytes)

        except Exception:
            logger.exception("[VoiceLive] Error in _send_continuous_audio")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def cleanup(self):
        """Cancel background tasks and close the Voice Live connection."""
        if self._receiver_task:
            self._receiver_task.cancel()
            try:
                await self._receiver_task
            except (asyncio.CancelledError, Exception):
                pass
            self._receiver_task = None
        # After the receiver stops (nothing can arm a new timer), before the connection closes.
        await self._stop_response_watchdog_tasks()
        if self._conn_ctx:
            try:
                await self._conn_ctx.__aexit__(None, None, None)
            except Exception:
                pass
            self._conn_ctx = None
            self.conn = None
        if self._credential:
            try:
                await self._credential.close()
            except Exception:
                pass
            self._credential = None
        logger.info("[VoiceLive] Cleaned up")
