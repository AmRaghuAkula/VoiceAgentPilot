"""Core: notify() processing order, limits, deadline and per-recipient outcomes (spec section 5, section 9)."""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from sms_notify.core.deadline import Deadline, budget_for
from sms_notify.core.errors import (
    NotifierAuthError,
    NotifierConfigError,
    NotifierRejected,
    NotifierUnavailable,
    SecretStoreUnavailable,
)
from sms_notify.core.limits import COOLDOWN_KEY, dedupe_key, slot_key
from sms_notify.core.service import CoreDeps, RequestRecord, notify, worst_code
from tests.fakes import (
    RECIPIENT_1,
    RECIPIENT_2,
    FakeClock,
    FakeNotifier,
    FakeSecretSource,
    FakeStateStore,
    make_settings,
)

TEXT = "Alpha: one\nBeta: two"


class Harness:
    def __init__(self, recipients=None, **settings):
        self.clock = FakeClock()
        self.state = FakeStateStore()
        self.secrets = FakeSecretSource(recipients)
        self.notifier = FakeNotifier(self.clock)
        self.settings = make_settings(**settings)
        self.deps = CoreDeps(self.clock, self.secrets, self.state, self.notifier)

    async def run(self, text=TEXT, deadline=None):
        record = RequestRecord()
        code = await notify(
            text,
            settings=self.settings,
            deps=self.deps,
            deadline=deadline or Deadline(self.clock),
            record=record,
        )
        return code, record


def _hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


# --- happy path and processing order ---------------------------------------------------


async def test_sends_once_and_records_outcome():
    h = Harness()
    code, record = await h.run()
    assert code == "sent"
    assert h.notifier.sent == [(RECIPIENT_1, TEXT)]
    assert h.state.json(dedupe_key(_hash(TEXT)))["outcome"] == "sent"
    assert record.code == "sent" and record.chars == len(TEXT)
    assert record.encoding == "GSM-7" and record.segments == 1
    assert record.recipients[0]["to"] == "***0199"


async def test_prefix_is_sent_but_hash_is_of_the_normalized_text():
    h = Harness(SMS_PREFIX="(X) ")
    code, _ = await h.run()
    assert code == "sent"
    assert h.notifier.sent[0][1] == "(X) " + TEXT
    assert dedupe_key(_hash(TEXT)) in h.state.items


async def test_invalid_message_touches_nothing():
    h = Harness()
    code, record = await h.run("Note: " + "a" * 481)
    assert code == "invalid_request" and record.reason == "too_long"
    assert h.state.calls == [] and h.secrets.timeouts == [] and h.notifier.sent == []


async def test_template_failure_is_invalid_request_before_any_claim():
    h = Harness()
    code, record = await h.run("Note: see example.com")
    assert code == "invalid_request" and record.reason == "bad_character"
    assert h.state.calls == [] and h.secrets.timeouts == [] and h.notifier.sent == []


@pytest.mark.parametrize("labels", ["", ",,", "Alpha,alpha"])
async def test_invalid_label_config_is_unavailable_and_nothing_claimed(labels):
    h = Harness(SMS_ALLOWED_LABELS=labels)
    code, _ = await h.run()
    assert code == "unavailable" and h.state.calls == [] and h.notifier.sent == []


async def test_malformed_twilio_secret_is_unavailable_before_any_claim():
    h = Harness()
    h.secrets.values["twilio-api"] = json.dumps({"account_sid": "bad"})
    code, _ = await h.run()
    assert code == "unavailable" and h.state.calls == [] and h.notifier.sent == []


async def test_both_secrets_read_before_any_claim():
    h = Harness()
    await h.run()
    assert len(h.secrets.timeouts) == 2


async def test_prefix_with_newline_is_unavailable():
    h = Harness(SMS_PREFIX="a\nb")
    code, _ = await h.run()
    assert code == "unavailable" and h.notifier.sent == []


# --- config and secrets -----------------------------------------------------------------


