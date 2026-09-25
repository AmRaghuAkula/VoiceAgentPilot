import pytest

from app.log_mask import mask_number


@pytest.mark.parametrize(
    "value, expected",
    [
        ("+14165551234", "***1234"),
        ("(416) 555-1234", "***1234"),
        ("4:+14165551234", "***1234"),
        ("123", "***"),
        ("1234", "***1234"),
        ("12345", "***2345"),
        ("", "***"),
        (None, "***"),
        (14165551234, "***1234"),
    ],
)
def test_mask_number(value, expected):
    assert mask_number(value) == expected


def test_masked_value_never_has_more_than_four_digits():
    assert sum(c.isdigit() for c in mask_number("+14165551234")) == 4
