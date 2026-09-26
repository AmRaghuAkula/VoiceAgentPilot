import asyncio

import pytest
from azure.core.exceptions import HttpResponseError

from app.providers.acs.call_session import CallSession, CallSessionRegistry, SessionSettings
from app.routing import AgentRoute

ROUTE = AgentRoute("proj", "agent-a", "10")
FALLBACK = "fallback text"
GOODBYE = "goodbye text"
FAST = SessionSettings(
    fallback_message=FALLBACK,
    goodbye_message=GOODBYE,
    tts_voice="en-US-JennyNeural",
    media_connect_timeout=0.05,
    voice_live_connect_timeout=0.05,
    media_lost_grace=0.05,
    wait_timeout=0.05,
    close_timeout=0.05,
    play_safety_timeout=0.1,
)


class FakeHandler:
    def __init__(self, cleanup_delay=0.0):
        self.stopped = False
        self.cleanups = 0
        self.cleanup_done = 0
        self.forced = False
        self._delay = cleanup_delay

    def stop_forwarding_agent_audio(self):
        self.stopped = True

    async def cleanup(self):
        self.cleanups += 1
        await asyncio.sleep(self._delay)
        self.cleanup_done += 1

    def force_close(self):
        self.forced = True


def make(fake_acs, route=ROUTE, registry=None):
    registry = registry or CallSessionRegistry()
    session = CallSession(
        call_key="k" * 32, route=route, masked_caller="***9876", masked_called="***1234",
        settings=FAST, acs_client=fake_acs, registry=registry,
    )
    registry.add(session)
    return session, registry


async def settle(seconds=0.02):
    await asyncio.sleep(seconds)


def connected(session):
    session.set_answered("conn-1")
    session.mark_connected()


async def test_request_end_is_idempotent_first_reason_wins(fake_acs):
    session, _ = make(fake_acs)
    connected(session)
    session.ws_used = True
    assert session.request_end("call_cap", GOODBYE) is None
    session.request_end("idle", FALLBACK)
    await settle()
    assert session.terminated_reason == "call_cap"
    assert fake_acs.log == [("play", GOODBYE)]


async def test_media_watchdog_plays_fallback_and_hangs_up_after_play(fake_acs):
    session, _ = make(fake_acs)
    connected(session)
    await settle(0.1)
    assert session.terminated_reason == "media_timeout"
    assert fake_acs.log == [("play", FALLBACK)]
    session.on_play_done(failed=False)
    await settle()
    assert fake_acs.log == [("play", FALLBACK), ("hang_up", True)]


async def test_media_ws_before_call_connected_disarms_watchdog(fake_acs):
    session, _ = make(fake_acs)
    session.set_answered("conn-1")
    session.ws_used = True
    session.mark_connected()
    await settle(0.1)
    assert session.terminated_reason is None


async def test_caller_hangup_never_plays_and_removes_session(fake_acs):
    session, registry = make(fake_acs)
    connected(session)
    session.ws_used = True
    session.handler = FakeHandler()
    session.on_call_disconnected()
    await settle()
    assert session.terminated_reason == "caller_hangup"
    assert fake_acs.log == []
    assert len(registry) == 0
    assert session.handler.cleanups == 1


@pytest.mark.parametrize("reason, message", [("call_cap", GOODBYE), ("voicelive_dropped", FALLBACK), ("idle", FALLBACK)])
async def test_every_reason_is_removed_on_disconnect(fake_acs, reason, message):
    session, registry = make(fake_acs)
    connected(session)
    session.ws_used = True
    session.request_end(reason, message)
    await settle()
    session.on_play_done(failed=False)
    session.on_call_disconnected()
    await settle()
    assert len(registry) == 0


async def test_safety_timer_hangs_up_without_play_event(fake_acs):
    session, _ = make(fake_acs)
    connected(session)
    session.ws_used = True
    session.request_end("call_cap", GOODBYE)
    await settle(0.2)
    assert fake_acs.log == [("play", GOODBYE), ("hang_up", True)]


async def test_play_done_cancels_safety_timer_single_hangup(fake_acs):
    session, _ = make(fake_acs)
    connected(session)
    session.ws_used = True
    session.request_end("call_cap", GOODBYE)
    await settle()
    session.on_play_done(failed=False)
    await settle(0.2)
    assert fake_acs.log.count(("hang_up", True)) == 1


