import asyncio

from app.call_loop import run_call_loop
from app.call_manager import CallManager


async def test_is_expired_reasons():
    mgr = CallManager(max_concurrent=5, max_duration=10, idle_timeout=100)
    await mgr.acquire("c", "acs")
    assert mgr.is_expired("c") is None
    mgr._calls["c"].started_at -= 11
    assert mgr.is_expired("c") == "duration"
    mgr._calls["c"].started_at += 11
    mgr._calls["c"].last_activity -= 101
    assert mgr.is_expired("c") == "idle"
    assert mgr.is_expired("unknown") is None


class FakeManager:
    receive_timeout = 0.01

    def __init__(self, reasons):
        self._reasons = list(reasons)

    def is_expired(self, call_id):
        return self._reasons.pop(0) if self._reasons else None

    def touch(self, call_id):
        pass


class FakeWs:
    async def receive(self):
        await asyncio.sleep(1)


class FakeHandler:
    def __init__(self):
        self.calls = []

    async def connect_voicelive(self):
        await asyncio.sleep(10)

    async def on_message(self, msg):
        pass

    async def on_call_cap(self):
        self.calls.append("cap")

    async def on_idle(self):
        self.calls.append("idle")


async def test_duration_expiry_calls_cap_hook():
    handler = FakeHandler()
    await run_call_loop(call_manager=FakeManager(["duration"]), call_id="c", ws=FakeWs(), handler=handler)
    assert handler.calls == ["cap"]


async def test_idle_expiry_calls_idle_hook():
    handler = FakeHandler()
    await run_call_loop(call_manager=FakeManager([None, "idle"]), call_id="c", ws=FakeWs(), handler=handler)
    assert handler.calls == ["idle"]


async def test_base_handler_hooks_are_noops():
    from app.handler.voicelive_media_handler import VoiceLiveMediaHandler

    assert await VoiceLiveMediaHandler.on_call_cap(object()) is None
    assert await VoiceLiveMediaHandler.on_idle(object()) is None
