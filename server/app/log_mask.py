import re

_NON_DIGIT = re.compile(r"\D")


def mask_number(value: object) -> str:
    if value is None:
        return "***"
    digits = _NON_DIGIT.sub("", str(value))
    if len(digits) < 4:
        return "***"
    return "***" + digits[-4:]
