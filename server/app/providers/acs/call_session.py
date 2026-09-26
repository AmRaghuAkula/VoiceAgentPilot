"""Per-call state for ACS calls. Every end path goes through CallSession.request_end()."""

import asyncio
import logging
import time
from dataclasses import dataclass

from azure.communication.callautomation import TextSource
from azure.core.exceptions import HttpResponseError

from app.routing import AgentRoute

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionSettings:
    fallback_message: str
    goodbye_message: str
    tts_voice: str
    media_connect_timeout: float = 10.0
    voice_live_connect_timeout: float = 8.0
    media_lost_grace: float = 5.0
    wait_timeout: float = 5.0
    close_timeout: float = 5.0
    play_safety_timeout: float = 15.0


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


class CallSessionRegistry:
    def __init__(self):
        self._by_key: dict[str, "CallSession"] = {}
        self._by_conn: dict[str, str] = {}

    def __len__(self) -> int:
        return len(self._by_key)

    def add(self, session: "CallSession") -> None:
        self._by_key[session.call_key] = session

    def index_connection(self, session: "CallSession") -> None:
        if session.call_connection_id:
            self._by_conn[session.call_connection_id] = session.call_key

    def get(self, call_key: str) -> "CallSession | None":
        return self._by_key.get(call_key)

    def get_by_connection(self, call_connection_id: str) -> "CallSession | None":
        key = self._by_conn.get(call_connection_id)
        return self._by_key.get(key) if key else None

    def remove(self, session: "CallSession") -> None:
        if self._by_key.get(session.call_key) is session:
            del self._by_key[session.call_key]
        conn = session.call_connection_id
        if conn and self._by_conn.get(conn) == session.call_key:
            del self._by_conn[conn]

    def sweep(self, max_age: float, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        stale = [s for s in self._by_key.values() if now - s.created_at > max_age]
        for session in stale:
            session.request_end("stale", None)
            session.force_hang_up()
            self.remove(session)
        return len(stale)


class CallSession:
    def __init__(self, *, call_key: str, route: AgentRoute | None, masked_caller: str, masked_called: str,
                 settings: SessionSettings, acs_client, registry: CallSessionRegistry):
        self.call_key = call_key
        self.route = route
        self.masked_caller = masked_caller
        self.masked_called = masked_called
        self.settings = settings
        self._acs_client = acs_client
        self._registry = registry

        self.call_connection_id: str | None = None
        self.handler = None
        self.ws_used = False
        self.terminated_reason: str | None = None
        self.ended_at: float | None = None
        self.disconnected = False
        self.hangup_after_play = False
        self.created_at = time.monotonic()

        self._hung_up = False
        self._hangup_pending_answer = False
        self._answered = asyncio.Event()
        self._connected = asyncio.Event()
        self._timers: set[asyncio.Task] = set()
        self._safety_timer: asyncio.Task | None = None
        self._close_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()

    @property
    def log_context(self) -> str:
        return f"call_key={self.call_key[:8]} conn={self.call_connection_id}"

    # --- task helpers -------------------------------------------------

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _start_timer(self, delay: float, action) -> None:
        async def _run():
            await asyncio.sleep(delay)
            action()

        task = self._spawn(_run())
        self._timers.add(task)
        task.add_done_callback(self._timers.discard)

    def _cancel_timers(self) -> None:
        for task in list(self._timers):
            task.cancel()
        self._timers.clear()

    # --- ACS lifecycle inputs ------------------------------------------

    def set_answered(self, call_connection_id: str) -> None:
        self.call_connection_id = call_connection_id
        self._registry.index_connection(self)
        self._answered.set()
        if self._hangup_pending_answer:
            # #D: an earlier _terminate() attempt already gave up on hanging up
            # because call_connection_id wasn't known yet. Now that we have it,
            # finish the job so the caller isn't left in silence indefinitely.
            self._hangup_pending_answer = False
            if not self.disconnected and not self._hung_up:
                self._spawn(self._safe_hang_up())
            return
        if self.terminated_reason is not None:
            return
        if self.route is not None:
            self._start_timer(self.settings.media_connect_timeout, self._media_watchdog_fired)

    def mark_connected(self) -> None:
        self._connected.set()
        if self.route is None:
            self.request_end("route_miss", self.settings.fallback_message)

    def mark_answer_failed(self) -> None:
        if self.terminated_reason is None:
            self.terminated_reason = "answer_failed"
            self.ended_at = time.monotonic()
            logger.error(
                "call_ended reason=answer_failed caller=%s called=%s %s",
                self.masked_caller, self.masked_called, self.log_context,
            )
        self._cancel_timers()
        self._registry.remove(self)

    def on_media_ws_closed(self) -> None:
        if self.terminated_reason is None and not self.disconnected:
            self._start_timer(self.settings.media_lost_grace, self._media_grace_expired)

    def on_call_disconnected(self) -> None:
        self.disconnected = True
        self.request_end("caller_hangup", None)
        self._cancel_timers()
        self._cancel_safety_timer()
        self._spawn(self._finalize())

    def on_play_done(self, failed: bool) -> None:
        if failed:
            logger.warning("play_failed %s", self.log_context)
        if self.hangup_after_play:
            self._cancel_safety_timer()
            self._spawn(self._safe_hang_up())

    def _cancel_safety_timer(self) -> None:
        # #A: cancelling the timer while _safe_hang_up() is already awaiting the
        # underlying HTTP call would inject CancelledError into that await and
        # drop the request on the floor (the caller stays connected). Once
        # _hung_up is set the request is in flight (or done); only cancel the
        # timer while it is still merely sleeping.
        if self._safety_timer is not None and not self._hung_up:
            self._safety_timer.cancel()

    # --- timers -------------------------------------------------------

    def _media_watchdog_fired(self) -> None:
        if self.terminated_reason is None and not self.ws_used:
            self.request_end("media_timeout", self.settings.fallback_message)

    def _media_grace_expired(self) -> None:
        if self.terminated_reason is None and not self.disconnected:
            self.request_end("media_lost", self.settings.fallback_message)

    async def _play_safety_timeout(self) -> None:
        await asyncio.sleep(self.settings.play_safety_timeout)
        if self.disconnected or self._registry.get(self.call_key) is not self:
            return
        logger.warning("hangup_timeout %s", self.log_context)
        await self._safe_hang_up()

    # --- ending -------------------------------------------------------

    def request_end(self, reason: str, message: str | None) -> None:
        if self.terminated_reason is not None:
            return
        self.terminated_reason = reason
        self.ended_at = time.monotonic()
        self._spawn(self._terminate(message))

    def ensure_voicelive_closed(self) -> asyncio.Task | None:
        if self._close_task is None and self.handler is not None:
            self._close_task = self._spawn(self._close_and_log())
        return self._close_task

    async def _close_and_log(self) -> None:
        await close_voicelive(self.handler, self.settings.close_timeout, self.log_context)
        since = self.ended_at if self.ended_at is not None else time.monotonic()
        logger.info("voicelive_closed_ms=%.0f %s", (time.monotonic() - since) * 1000, self.log_context)

    async def _wait(self, event: asyncio.Event) -> bool:
        try:
            await asyncio.wait_for(event.wait(), self.settings.wait_timeout)
            return True
        except TimeoutError:
            return False

    async def _terminate(self, message: str | None) -> None:
        try:
            logger.info(
                "call_ended reason=%s caller=%s called=%s %s",
                self.terminated_reason, self.masked_caller, self.masked_called, self.log_context,
            )
            self._cancel_timers()
            if self.handler is not None:
                self.handler.stop_forwarding_agent_audio()
                self.ensure_voicelive_closed()
            if self.disconnected:
                return
            if not message:
                await self._safe_hang_up()
                return
            ready = await self._wait(self._answered) and await self._wait(self._connected)
            if self.disconnected:
                return
            if not ready:
                await self._safe_hang_up()
                return
            self.hangup_after_play = True
            self._safety_timer = self._spawn(self._play_safety_timeout())
            try:
                await self._acs_client.get_call_connection(self.call_connection_id).play_media(
                    play_source=TextSource(text=message, voice_name=self.settings.tts_voice),
                    play_to="all",
                    operation_context=f"bridge-{self.terminated_reason}",
                )
            except Exception:
                logger.warning("play_media failed %s", self.log_context, exc_info=True)
                self._cancel_safety_timer()
                await self._safe_hang_up()
        except Exception:
            logger.exception("terminate failed %s", self.log_context)

    # Status codes meaning the call is already gone server-side, so the hang_up
    # is done in spirit even though it "failed" — nothing to retry.
    _TERMINAL_HANGUP_STATUSES = frozenset({404, 410})

    async def _safe_hang_up(self) -> None:
        if self.disconnected or self._hung_up:
            return
        if not self.call_connection_id:
            # #D: nothing to hang up yet — remember to retry once set_answered()
            # supplies a call_connection_id, so the caller isn't stranded.
            self._hangup_pending_answer = True
            return
        self._hung_up = True
        try:
            # #A: shield so that if this coroutine's own task gets cancelled
            # (e.g. by a stray timer cancel that races past _cancel_safety_timer's
            # guard), the underlying hang_up request is not dropped mid-flight.
            await asyncio.shield(
                self._acs_client.get_call_connection(self.call_connection_id).hang_up(is_for_everyone=True)
            )
        except HttpResponseError as exc:
            if exc.status_code in self._TERMINAL_HANGUP_STATUSES:
                logger.info("hang_up ignored status=%s %s", exc.status_code, self.log_context)
            else:
                # #B: a transient failure (5xx, 429, network-ish error surfaced as
                # HttpResponseError) must not be treated as "done" — clear the
                # flag so a later _safe_hang_up() call (safety timer, sweep,
                # another end path) can retry instead of silently no-op'ing
                # forever with the caller still connected.
                logger.warning("hang_up failed status=%s %s", exc.status_code, self.log_context, exc_info=True)
                self._hung_up = False
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("hang_up failed %s", self.log_context, exc_info=True)
            self._hung_up = False

    def force_hang_up(self) -> None:
        """Best-effort hang-up used by sweep(): fire-and-forget, never raises."""
        if not self.disconnected and not self._hung_up and self.call_connection_id:
            self._spawn(self._safe_hang_up())

    async def _finalize(self) -> None:
        try:
            task = self.ensure_voicelive_closed()
            if task is not None:
                await task
        finally:
            self._registry.remove(self)
