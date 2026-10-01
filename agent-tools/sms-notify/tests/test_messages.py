"""Core: K11 normalization, the K12 template validator (plan A1, T1 to T8 and T11), segments.

Labels here are made up (never the founder's). Non-ASCII test inputs are built with chr()
so this source stays ASCII.
"""

from __future__ import annotations

import pytest

from sms_notify.core.messages import (
    REASONS,
    MessageRejected,
    TemplateConfigInvalid,
    build_body,
    normalize,
    parse_labels,
    segment_info,
)

LABELS = "Contact,Callback #,Reason,Amount,When,Channel"

ZWSP, SHY, FW_DOT, IDEO_DOT = chr(0x200B), chr(0xAD), chr(0xFF0E), chr(0x3002)


def fw(text: str) -> str:
    """The full-width form of ASCII text."""
    return "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in text)


def body(text, *, prefix="", labels=LABELS, max_chars=480, max_lines=8):
    return build_body(text, prefix=prefix, labels_raw=labels, max_chars=max_chars, max_lines=max_lines)


def reason_of(text, **kwargs):
    with pytest.raises(MessageRejected) as err:
        body(text, **kwargs)
    assert err.value.code == "invalid_request"
    return err.value.reason


# --- K11 normalization ---------------------------------------------------------------------


def test_nfkc_applied():
    assert normalize(fw("Abc") + " " + chr(0xFB01) + "ne") == "Abc fine"


def test_crlf_and_cr_become_lf():
    assert normalize("one\r\ntwo\rthree\nfour") == "one\ntwo\nthree\nfour"


def test_control_characters_stripped_except_lf():
    assert normalize("a\x00b\x07c\x1bd\x7fe\x85f\x9fg\nh") == "abcdefg\nh"


def test_tab_counts_as_whitespace():
    assert normalize("a\tb") == "a b"


def test_bidi_zero_width_and_every_cf_character_stripped():
    hidden = [0x202E, 0x2066, 0x2069, 0x200B, 0x200E, 0xFEFF, 0xAD, 0x2061, 0x180E]
    text = "ab" + "".join(chr(c) + "x" for c in hidden)
    assert normalize(text) == "ab" + "x" * len(hidden)


def test_whitespace_collapsed_within_lines_and_blank_lines_dropped():
    assert normalize("  a   b  \n\n   \n c" + chr(0xA0) * 2 + "d  ") == "a b\nc d"


def test_punctuation_mapped_to_ascii():
    text = "".join(
        [chr(0x2018), "q", chr(0x2019), " ", chr(0x201C), "dq", chr(0x201D), " a", chr(0x2013), "b c",
         chr(0x2014), "d e", chr(0x2026), " `tick`"]
    )  # fmt: skip
    assert normalize(text) == "'q' \"dq\" a-b c-d e... 'tick'"


def test_unicode_line_separators_become_lf():
    assert normalize("a" + chr(0x2028) + "b" + chr(0x2029) + "c") == "a\nb\nc"


def test_full_stop_variants_become_period():
    assert normalize("1" + FW_DOT + "5 2" + IDEO_DOT + "5 3" + chr(0xFF61) + "5") == "1.5 2.5 3.5"


# --- T1: accepted --------------------------------------------------------------------------

FIXTURE = "\n".join(
    [
        "Contact: Zo" + chr(0xEB) + " Example",
        "Callback #: 416-555-0142",
        "Reason: Alpha, Beta",
        "Amount: $650K",
        "When: 3 months or $1.5M by then",
        "Channel: Phone & Text (100%)",
    ]
)


def test_t1_six_line_fixture_passes_unchanged():
    assert body(FIXTURE) == FIXTURE


def test_t1_label_case_order_and_omission():
    lines = FIXTURE.split("\n")
    assert body(FIXTURE.replace("Contact:", "CONTACT:")) == FIXTURE.replace("Contact:", "CONTACT:")
    reordered = "\n".join(reversed(lines))
    assert body(reordered) == reordered
    partial = "\n".join(lines[:3])
    assert body(partial) == partial


@pytest.mark.parametrize("value", ["$1.5M", "2.5", "O'Neil", "#12", "+1 (613) 555-0101", "Ren" + chr(0xE9)])
def test_t1_allowed_values(value):
    assert body(f"Contact: {value}") == f"Contact: {value}"


# --- T2: rejected values -------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "pay-now.top",
        "evil.ru",
        "goo.gl/x",
        "203.0.113.5/x",
        "203.0.113.5",
        "1.2.3",
        "http" + "://x",
        "a@b.com",
        "J.Smith",
        "Jr.",
        "3 months.",
        "www" + "example",
        "x_y",
        '"quoted"',
        "a" + chr(0xD7) + "b",
        "a;b",
        "50!",
        ".5",
    ],
)
def test_t2_rejected_values(value):
    assert reason_of(f"Contact: {value}") == "bad_character"


