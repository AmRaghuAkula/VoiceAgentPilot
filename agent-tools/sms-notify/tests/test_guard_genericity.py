"""G2: genericity guard (calendar plan P6): phone numbers and the hashed denylist.

Failing-case inputs are assembled at runtime from fragments, so no source line of
this file contains a literal that G2's own rules would flag. A final self-test runs
G2 over this file and asserts that it is clean.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DENYLIST = ROOT / "tests" / "genericity_denylist.txt"
SKIP_DIRS = {".venv", "__pycache__", ".pytest_cache", ".python_packages", ".azure"}
SCAN_SUFFIXES = {".py", ".json", ".bicep", ".yaml", ".yml", ".md", ".txt", ".toml", ".ini"}
EXCLUDED_FILES = {DENYLIST.resolve()}

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
            last7 = digits[-7:]
            if not (len(last7) == 7 and last7.startswith("55501")):
                return True
    return False


def scanned_files() -> list[Path]:
    files = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(ROOT).parts
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        if path.resolve() in EXCLUDED_FILES:
            continue
        if path.name == "uv.lock":
            continue
        if path.suffix.lower() in SCAN_SUFFIXES or path.name in {".gitignore", ".funcignore"}:
            files.append(path)
    return files


def scan_file(path: Path) -> list[str]:
    findings = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if word_hits(line) or phone_hits(line):
            findings.append(f"{path.relative_to(ROOT)}:{number}")
    return findings


def test_denylist_loaded():
    assert PLAIN, "plaintext section is empty"
    assert HASHED, "hashed section is empty"
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in HASHED)


def test_scanned_files_cover_the_project():
    names = {p.relative_to(ROOT).as_posix() for p in scanned_files()}
    for expected in (
        "sms_notify/core/service.py",
        "openapi/sms-notify.json",
        "infra/main.bicep",
        "azure.yaml",
        "function_app.py",
        "README.md",
        "tests/test_guard_genericity.py",
    ):
        assert expected in names
    assert "tests/genericity_denylist.txt" not in names


def test_no_denylisted_words_or_real_numbers_anywhere():
    findings = []
    for path in scanned_files():
        findings.extend(scan_file(path))
    # Only file and line are printed, never the matched text.
    assert findings == []


def test_fictional_number_passes():
    assert not phone_hits("+1 613 555 0123")
    assert not phone_hits("+16135550199")
    assert not phone_hits("(613) 555-0100")


def test_non_fictional_number_fails():
    formatted = "".join(["+1 613 ", "555 ", "02", "00"])
    assert phone_hits(formatted)
    dashed = "-".join(["613", "4" + "21", "77" + "00"])
    assert phone_hits(dashed)


def test_bare_digit_runs_are_not_matched():
    assert not phone_hits("epoch 1727712000123 id 98765432101234")


def test_plain_word_matcher():
    planted = "A " + "re" + "al" + " " + "es" + "tate" + " thing"
    assert word_hits(planted)
    assert not word_hits("a generic follow-up message")


def test_hashed_matcher_catches_planted_canary():
    canary = "zzcanary" + "word"
    assert word_hits(f"text with {canary} inside")
    assert not word_hits("text with zzcanary inside")


def test_g2_is_clean_on_its_own_file():
    assert scan_file(Path(__file__)) == []


@pytest.mark.parametrize(
    "text",
    ["Name: Jordan Example", "Phone: +1 613 555 0142", "Budget: $1.5M", "J.Doe a.m."],
)
def test_fixture_values_pass(text):
    assert not word_hits(text)
    assert not phone_hits(text)
