"""Core: normalization, limits, links, segments (spec K11, section 5 step 2, section 9)."""

from __future__ import annotations

import pytest

from sms_notify.core.messages import (
    MessageRejected,
    PrefixInvalid,
    build_body,
    contains_link,
    normalize,
    segment_info,
)

# --- normalization ----------------------------------------------------------------------


def test_nfkc_applied():
    # Full-width letters and a ligature fold to ASCII under NFKC.
    assert normalize("Ａｂｃ ﬁne") == "Abc fine"


def test_crlf_and_cr_become_lf():
    assert normalize("one\r\ntwo\rthree\nfour") == "one\ntwo\nthree\nfour"


def test_control_characters_stripped_except_lf():
    assert normalize("a\x00b\x07c\x1bd\x7fe\x85f\x9fg\nh") == "abcdefg\nh"


def test_tab_counts_as_whitespace():
    assert normalize("a\tb") == "a b"


def test_bidi_and_zero_width_stripped():
    text = "ab‮cd⁦ef⁩gh​ij‎kl﻿"
    assert normalize(text) == "abcdefghijkl"


def test_whitespace_collapsed_within_lines_and_blank_lines_dropped():
    assert normalize("  a   b  \n\n   \n c  d  ") == "a b\nc d"


def test_punctuation_mapped_to_ascii():
    text = "‘q’ “dq” a–b c—d e… `tick`"
    assert normalize(text) == "'q' \"dq\" a-b c-d e... 'tick'"


def test_newlines_preserved():
    assert normalize("Alpha: 1\nBeta: 2\nGamma: 3") == "Alpha: 1\nBeta: 2\nGamma: 3"


def test_unicode_line_separators_become_lf():
    assert normalize("a b c") == "a\nb\nc"


# --- build_body: prefix, limits, links, empty ------------------------------------------


def _ok(text, prefix="", max_chars=480, max_lines=8):
    return build_body(text, prefix=prefix, max_chars=max_chars, max_lines=max_lines)


def test_generic_six_line_fixture_passes_unchanged():
    fixture = (
        "Field one: Jordan Example\n"
        "Field two: +1 613 555 0142\n"
        "Field three: Alpha\n"
        "Field four: $650,000\n"
        "Field five: 3 months\n"
        "Field six: Beta"
    )
    assert _ok(fixture) == fixture


def test_exactly_480_chars_passes_and_481_fails():
    assert len(_ok("a" * 480)) == 480
    with pytest.raises(MessageRejected) as err:
        _ok("a" * 481)
    assert err.value.code == "invalid_request"


def test_exactly_8_lines_passes_and_9_fails():
    assert _ok("\n".join(["x"] * 8)).count("\n") == 7
    with pytest.raises(MessageRejected) as err:
        _ok("\n".join(["x"] * 9))
    assert err.value.code == "invalid_request"


def test_prefix_moves_the_boundary_down():
    prefix = "[P] "
    body = _ok("a" * 476, prefix=prefix)
    assert body == prefix + "a" * 476 and len(body) == 480
    with pytest.raises(MessageRejected) as err:
        _ok("a" * 477, prefix=prefix)
    assert err.value.code == "invalid_request"


def test_prefix_prepended_to_first_line():
    assert _ok("one\ntwo", prefix="P: ") == "P: one\ntwo"


@pytest.mark.parametrize("prefix", ["bad\nprefix", "bad\rprefix"])
def test_prefix_with_newline_is_invalid(prefix):
    with pytest.raises(PrefixInvalid):
        _ok("hello", prefix=prefix)


def test_empty_after_stripping_is_content_rejected():
    with pytest.raises(MessageRejected) as err:
        _ok(" \n​\x00\t \r\n ")
    assert err.value.code == "content_rejected"


@pytest.mark.parametrize(
    "text",
    [
        "see https://example.test/x",
        "go to www.example",
        "visit example.com today",
        "x.ca",
        "mail me at someone@example.org",
        "EXAMPLE.COM",
        "short.ly/abc",
        "bit.link.",
    ],
)
def test_links_rejected(text):
    assert contains_link(text)
    with pytest.raises(MessageRejected) as err:
        _ok(text)
    assert err.value.code == "content_rejected"


@pytest.mark.parametrize("text", ["J.Doe", "$1.5M", "a.m.", "3 p.m. or 9 a.m.", "e.g. later", "v2.0"])
def test_non_links_pass(text):
    assert not contains_link(text)
    assert _ok(text) == text


def test_link_hidden_by_zero_width_space_still_rejected():
    with pytest.raises(MessageRejected):
        _ok("example​.com")


# --- segments ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("length", "segments"),
    [(160, 1), (161, 2), (306, 2), (307, 3), (459, 3), (460, 4)],
)
def test_gsm7_segments(length, segments):
    info = segment_info("a" * length)
    assert info.encoding == "GSM-7"
    assert info.segments == segments


@pytest.mark.parametrize(("length", "segments"), [(70, 1), (71, 2), (134, 2), (135, 3)])
def test_ucs2_segments(length, segments):
    info = segment_info("一" * length)
    assert info.encoding == "UCS-2"
    assert info.segments == segments


def test_gsm7_extension_characters_count_as_two():
    assert segment_info("a" * 158 + "{").segments == 1  # 160 septets
    assert segment_info("a" * 159 + "{").segments == 2  # 161 septets
    assert segment_info("a" * 159 + "€").segments == 2


def test_newline_hash_and_dollar_are_gsm7():
    assert segment_info("A: #1\nB: $2").encoding == "GSM-7"


def test_backtick_is_not_gsm7_but_normalization_maps_it():
    assert segment_info("`").encoding == "UCS-2"
    assert segment_info(normalize("`")).encoding == "GSM-7"


def test_emoji_counts_as_two_utf16_units():
    info = segment_info("\U0001f600" * 35)
    assert info.encoding == "UCS-2" and info.segments == 1
    assert segment_info("\U0001f600" * 36).segments == 2


@pytest.mark.parametrize(
    "text",
    ["example­.com", "example.c­om", "example⁡.com", "evil。com", "evil．com"],
    ids=["soft-hyphen-dot", "soft-hyphen-tld", "invisible-op", "ideographic-dot", "fullwidth-dot"],
)
def test_hidden_link_tricks_rejected(text):
    with pytest.raises(MessageRejected) as err:
        _ok(text)
    assert err.value.code == "content_rejected"
