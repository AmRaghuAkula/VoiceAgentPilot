"""G2: genericity guard (spec section 13.1, plan P6): no denylisted words or
names, and no real phone numbers, anywhere in the project.

Failing-case inputs are assembled at runtime from fragments, so no source line
of this file contains a literal that G2's own rules would flag. A final
self-test runs G2 over this file and asserts that it is clean. On a match, only
the file and line are printed, never the matched text.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DENYLIST = ROOT / "tests" / "genericity_denylist.txt"
SKIP_DIRS = {".venv", "__pycache__", ".pytest_cache", ".python_packages", ".azure"}
SCAN_SUFFIXES = {".py", ".json", ".bicep", ".yaml", ".yml", ".md", ".txt", ".toml", ".ini", ".sh", ".cfg"}
SCAN_NAMES = {".gitignore", ".funcignore", ".python-version"}
EXCLUDED = {DENYLIST.resolve()}

PHONE_PATTERNS = (
    re.compile(r"\+\d[\d ().-]{9,18}\d"),
    re.compile(r"\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}"),
)
TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def _load_denylist() -> tuple[set[str], set[str]]:
    plain: set[str] = set()
    hashed: set[str] = set()
    section = None
    for raw in DENYLIST.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line in ("[plain]", "[sha256]"):
            section = line
            continue
        if section == "[plain]":
            plain.add(" ".join(t for t in TOKEN_SPLIT.split(line.lower()) if t))
        elif section == "[sha256]":
            hashed.add(line.lower())
    return plain, hashed


PLAIN, HASHED = _load_denylist()


def _windows(text: str) -> set[str]:
    tokens = [t for t in TOKEN_SPLIT.split(text.lower()) if t]
    out: set[str] = set()
    for size in (1, 2, 3):
        for i in range(len(tokens) - size + 1):
            out.add(" ".join(tokens[i : i + size]))
    return out


def word_hits(text: str) -> bool:
    for window in _windows(text):
        if window in PLAIN:
            return True
        if hashlib.sha256(window.encode("utf-8")).hexdigest() in HASHED:
            return True
    return False


def phone_hits(text: str) -> bool:
    for pattern in PHONE_PATTERNS:
        for match in pattern.finditer(text):
            digits = re.sub(r"\D", "", match.group(0))
            if not (len(digits) >= 7 and digits[-7:].startswith("55501")):
                return True
    return False


def scanned_files() -> list[Path]:
    files = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if path.resolve() in EXCLUDED or path.name == "uv.lock":
            continue
        if path.suffix.lower() in SCAN_SUFFIXES or path.name in SCAN_NAMES:
            files.append(path)
    return files


def scan_file(path: Path) -> list[str]:
    findings = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if word_hits(line) or phone_hits(line):
            findings.append(f"{path.relative_to(ROOT).as_posix()}:{number}")
    return findings


def test_denylist_loaded():
    assert PLAIN, "plaintext section is empty"
    assert HASHED, "hashed section is empty"
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in HASHED)


def test_scanned_files_cover_the_project():
    names = {p.relative_to(ROOT).as_posix() for p in scanned_files()}
    for expected in (
        "calendar_tools/core/bindings.py",
        "calendar_tools/obs.py",
        "tools/render_openapi.py",
        "openapi/calendar-tools.openapi.yaml",
        "bindings.sample.json",
        "README.md",
        "pyproject.toml",
        "tests/test_guard_genericity.py",
        "tests/data/spec_examples/book_request.json",
    ):
        assert expected in names
    assert "tests/genericity_denylist.txt" not in names


def test_no_denylisted_words_or_real_numbers_anywhere():
    findings = [f for path in scanned_files() for f in scan_file(path)]
    # Only file and line are printed, never the matched text.
    assert findings == []


def test_fictional_numbers_pass():
    assert not phone_hits("+1 613 555 0123")
    assert not phone_hits("+16135550199")
    assert not phone_hits("(613) 555-0100")


def test_non_fictional_numbers_fail():
    assert phone_hits("".join(["+1 613 ", "555 ", "02", "00"]))
    assert phone_hits("-".join(["613", "4" + "21", "77" + "00"]))
    # The spec's malformed example shape (exchange 010) is not fictional either (P15).
    assert phone_hits(" ".join(["+1", "555", "0" + "10", "01" + "23"]))


def test_bare_digit_runs_are_not_matched():
    assert not phone_hits("epoch 1727712000123 id 98765432101234")


def test_plain_word_matcher():
    assert word_hits("A " + "re" + "al" + " " + "es" + "tate" + " thing")
    assert not word_hits("a generic appointment on the calendar")


def test_hashed_matcher_catches_planted_canary():
    canary = "zzcanary" + "word"
    assert word_hits(f"text with {canary} inside")
    assert not word_hits("text with zzcanary inside")


def test_g2_is_clean_on_its_own_file():
    assert scan_file(Path(__file__)) == []


@pytest.mark.parametrize(
    "text",
    ["Name: Jordan Example", "Name: Sam Sample", "Phone: +1 613 555 0142", "binding test-alpha", "phone_call"],
)
def test_fixture_values_pass(text):
    assert not word_hits(text)
    assert not phone_hits(text)
