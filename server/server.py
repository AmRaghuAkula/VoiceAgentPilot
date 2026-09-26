import asyncio
import logging
import os
import sys

from dotenv import load_dotenv
from quart import Quart, request, websocket

from app.bridge_config import BridgeConfigError, load_bridge_config
from app.call_loop import run_call_loop
from app.call_manager import CallManager
from app.config_validator import validate_config
from app.handler.voicelive_media_handler import VoiceLiveMediaHandler
from app.logging_config import configure_logging, new_correlation_id
from app.provider_registry import detect_provider, get_configured_providers, get_provider

load_dotenv()

# ---------------------------------------------------------------------------
# Structured logging (with correlation ID support)
# ---------------------------------------------------------------------------

_debug = os.getenv("DEBUG_MODE", "false").lower() == "true"
configure_logging(level=logging.DEBUG if _debug else logging.INFO)
logger = logging.getLogger(__name__)

try:
    bridge = load_bridge_config(os.environ, acs_active=bool(os.getenv("ACS_CONNECTION_STRING")))
except BridgeConfigError as exc:
    logger.error("Bridge configuration error: %s", exc)
    sys.exit(1)

# ---------------------------------------------------------------------------
# App configuration
# ---------------------------------------------------------------------------

app = Quart(__name__, static_folder="static" if bridge.enable_web_client else None)
app.config["AZURE_VOICE_LIVE_API_KEY"] = os.getenv("AZURE_VOICE_LIVE_API_KEY", "")
app.config["AZURE_VOICE_LIVE_ENDPOINT"] = bridge.voice_live_endpoint
app.config["BRIDGE"] = bridge
app.config["VOICE_LIVE_API_VERSION"] = bridge.voice_live_api_version
app.config["INTERIM_RESPONSE"] = bridge.interim_response
app.config["VOICE_LIVE_MODEL"] = os.getenv("VOICE_LIVE_MODEL", "gpt-4o-mini")
app.config["AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID"] = os.getenv(
    "AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID", ""
)
app.config["AMBIENT_PRESET"] = os.getenv("AMBIENT_PRESET", "none")

# Log ambient configuration on startup
ambient_preset = app.config["AMBIENT_PRESET"]
if ambient_preset and ambient_preset != "none":
    logger.info("Ambient scenes ENABLED: preset='%s'", ambient_preset)
else:
    logger.info("Ambient scenes DISABLED (preset=none)")

# ---------------------------------------------------------------------------
# Call manager (concurrency limits + timeout enforcement)
# ---------------------------------------------------------------------------

call_manager = CallManager(
    max_concurrent=int(os.getenv("MAX_CONCURRENT_CALLS", "50")),
    max_duration=bridge.max_call_seconds,
    idle_timeout=int(os.getenv("CALL_IDLE_TIMEOUT", "120")),
)

# ---------------------------------------------------------------------------
# Telephony provider detection and validation
# ---------------------------------------------------------------------------

# Import all provider packages to populate the detection registry.
# Only lightweight registration happens here (no heavy SDK imports until
# register_routes is called — each provider defers handler imports).
import importlib
import pkgutil

_providers_pkg = importlib.import_module("app.providers")
for _finder, _mod_name, _ispkg in pkgutil.iter_modules(_providers_pkg.__path__):
    if _ispkg:  # each provider is a package (folder with __init__.py)
        try:
            importlib.import_module(f"app.providers.{_mod_name}")
        except ImportError as e:
            logger.debug("Skipping provider %s: %s", _mod_name, e)

_telephony_client = detect_provider()

# D-028: the web debug client (/web/ws, unauthenticated) and a real telephony provider must
# never both be active. Fail startup rather than just warn (supersedes the warn-only behavior
# D-007/D-024/D-025 shipped; answers Q-007).
if bridge.enable_web_client and _telephony_client:
    logger.error(
        "Refusing to start: ENABLE_WEB_CLIENT=true and telephony provider '%s' are both "
        "active. The unauthenticated web debug client must never run alongside a real "
        "telephony provider (D-028). Disable one of the two and restart.",
        _telephony_client,
    )
    sys.exit(1)

# Warn if multiple telephony credentials are set — only one can be active
_configured_providers = get_configured_providers()
if len(_configured_providers) > 1:
    logger.warning(
        "Multiple telephony credentials detected: %s. Using '%s' (first match). "
        "Remove unused credentials to avoid confusion.",
        ", ".join(_configured_providers),
        _telephony_client,
    )

if _telephony_client:
    logger.info("Telephony provider: %s", _telephony_client)
else:
    logger.info("No telephony provider credentials found")

# Validate configuration — returns False if telephony provider is misconfigured
_provider_ready = validate_config(app.config, _telephony_client)

# Register telephony routes only if the provider is fully configured
_provider_info = get_provider(_telephony_client) if _telephony_client else None
if _provider_ready and _provider_info:
    _provider_info.register_routes(app, call_manager)
    logger.info("Registered routes for provider: %s", _provider_info.display_name)
else:
    logger.warning(
        "Telephony routes not registered — web client %s",
        "available" if bridge.enable_web_client else "disabled",
    )

# ---------------------------------------------------------------------------
# Routes: Web client (only when bridge.enable_web_client is true — D-007)
# ---------------------------------------------------------------------------


async def web_ws():
    """WebSocket endpoint for web clients to send audio to Voice Live."""
    cid = new_correlation_id()
    logger.info("Incoming Web WebSocket connection")

    call_id = cid
    if not await call_manager.acquire(call_id, "web"):
        await websocket.close(4429, "Too Many Connections")
        return

    handler = VoiceLiveMediaHandler(app.config)
    await handler.init_websocket(websocket)
    try:
        await run_call_loop(
            call_manager=call_manager,
            call_id=call_id,
            ws=websocket,
            handler=handler,
        )
    except asyncio.CancelledError:
        logger.info("Web WebSocket cancelled")
    except Exception:
        logger.exception("Web WebSocket connection closed")
    finally:
        await call_manager.release(call_id)
        await handler.cleanup()


async def index():
    """Serves the static index page."""
    return await app.send_static_file("index.html")


if bridge.enable_web_client:
    app.websocket("/web/ws")(web_ws)
    app.route("/")(index)
    logger.warning("Web debug client ENABLED (/web/ws is unauthenticated; local use only)")


@app.route("/health")
async def health():
    """Liveness/readiness probe endpoint."""
    return {"status": "healthy"}, 200


if __name__ == "__main__":
    app.run(debug=_debug, host="0.0.0.0", port=8000)
