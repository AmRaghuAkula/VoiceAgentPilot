"""G1: the core is framework-free (spec section 3 property 3, section 13.1).
G1b: self-containment; nothing here imports the bridge or another agent tool
(spec section 15.2, CLAUDE.md section 7)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "calendar_tools" / "core"
SKIP_DIRS = {".venv", "__pycache__", ".pytest_cache", ".python_packages", ".azure"}

CORE_FORBIDDEN = (
    "azure",
    "httpx",
    "aiohttp",
    "calendar_tools.providers",
    "calendar_tools.stores",
    "calendar_tools.http",
    "calendar_tools.secrets",
    "calendar_tools.azure_rest",
)

# The bridge's packages (server/, server/app/) and the sibling agent tool.
FORBIDDEN_TOP_LEVEL = {"app", "server", "sms_notify"}


def _python_files(base: Path) -> list[Path]:
    return [
        p
        for p in base.rglob("*.py")
        if not any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts)
    ]


def _resolve_relative(path: Path, module: str | None, level: int) -> str:
    # The containing package (for `pkg/__init__.py` that is `pkg` itself).
    package_parts = list(path.relative_to(ROOT).parts[:-1])
    if level > 1:
        package_parts = package_parts[: len(package_parts) - (level - 1)]
    base = ".".join(package_parts)
    if module:
        return f"{base}.{module}" if base else module
    return base


def imported_modules(path: Path, source: str | None = None) -> list[str]:
    """Imported module names; `source` lets a probe be parsed as if it were at
    `path` without writing a file into the tree."""
    text = path.read_text(encoding="utf-8") if source is None else source
    tree = ast.parse(text, filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_relative(path, node.module, node.level) if node.level else (node.module or "")
            names.append(base)
            # `from calendar_tools import providers` imports a forbidden module too.
            names.extend(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
    return names


def _forbidden_in_core(name: str) -> bool:
    return any(name == f or name.startswith(f + ".") for f in CORE_FORBIDDEN)


def test_core_files_found():
    names = {p.name for p in _python_files(CORE)}
    assert {"ports.py", "bindings.py", "clock.py", "deadline.py"} <= names


def test_g1_core_imports_no_framework_or_shell():
    offenders = [
        f"{p.relative_to(ROOT)}: {name}"
        for p in _python_files(CORE)
        for name in imported_modules(p)
        if _forbidden_in_core(name)
    ]
    assert offenders == []


def test_g1b_no_import_of_bridge_or_sibling_tool():
    offenders = [
        f"{p.relative_to(ROOT)}: {name}"
        for p in _python_files(ROOT)
        for name in imported_modules(p)
        if name.split(".")[0] in FORBIDDEN_TOP_LEVEL
    ]
    assert offenders == []


@pytest.mark.parametrize(
    "source",
    [
        "import " + "httpx\n",
        "import azure" + ".identity\n",
        "from azure" + ".functions import HttpRequest\n",
        "from calendar_tools" + ".providers import fake\n",
        "from calendar_tools import " + "providers\n",
        "from .." + "stores import azure_blob\n",
        "from ..http" + ".app import handle\n",
        "from .. import " + "secrets\n",
    ],
)
def test_g1_detects_planted_violations(source):
    probe = CORE / "_g1_probe.py"  # never written; parsed as if it lived in core/
    assert any(_forbidden_in_core(n) for n in imported_modules(probe, source))


@pytest.mark.parametrize(
    "source",
    ["import " + "server" + ".app\n", "from " + "app" + ".config import x\n", "import sms" + "_notify\n"],
)
def test_g1b_detects_planted_violations(source):
    probe = ROOT / "calendar_tools" / "_g1b_probe.py"  # never written
    assert any(n.split(".")[0] in FORBIDDEN_TOP_LEVEL for n in imported_modules(probe, source))


def test_allowed_core_imports_not_flagged():
    probe = CORE / "_g1_probe_ok.py"  # never written
    source = "from . import ports\nfrom .clock import Clock\nimport phonenumbers\n"
    assert not any(_forbidden_in_core(n) for n in imported_modules(probe, source))


def test_relative_import_resolution():
    path = CORE / "bindings.py"
    assert _resolve_relative(path, "ports", 1) == "calendar_tools.core.ports"
    assert _resolve_relative(path, "providers", 2) == "calendar_tools.providers"
    assert _resolve_relative(CORE / "__init__.py", "ports", 1) == "calendar_tools.core.ports"
