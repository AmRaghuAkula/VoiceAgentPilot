import pytest

from app.providers.acs.signing import sign, verify

SECRET = "s" * 40


def test_sign_is_hex_and_purpose_bound():
    ws = sign(SECRET, "ws", "abc")
    cb = sign(SECRET, "cb", "abc")
    assert ws != cb
    assert all(c in "0123456789abcdef" for c in ws) and len(ws) == 64


def test_verify_round_trip():
    assert verify(SECRET, "ws", "abc", sign(SECRET, "ws", "abc"))


@pytest.mark.parametrize("sig", [None, "", "00", "zz" * 32, "é" * 64, sign(SECRET, "cb", "abc")])
def test_verify_rejects_bad_signatures_without_raising(sig):
    assert verify(SECRET, "ws", "abc", sig) is False


def test_verify_rejects_other_call_key():
    assert verify(SECRET, "ws", "other", sign(SECRET, "ws", "abc")) is False
