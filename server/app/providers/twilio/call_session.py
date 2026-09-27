"""Per-call end state for Twilio calls (UT01b). Every end path goes through TwilioCallSession.request_end().

Deliberately not ACS's CallSession: that class drives the ACS Call Automation REST API (play/hang-up),
which has no Twilio equivalent here. A Twilio call ends when the bridge closes the media-stream socket:
the TwiML has no verb after <Connect>, so Twilio then hangs up.

Ownership split (keep it this way, it is what prevents U11's mid-connect leak):
  * request_end never touches Voice Live; the only close is /twilio/ws's finally.
  * request_end only records the reason, stops agent audio, schedules the Twilio socket close, and
    wakes the call loop (receive() raises CallEnded).
  * /twilio/ws's finally calls close_voicelive() once, after run_call_loop has already cancelled and
    awaited the connect task, so a Voice Live connection can never complete after cleanup has run.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

_WS_CLOSE_CODE = 1000


class CallEnded(Exception):
    """Raised by TwilioCallSession.receive() once request_end() has run, so run_call_loop exits."""


class TwilioCallSession:
    def __init__(self, *, handler, twilio_ws, fallback_message: str, goodbye_message: str,
                 voice_live_connect_timeout: float, log_context: str):
        self.handler = handler
        self.fallback_message = fallback_message
        self.goodbye_message = goodbye_message
        self.voice_live_connect_timeout = voice_live_connect_timeout
        self.log_context = log_context
        self.terminated_reason: str | None = None
        self._ws = twilio_ws
        self._ended = asyncio.Event()
        self._close_task: asyncio.Task | None = None
        self._pending_recv: asyncio.Future | None = None

    def request_end(self, reason: str, message: str | None) -> None:
        """End the call. Synchronous and idempotent: the first reason wins (D-005).

        `message` is accepted for interface parity with ACS's CallSession but is not spoken: this pilot
        closes the Twilio call silently.
        """
        if self.terminated_reason is not None:
            return
        self.terminated_reason = reason
        self._ended.set()
        logger.info("call_ended reason=%s %s", reason, self.log_context)
        try:
            self.handler.stop_forwarding_agent_audio()
        except Exception:
            logger.exception("stop_forwarding_agent_audio failed %s", self.log_context)
        self._close_task = asyncio.get_running_loop().create_task(self._close_ws())

    async def _close_ws(self) -> None:
        try:
            await self._ws.close(_WS_CLOSE_CODE)
        except Exception:
            # Already closed (e.g. the caller hung up first): nothing left to do.
            logger.debug("twilio ws close skipped %s", self.log_context, exc_info=True)

    async def wait_closed(self, timeout: float) -> None:
        """Give a scheduled Twilio socket close a bounded chance to finish. Never raises."""
        if self._close_task is not None:
            await asyncio.wait([self._close_task], timeout=timeout)

    async def receive(self):
        """Receive the next Twilio message, or raise CallEnded as soon as request_end() has run.

        run_call_loop reads from this instead of the raw socket. Closing the socket from the server side
        does not wake a pending Quart receive(), so without this the loop would keep waiting until the
        ASGI server reported the disconnect or the idle timeout fired.
        """
        if self._ended.is_set():
            raise CallEnded
        recv = self._pending_recv or asyncio.ensure_future(self._ws.receive())
        self._pending_recv = None
        ended = asyncio.ensure_future(self._ended.wait())
        try:
            await asyncio.wait({recv, ended}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            # Our caller gave up (run_call_loop's receive timeout). If a message landed in that same
            # instant, keep it (or the socket error) for the next receive() instead of dropping it.
            if recv.done() and not recv.cancelled():
                self._pending_recv = recv
            raise
        finally:
            ended.cancel()
            if not recv.done():
                recv.cancel()
        if self._ended.is_set():
            if recv.done() and not recv.cancelled():
                recv.exception()  # mark any socket error as retrieved; the call is ending anyway
            raise CallEnded
        return recv.result()