async def test_secret_store_failure_is_unavailable_and_nothing_claimed():
    h = Harness()
    h.secrets.fail = True
    code, _ = await h.run()
    assert code == "unavailable" and h.state.calls == [] and h.notifier.sent == []


@pytest.mark.parametrize(
    "recipients",
    [
        ["6135550199"],
        ["+12025550123"],
        ["+16135550101", "+16135550102", "+16135550103", "+16135550104"],
        [],
    ],
    ids=["not-e164", "not-canada", "more-than-3", "empty"],
)
async def test_bad_recipients_are_unavailable(recipients):
    h = Harness()
    h.secrets.values["sms-recipients"] = json.dumps({"recipients": recipients})
    code, _ = await h.run()
    assert code == "unavailable" and h.notifier.sent == [] and h.state.calls == []


async def test_missing_twilio_secret_is_unavailable_before_claims():
    h = Harness()
    del h.secrets.values["twilio-api"]
    code, _ = await h.run()
    assert code == "unavailable" and h.state.calls == []


# --- dedupe -------------------------------------------------------------------------------


async def test_duplicate_after_sent_is_already_sent_and_nothing_sent():
    h = Harness()
    assert (await h.run())[0] == "sent"
    h.clock.advance(120)  # past the cooldown
    code, _ = await h.run()
    assert code == "already_sent"
    assert len(h.notifier.sent) == 1


async def test_duplicate_after_failed_send_is_rate_limited():
    h = Harness()
    h.notifier.behavior[RECIPIENT_1] = NotifierRejected(status=400, error_code=21211)
    assert (await h.run())[0] == "send_failed"
    assert h.state.json(dedupe_key(_hash(TEXT)))["outcome"] == "send_failed"
    h.notifier.behavior.clear()
    h.clock.advance(120)
    code, _ = await h.run()
    assert code == "rate_limited" and h.notifier.sent == []


async def test_duplicate_after_pending_is_rate_limited():
    h = Harness()
    first, _ = await h.run()
    key = dedupe_key(_hash(TEXT))
    body, etag = h.state.items[key]
    data = json.loads(body)
    data["outcome"] = "pending"
    h.state.items[key] = (json.dumps(data).encode(), etag)
    h.clock.advance(120)
    code, _ = await h.run()
    assert first == "sent" and code == "rate_limited" and len(h.notifier.sent) == 1


async def test_duplicate_after_the_window_sends_again():
    h = Harness()
    await h.run()
    h.clock.advance(30 * 60)
    code, _ = await h.run()
    assert code == "sent" and len(h.notifier.sent) == 2


async def test_duplicate_just_inside_the_window_is_deduped():
    h = Harness()
    await h.run()
    h.clock.advance(30 * 60 - 1)
    assert (await h.run())[0] == "already_sent"


async def test_failed_outcome_write_back_leaves_pending():
    h = Harness()
    key = dedupe_key(_hash(TEXT))
    h.state.fail_keys = lambda k: False
    original_replace = h.state.replace

    async def failing_replace(k, body, etag, *, timeout):
        if k == key:
            raise RuntimeError("write-back fails")
        return await original_replace(k, body, etag, timeout=timeout)

    h.state.replace = failing_replace
    assert (await h.run())[0] == "sent"
    assert h.state.json(key)["outcome"] == "pending"


async def test_corrupt_dedupe_blob_is_taken_over():
    h = Harness()
    h.state.items[dedupe_key(_hash(TEXT))] = (b"not json", '"etag-x"')
    assert (await h.run())[0] == "sent"


# --- cooldown -----------------------------------------------------------------------------


async def test_cooldown_blocks_a_second_text_and_is_stamped_at_claim_time():
    h = Harness()
    assert (await h.run("Note: first text"))[0] == "sent"
    stamp = h.state.json(COOLDOWN_KEY)["at"]
    h.clock.advance(89)
    assert (await h.run("Note: second text"))[0] == "rate_limited"
    assert h.state.json(COOLDOWN_KEY)["at"] == stamp  # not re-stamped when refused