async def test_play_media_failure_still_hangs_up_without_raising(fake_acs):
    fake_acs.play_error = HttpResponseError("call gone")
    session, _ = make(fake_acs)
    connected(session)
    session.ws_used = True
    session.request_end("voicelive_dropped", FALLBACK)
    await settle()
    assert fake_acs.log == [("hang_up", True)]


async def test_connected_wait_timeout_hangs_up_without_play(fake_acs):
    session, _ = make(fake_acs)
    session.set_answered("conn-1")
    session.ws_used = True
    session.request_end("voicelive_connect_failed", FALLBACK)
    await settle(0.1)
    assert fake_acs.log == [("hang_up", True)]


async def test_goodbye_plays_without_waiting_for_voicelive_close(fake_acs):
    session, _ = make(fake_acs)
    connected(session)
    session.ws_used = True
    session.handler = FakeHandler(cleanup_delay=1.0)
    session.request_end("call_cap", GOODBYE)
    await settle()
    assert fake_acs.log == [("play", GOODBYE)]
    assert session.handler.stopped is True
    assert session.handler.cleanup_done == 0


async def test_voicelive_closed_once_when_terminate_and_disconnect_both_ask(fake_acs):
    session, _ = make(fake_acs)
    connected(session)
    session.ws_used = True
    session.handler = FakeHandler(cleanup_delay=0.01)
    session.request_end("call_cap", GOODBYE)
    session.on_call_disconnected()
    await settle(0.1)
    assert session.handler.cleanups == 1


async def test_hung_cleanup_is_force_closed(fake_acs):
    session, _ = make(fake_acs)
    session.handler = FakeHandler(cleanup_delay=1.0)
    session.on_call_disconnected()
    await settle(0.15)
    assert session.handler.forced is True


async def test_disconnect_during_connected_wait_means_no_play(fake_acs):
    session, _ = make(fake_acs)
    session.set_answered("conn-1")
    session.ws_used = True
    session.request_end("call_cap", GOODBYE)
    await settle(0.01)
    session.on_call_disconnected()
    await settle(0.2)
    assert fake_acs.log == []


async def test_disconnect_within_grace_window_means_no_fallback(fake_acs):
    session, _ = make(fake_acs)
    connected(session)
    session.ws_used = True
    session.on_media_ws_closed()
    session.on_call_disconnected()
    await settle(0.1)
    assert session.terminated_reason == "caller_hangup"
    assert fake_acs.log == []


async def test_media_lost_after_grace(fake_acs):
    session, _ = make(fake_acs)
    connected(session)
    session.ws_used = True
    session.on_media_ws_closed()
    await settle(0.1)
    assert session.terminated_reason == "media_lost"
    assert fake_acs.log == [("play", FALLBACK)]


async def test_route_miss_plays_fallback_on_connected(fake_acs):
    session, _ = make(fake_acs, route=None)
    session.set_answered("conn-1")
    session.mark_connected()
    await settle()
    assert session.terminated_reason == "route_miss"
    assert fake_acs.log == [("play", FALLBACK)]


async def test_callback_before_answer_returns_still_plays(fake_acs):
    session, _ = make(fake_acs, route=None)
    session.mark_connected()
    await settle(0.01)
    session.set_answered("conn-1")
    await settle()
    assert fake_acs.log == [("play", FALLBACK)]


async def test_answer_failed_removes_session(fake_acs):
    session, registry = make(fake_acs)
    session.mark_answer_failed()
    assert session.terminated_reason == "answer_failed"
    assert len(registry) == 0


async def test_terminated_reason_logged_exactly_once(fake_acs, logs):
    session, _ = make(fake_acs)
    connected(session)
    session.ws_used = True
    session.request_end("call_cap", GOODBYE)
    session.request_end("idle", FALLBACK)
    session.on_call_disconnected()
    await settle(0.1)
    assert logs.text.count("call_ended") == 1
    assert "9876" not in logs.text.replace("***9876", "")


async def test_sweep_ends_and_removes_stale_sessions(fake_acs):
    session, registry = make(fake_acs)
    session.set_answered("conn-1")
    assert registry.get_by_connection("conn-1") is session
    removed = registry.sweep(max_age=10, now=session.created_at + 11)
    await settle()
    assert removed == 1
    assert session.terminated_reason == "stale"
    assert len(registry) == 0
    assert registry.get_by_connection("conn-1") is None


