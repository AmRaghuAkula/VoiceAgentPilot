"""Answers ACS IncomingCall events and dispatches Call Automation callbacks to CallSessions."""

import logging
import uuid
from urllib.parse import urlparse

from azure.communication.callautomation import (
    AudioFormat,
    MediaStreamingAudioChannelType,
    MediaStreamingContentType,
    MediaStreamingOptions,
    StreamingTransportType,
)

from app.bridge_config import BridgeConfig
from app.log_mask import mask_number
from app.providers.acs.call_session import CallSession, CallSessionRegistry, SessionSettings
from app.providers.acs.signing import sign
from app.routing import called_number_from_event, caller_number_from_event, resolve_route

logger = logging.getLogger(__name__)

VALIDATION_EVENT = "Microsoft.EventGrid.SubscriptionValidationEvent"
INCOMING_CALL_EVENT = "Microsoft.Communication.IncomingCall"


def settings_from_bridge(bridge: BridgeConfig) -> SessionSettings:
    return SessionSettings(
        fallback_message=bridge.fallback_message,
        goodbye_message=bridge.goodbye_message,
        tts_voice=bridge.tts_voice,
        media_connect_timeout=bridge.media_connect_timeout,
        voice_live_connect_timeout=bridge.voice_live_connect_timeout,
        media_lost_grace=bridge.media_lost_grace,
    )


class BridgeCallController:
    def __init__(self, *, acs_client, bridge: BridgeConfig, registry: CallSessionRegistry,
                 public_base_url_override: str = ""):
        self.acs_client = acs_client
        self.bridge = bridge
        self.registry = registry
        self.public_base_url_override = public_base_url_override
        self.settings = settings_from_bridge(bridge)

    async def handle_incoming(self, events: list | None, host_url: str) -> tuple[dict | str, int]:
        for event in events or []:
            event_type = event.get("eventType")
            if event_type == VALIDATION_EVENT:
                return {"validationResponse": event["data"]["validationCode"]}, 200
            if event_type == INCOMING_CALL_EVENT:
                return await self._answer(event.get("data") or {}, host_url)
        return "", 400

    async def _answer(self, data: dict, host_url: str) -> tuple[str, int]:
        context = data.get("incomingCallContext")
        called = called_number_from_event(data)
        caller = caller_number_from_event(data)
        if not context:
            logger.warning("incoming_call missing incomingCallContext called=%s", mask_number(called))
            return "", 400

        route = resolve_route(self.bridge.routes, called)
        session = CallSession(
            call_key=uuid.uuid4().hex,
            route=route,
            masked_caller=mask_number(caller),
            masked_called=mask_number(called),
            settings=self.settings,
            acs_client=self.acs_client,
            registry=self.registry,
        )
        self.registry.add(session)

        base = (self.public_base_url_override or host_url).rstrip("/")
        secret = self.bridge.media_ws_token
        kwargs = {
            "incoming_call_context": context,
            "callback_url": f"{base}/acs/callbacks/{session.call_key}/{sign(secret, 'cb', session.call_key)}",
            "cognitive_services_endpoint": self.bridge.acs_cognitive_services_endpoint,
            "operation_context": "bridge",
        }
        if route is not None:
            netloc = urlparse(base).netloc
            kwargs["media_streaming"] = MediaStreamingOptions(
                transport_url=f"wss://{netloc}/acs/ws/{session.call_key}/{sign(secret, 'ws', session.call_key)}",
                transport_type=StreamingTransportType.WEBSOCKET,
                content_type=MediaStreamingContentType.AUDIO,
                audio_channel_type=MediaStreamingAudioChannelType.MIXED,
                start_media_streaming=True,
                enable_bidirectional=True,
                audio_format=AudioFormat.PCM24_K_MONO,
            )

        logger.info(
            "incoming_call route=%s caller=%s called=%s %s",
            "hit" if route else "miss", session.masked_caller, session.masked_called, session.log_context,
        )
        try:
            result = await self.acs_client.answer_call(**kwargs)
        except Exception:
            logger.exception("answer_call failed %s", session.log_context)
            session.mark_answer_failed()
            return "", 200
        session.set_answered(result.call_connection_id)
        return "", 200

    def handle_callbacks(self, call_key: str, events: list | None) -> None:
        session = self.registry.get(call_key)
        if session is None:
            logger.info("callback for unknown call_key=%s", call_key[:8])
            return
        for event in events or []:
            event_type = (event or {}).get("type", "")
            if event_type == "Microsoft.Communication.CallConnected":
                session.mark_connected()
            elif event_type == "Microsoft.Communication.CallDisconnected":
                session.on_call_disconnected()
            elif event_type == "Microsoft.Communication.PlayCompleted":
                session.on_play_done(failed=False)
            elif event_type == "Microsoft.Communication.PlayFailed":
                session.on_play_done(failed=True)
            elif event_type == "Microsoft.Communication.MediaStreamingFailed":
                logger.warning("media_streaming_failed %s", session.log_context)