async def test_cooldown_boundary_at_90_seconds():
    h = Harness()
    await h.run("Note: first text")
    h.clock.advance(90)
    assert (await h.run("Note: second text"))[0] == "sent"


async def test_cooldown_not_rolled_back_after_a_failed_send():
    h = Harness()
    h.notifier.behavior[RECIPIENT_1] = NotifierRejected(status=500, error_code=20500)
    assert (await h.run("Note: first text"))[0] == "send_failed"
    h.notifier.behavior.clear()
    h.clock.advance(10)
    assert (await h.run("Note: second text"))[0] == "rate_limited"
    assert h.notifier.sent == []


# --- hourly cap -----------------------------------------------------------------------------


async def test_hourly_cap_sixth_sends_seventh_refused():
    h = Harness(SMS_MIN_INTERVAL_SECONDS="0")
    h.clock._now = h.clock._now.replace(minute=0)
    codes = []
    for i in range(7):
        codes.append((await h.run(f"Note: text number {i}"))[0])
        h.clock.advance(1)
    assert codes == ["sent"] * 6 + ["rate_limited"]
    assert len(h.notifier.sent) == 6


async def test_hourly_cap_rolls_over_with_the_hour():
    h = Harness(SMS_MIN_INTERVAL_SECONDS="0")
    h.clock._now = h.clock._now.replace(minute=59)
    for i in range(6):
        await h.run(f"Note: text number {i}")
    assert (await h.run("Note: one more"))[0] == "rate_limited"
    h.clock.advance(60)  # next UTC hour
    assert (await h.run("Note: next hour text"))[0] == "sent"
    assert slot_key(h.clock.now(), 1) in h.state.items


# --- concurrency ------------------------------------------------------------------------------


async def test_concurrent_identical_requests_send_once():
    h = Harness()
    h.state.yield_between = True
    results = await asyncio.gather(h.run(), h.run())
    codes = sorted(code for code, _ in results)
    assert codes == ["rate_limited", "sent"]
    assert len(h.notifier.sent) == 1


async def test_concurrent_different_requests_only_one_wins_the_cooldown():
    h = Harness()
    h.state.yield_between = True
    results = await asyncio.gather(h.run("Note: text one"), h.run("Note: text two"))
    codes = sorted(code for code, _ in results)
    assert codes == ["rate_limited", "sent"]
    assert len(h.notifier.sent) == 1


# --- storage failures -----------------------------------------------------------------------


@pytest.mark.parametrize("op", ["create", "get", "replace"])
async def test_storage_failure_is_unavailable_and_nothing_sent(op):
    h = Harness()
    if op != "create":
        await h.run("Note: warm up text")  # so the next request reads and replaces existing blobs
        h.clock.advance(120)
        h.notifier.sent.clear()
    h.state.fail_on = {op}
    code, _ = await h.run()
    assert code == "unavailable"
    assert h.notifier.sent == []


async def test_storage_failure_on_hourly_slot_is_unavailable():
    h = Harness()
    h.state.fail_keys = lambda key: key.startswith("rate/")
    assert (await h.run())[0] == "unavailable"
    assert h.notifier.sent == []


# --- deadline (K5) -------------------------------------------------------------------------------


def test_deadline_budget_is_min_of_budget_and_remaining():
    clock = FakeClock()
    deadline = Deadline(clock, 5.0)
    assert deadline.budget(3.5) == 3.5
    clock.advance(3.0)
    assert deadline.budget(3.5) == pytest.approx(2.0)
    assert deadline.budget(0.5) == 0.5
    clock.advance(2.5)
    assert deadline.budget(0.5) == 0.0 and deadline.expired


def test_budget_for_without_a_deadline_is_the_operation_budget():
    assert budget_for(3.5) == 3.5


async def test_operation_timeouts_never_exceed_budgets_or_remaining_time():
    h = Harness()
    h.state.on_call = lambda op, key: h.clock.advance(0.4)
    await h.run()
    assert all(t <= 0.5 for t in h.state.timeouts)
    assert all(t <= 1.5 for t in h.secrets.timeouts)
    assert h.notifier.seen_budgets[0] is not None and h.notifier.seen_budgets[0] < 5.0