class SlowHangUpConnection:
    """A call connection whose hang_up() takes a while to resolve, so tests can
    exercise races between an in-flight hang_up and a concurrent timer cancel."""

    def __init__(self, acs, delay):
        self._acs = acs
        self._delay = delay

    async def play_media(self, play_source, play_to="all", operation_context=None, **kwargs):
        if self._acs.play_error is not None:
            raise self._acs.play_error
        self._acs.log.append(("play", play_source.text))

    async def hang_up(self, is_for_everyone, **kwargs):
        self._acs.log.append(("hang_up_start", is_for_everyone))
        await asyncio.sleep(self._delay)
        self._acs.log.append(("hang_up_sent", is_for_everyone))


class SlowHangUpAcs:
    def __init__(self, delay):
        self.log = []
        self.play_error = None
        self._delay = delay

    def get_call_connection(self, call_connection_id):
        return SlowHangUpConnection(self, self._delay)


async def test_late_play_done_during_inflight_safety_hangup_still_completes():
    # Reviewer-found bug #A: if PlayCompleted arrives while the safety timer's
    # hang_up is already awaiting the HTTP call, on_play_done must not cancel
    # that in-flight request out from under it.
    acs = SlowHangUpAcs(delay=0.05)
    session, _ = make(acs)
    connected(session)
    session.ws_used = True
    session.request_end("call_cap", GOODBYE)
    await settle()
    assert acs.log == [("play", GOODBYE)]
    # Let the safety timer fire and start its hang_up, then race on_play_done
    # against it while the hang_up call is still in flight.
    await settle(0.1)
    assert acs.log[-1] == ("hang_up_start", True)
    session.on_play_done(failed=False)
    await settle(0.1)
    assert ("hang_up_sent", True) in acs.log
    assert acs.log.count(("hang_up_sent", True)) == 1


class FlakyThenOkConnection:
    def __init__(self, acs):
        self._acs = acs

    async def play_media(self, play_source, play_to="all", operation_context=None, **kwargs):
        self._acs.log.append(("play", play_source.text))

    async def hang_up(self, is_for_everyone, **kwargs):
        self._acs.attempts += 1
        if self._acs.attempts <= self._acs.fail_times:
            raise HttpResponseError(message="server error", response=SimpleNamespaceResponse(503))
        self._acs.log.append(("hang_up", is_for_everyone))


class SimpleNamespaceResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class FlakyThenOkAcs:
    def __init__(self, fail_times=1):
        self.log = []
        self.attempts = 0
        self.fail_times = fail_times

    def get_call_connection(self, call_connection_id):
        return FlakyThenOkConnection(self)


async def test_transient_hangup_failure_leaves_room_for_a_later_retry():
    # Reviewer-found bug #B: a transient (5xx) hang_up failure must not be
    # treated as terminal — a later attempt (here, a direct retry, standing in
    # for sweep()/force_hang_up() finding the session still up) must still get
    # the call down.
    acs = FlakyThenOkAcs(fail_times=1)
    session, registry = make(acs)
    session.set_answered("conn-1")
    session.ws_used = True
    session.request_end("voicelive_connect_failed", FALLBACK)
    await settle(0.1)  # wait_timeout elapses (never connected) -> _safe_hang_up
    assert acs.attempts == 1
    assert ("hang_up", True) not in acs.log
    assert session._hung_up is False
    # A later retry path (e.g. another end-path or sweep) can still succeed.
    await session._safe_hang_up()
    assert acs.attempts == 2
    assert ("hang_up", True) in acs.log


async def test_sweep_hangs_up_stale_answered_session():
    acs = FlakyThenOkAcs(fail_times=0)
    session, registry = make(acs)
    session.set_answered("conn-1")
    removed = registry.sweep(max_age=10, now=session.created_at + 11)
    await settle()
    assert removed == 1
    assert ("hang_up", True) in acs.log


async def test_request_end_before_set_answered_still_hangs_up_once_answered(fake_acs):
    # Reviewer-found bug #D: request_end() can run before set_answered() ever
    # supplies call_connection_id (e.g. CallConnected racing ahead of the
    # answer_call() response). _terminate() then waits up to wait_timeout for
    # _answered, times out, and _safe_hang_up() no-ops for lack of a connection
    # id. If set_answered() then arrives late, the caller must not be left in
    # silence forever.
    session, registry = make(fake_acs, route=None)
    session.mark_connected()
    await settle(0.1)  # wait_timeout (0.05) elapses with call_connection_id still None
    assert session.terminated_reason == "route_miss"
    assert fake_acs.log == []
    assert session._hung_up is False
    session.set_answered("conn-1")
    await settle()
    assert fake_acs.log == [("hang_up", True)]
