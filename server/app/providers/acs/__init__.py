"""ACS (Azure Communication Services) provider route registration."""

import asyncio
import contextlib
import logging

from quart import request, websocket

from app.call_loop import run_call_loop
from app.call_manager import CallManager
from app.logging_config import new_correlation_id
from app.provider_registry import register_provider

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 60


@register_provider(
    name="acs",
    display_name="Azure Communication Services",
    detect_key="ACS_CONNECTION_STRING",
    required_config=["ACS_CONNECTION_STRING"],
)
def register_acs_routes(app, call_manager: CallManager):
    """Register ACS webhook and WebSocket routes."""
    import os

    from azure.communication.callautomation.aio import CallAutomationClient

    from app.providers.acs.bridge_calls import BridgeCallController
    from app.providers.acs.call_session import CallSessionRegistry
    from app.providers.acs.callback_auth import CallbackJwtVerifier
    from app.providers.acs.media_handler import ACSMediaHandler
    from app.providers.acs.signing import verify

    app.config["ACS_CONNECTION_STRING"] = os.getenv("ACS_CONNECTION_STRING")
    # ACS_DEV_TUNNEL: local dev only — overrides the public base URL for devtunnel/ngrok.
    app.config["ACS_DEV_TUNNEL"] = os.getenv("ACS_DEV_TUNNEL", "")

    bridge = app.config["BRIDGE"]
    registry = CallSessionRegistry()
    app.config["ACS_CALL_REGISTRY"] = registry
    acs_client = CallAutomationClient.from_connection_string(app.config["ACS_CONNECTION_STRING"])
    controller = BridgeCallController(
        acs_client=acs_client, bridge=bridge, registry=registry,
        public_base_url_override=app.config["ACS_DEV_TUNNEL"],
    )
    jwt_verifier = CallbackJwtVerifier(bridge.callback_jwt_audience) if bridge.callback_jwt_audience else None
    if jwt_verifier is None:
        logger.warning("ACS callback JWT check disabled (ACS_CALLBACK_JWT_AUDIENCE unset); signed URLs only")

    @app.route("/acs/incomingcall", methods=["POST"])
    async def incoming_call_handler():
        new_correlation_id()
        events = await request.get_json(silent=True)
        host_url = request.host_url.replace("http://", "https://", 1).rstrip("/")
        body, status = await controller.handle_incoming(events, host_url)
        return body, status

    @app.route("/acs/callbacks/<call_key>/<sig>", methods=["POST"])
    async def acs_event_callbacks(call_key, sig):
        new_correlation_id()
        if not verify(bridge.media_ws_token, "cb", call_key, sig):
            logger.warning("callback_rejected reason=bad_signature call_key=%s", call_key[:8])
            return "", 403
        if jwt_verifier is not None and not await jwt_verifier.verify(request.headers.get("Authorization")):
            logger.warning("callback_rejected reason=bad_jwt call_key=%s", call_key[:8])
            return "", 403
        controller.handle_callbacks(call_key, await request.get_json(silent=True))
        return "", 200

    @app.websocket("/acs/ws/<call_key>/<sig>")
    async def acs_ws(call_key, sig):
        new_correlation_id()
        session = registry.get(call_key)
        reason = None
        if session is None:
            reason = "unknown_call"
        elif not verify(bridge.media_ws_token, "ws", call_key, sig):
            reason = "bad_signature"
        elif session.ws_used:
            reason = "reused"
        elif session.terminated_reason is not None:
            reason = "terminated"
        if reason is not None:
            logger.warning("media_ws_rejected reason=%s call_key=%s", reason, call_key[:8])
            return "", 403

        session.ws_used = True
        if not await call_manager.acquire(call_key, "acs"):
            session.request_end("at_capacity", bridge.fallback_message)
            await websocket.close(4429, "Too Many Connections")
            return

        handler = ACSMediaHandler(app.config, session=session)
        session.handler = handler
        await handler.init_websocket(websocket)
        logger.info("media_ws_accepted %s", session.log_context)
        try:
            await run_call_loop(call_manager=call_manager, call_id=call_key, ws=websocket, handler=handler)
        except asyncio.CancelledError:
            logger.info("media_ws_closed %s", session.log_context)
        except Exception:
            logger.exception("media_ws_error %s", session.log_context)
        finally:
            await call_manager.release(call_key)
            session.on_media_ws_closed()
            session.ensure_voicelive_closed()

    async def _sweep_forever():
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            # One failing sweep must not kill the loop: stale calls would then never be hung up again.
            try:
                removed = registry.sweep(bridge.max_call_seconds + 120)
            except Exception:
                logger.exception("stale_call_sweep_failed")
                continue
            if removed:
                logger.warning("stale_calls_swept count=%d", removed)

    @app.before_serving
    async def _start_sweeper():
        app.config["ACS_SWEEPER"] = asyncio.get_running_loop().create_task(_sweep_forever())

    @app.after_serving
    async def _stop_sweeper():
        task = app.config.pop("ACS_SWEEPER", None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