# --- T3: bad line shape --------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["http" + "://x", "Contact:", "Contact:value", "no colon here", ": value", "Contact: "],
    ids=["bare-url", "nothing-after", "no-space", "no-colon", "empty-label", "empty-value"],
)
def test_t3_bad_line(text):
    assert reason_of(text) == "bad_line"


# --- T4, T5: labels ------------------------------------------------------------------------


def test_t4_unknown_label():
    assert reason_of("Website: x") == "unknown_label"


@pytest.mark.parametrize("second", ["Contact: b", "CONTACT: b", "contact: b"])
def test_t5_duplicate_label(second):
    assert reason_of(f"Contact: a\n{second}") == "duplicate_label"


# --- T6: label config ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "labels",
    [
        "",
        "   ",
        ",,,",
        "Contact,Re.ason",
        "Contact,Re:ason",
        "Contact,Call  back",
        "Contact,contact",
        ",".join(f"L{i}" for i in range(9)),
        "A" * 33,
        "Contact,",
        "Con/tact",
    ],
    ids=["empty", "blank", "commas", "dot", "colon", "double-space", "duplicate", "too-many", "too-long",
         "trailing-comma", "slash"],  # fmt: skip
)
def test_t6_invalid_label_config_is_config_error(labels):
    with pytest.raises(TemplateConfigInvalid):
        body("Contact: a", labels=labels)


def test_t6_labels_parse_trimmed_and_case_folded():
    parsed = parse_labels(" Contact , Callback # ", 8)
    assert parsed == {"contact": "Contact", "callback #": "Callback #"}


# --- T7: SMS_PREFIX ------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", ["a/b ", "a@b ", "a.b ", "line\nbreak ", "www "])
def test_t7_bad_prefix_is_config_error(prefix):
    with pytest.raises(TemplateConfigInvalid):
        body("Contact: a", prefix=prefix)


def test_t7_valid_prefix_moves_the_boundary_down():
    prefix = "(P) "
    text = "Contact: " + "a" * (480 - len(prefix) - len("Contact: "))
    assert body(text, prefix=prefix) == prefix + text
    assert reason_of(text + "a", prefix=prefix) == "too_long"


# --- T8: Unicode tricks --------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "evil" + ZWSP + ".ru",
        "evil" + SHY + ".ru",
        "evil" + FW_DOT + "ru",
        "evil" + IDEO_DOT + "ru",
        fw("evil.ru"),
    ],
    ids=["zero-width", "soft-hyphen", "fullwidth-dot", "ideographic-dot", "fullwidth-all"],
)
def test_t8_unicode_tricks_rejected(value):
    assert reason_of(f"Contact: {value}") == "bad_character"


# --- limits and order (T11) ----------------------------------------------------------------


def test_empty_after_normalization():
    assert reason_of(" \n" + ZWSP + "\x00\t \r\n ") == "empty"


def test_8_lines_pass_9_fail():
    labels = ",".join(f"L{i}" for i in range(8))
    eight = "\n".join(f"L{i}: x" for i in range(8))
    assert body(eight, labels=labels) == eight
    assert reason_of(eight + "\nL0: y", labels=labels) == "too_many_lines"


def test_480_pass_481_fail():
    text = "Contact: " + "a" * (480 - len("Contact: "))
    assert len(body(text)) == 480
    assert reason_of(text + "a") == "too_long"


def test_t11_too_long_wins_over_bad_character():
    assert reason_of("Contact: " + "a/" * 300) == "too_long"


def test_t11_first_line_failure_wins():
    assert reason_of("Website: x\nContact: a/b") == "unknown_label"


def test_t11_too_many_lines_wins_over_too_long():
    assert reason_of("\n".join(["Contact: " + "a" * 100] * 9)) == "too_many_lines"


def test_config_error_wins_over_content_errors():
    with pytest.raises(TemplateConfigInvalid):
        body("", labels="")


def test_reasons_are_the_eight_k12_values():
    assert set(REASONS) == {
        "bad_request",
        "empty",
        "too_many_lines",
        "too_long",
        "bad_line",
        "unknown_label",
        "duplicate_label",
        "bad_character",
    }


# --- segments ------------------------------------------------------------------------------


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
    info = segment_info(chr(0x4E00) * length)
    assert info.encoding == "UCS-2"
    assert info.segments == segments


def test_gsm7_extension_characters_count_as_two():
    assert segment_info("a" * 158 + "{").segments == 1
    assert segment_info("a" * 159 + "{").segments == 2
    assert segment_info("a" * 159 + chr(0x20AC)).segments == 2


def test_newline_hash_and_dollar_are_gsm7():
    assert segment_info("A: #1\nB: $2").encoding == "GSM-7"


def test_backtick_is_not_gsm7_but_normalization_maps_it():
    assert segment_info("`").encoding == "UCS-2"
    assert segment_info(normalize("`")).encoding == "GSM-7"


def test_emoji_counts_as_two_utf16_units():
    assert segment_info(chr(0x1F600) * 35).segments == 1
    assert segment_info(chr(0x1F600) * 36).segments == 2