async def test_deadline_expired_before_the_post_is_send_failed_and_not_called():
    h = Harness()
    h.state.on_call = lambda op, key: h.clock.advance(1.5)  # storage eats the whole budget
    code, record = await h.run()
    assert code == "send_failed"
    assert h.notifier.sent == []
    assert record.recipients[0]["outcome"] == "send_failed"


async def test_deadline_expired_after_the_post_is_send_unconfirmed():
    h = Harness()

    def slow_then_timeout():
        h.clock.advance(6.0)
        raise NotifierUnavailable(maybe_sent=True)

    h.notifier.behavior[RECIPIENT_1] = slow_then_timeout
    code, _ = await h.run()
    assert code == "send_unconfirmed"


async def test_a_hung_send_is_cut_off_by_the_backstop_as_unconfirmed():
    h = Harness()
    deadline = Deadline(h.clock, 0.05)

    async def hang(cfg, message):
        await asyncio.sleep(5)

    h.notifier.send = hang
    code, _ = await h.run(deadline=deadline)
    assert code == "send_unconfirmed"


# --- per-recipient outcomes and the worst-code rule (P6) -------------------------------------


async def test_two_recipients_both_sent():
    h = Harness([RECIPIENT_1, RECIPIENT_2])
    code, record = await h.run()
    assert code == "sent"
    assert [r for r, _ in h.notifier.sent] == [RECIPIENT_1, RECIPIENT_2]
    assert [r["to"] for r in record.recipients] == ["***0199", "***0142"]


async def test_partial_success_is_not_ok_with_the_worst_code():
    h = Harness([RECIPIENT_1, RECIPIENT_2])
    h.notifier.behavior[RECIPIENT_2] = NotifierUnavailable(maybe_sent=False)
    code, record = await h.run()
    assert code == "send_failed"
    assert [r["outcome"] for r in record.recipients] == ["sent", "send_failed"]
    assert h.state.json(dedupe_key(_hash(TEXT)))["outcome"] == "send_failed"


async def test_second_recipient_skipped_when_first_used_up_the_deadline():
    h = Harness([RECIPIENT_1, RECIPIENT_2])
    h.notifier.behavior[RECIPIENT_1] = lambda: h.clock.advance(10)
    code, record = await h.run()
    assert [r["outcome"] for r in record.recipients] == ["sent", "send_failed"]
    assert code == "send_failed"
    assert [r for r, _ in h.notifier.sent] == [RECIPIENT_1]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (NotifierUnavailable(maybe_sent=True), "send_unconfirmed"),
        (NotifierUnavailable(maybe_sent=False), "send_failed"),
        (NotifierAuthError(error_code=20003), "send_failed"),
        (NotifierRejected(status=400, error_code=21211), "send_failed"),
        (NotifierConfigError("bad"), "unavailable"),
        (SecretStoreUnavailable("down"), "unavailable"),
        (RuntimeError("unexpected"), "send_unconfirmed"),
    ],
)
async def test_notifier_errors_map_to_codes(error, expected):
    h = Harness()
    h.notifier.behavior[RECIPIENT_1] = error
    assert (await h.run())[0] == expected


async def test_provider_error_code_recorded_but_nothing_else():
    h = Harness()
    h.notifier.behavior[RECIPIENT_1] = NotifierRejected(status=400, error_code=21211)
    _, record = await h.run()
    assert record.recipients[0]["provider_error"] == 21211


@pytest.mark.parametrize(
    ("codes", "expected"),
    [
        (["sent", "sent"], "sent"),
        (["sent", "send_failed"], "send_failed"),
        (["send_failed", "unavailable"], "unavailable"),
        (["unavailable", "send_unconfirmed", "send_failed"], "send_unconfirmed"),
        (["sent", "send_unconfirmed"], "send_unconfirmed"),
    ],
)
def test_worst_code_order(codes, expected):
    assert worst_code(codes) == expected
