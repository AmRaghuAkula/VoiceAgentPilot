"""Provider-neutral bounded Voice Live close.

Lives here, not in providers/acs/call_session.py, so a Twilio-only code path can use it without
importing the ACS SDK (UT01b). providers/acs/call_session.py re-exports it for ACS callers.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)


async def close_voicelive(handler, timeout: float, log_context: str) -> None:
    cleanup = asyncio.ensure_future(handler.cleanup())
    try:
        await asyncio.wait_for(asyncio.shield(cleanup), timeout)
    except TimeoutError:
        logger.warning("voicelive_force_closed %s", log_context)
        try:
            handler.force_close()
        except Exception:
            logger.exception("force_close failed %s", log_context)
    except Exception:
        logger.exception("voicelive cleanup failed %s", log_context)
