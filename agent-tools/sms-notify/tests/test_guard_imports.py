"""G1: self-containment and a framework-free core (AST scan)."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".venv", "__pycache__", ".pytest_cache", ".python_packages"}

# The bridge's packages (server/, server/app/) and the calendar tool's package.
FORBIDDEN_TOP_LEVEL = {"server", "app", "calendar_tools"}

# The core and the ports stay free of transports, SDKs and the shells around them.
CORE_FORBIDDEN_PREFIXES = (
    "httpx",
    "azure",
    "jwt",
    "sms_notify.adapters",
    "sms_notify.http",
)


def _python_files() -> list[Path]:
    files = []
    for path in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        files.append(path)
    return files


def _resolve_relative(path: Path, module: str | None, level: int) -> str:
    package_parts = list(path.relative_to(ROOT).with_suffix("").parts[:-1])
    if level > 1:
        package_parts = package_parts[: len(package_parts) - (level - 1)]
    base = ".".join(package_parts)
    if module:
        return f"{base}.{module}" if base else module
    return base


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                names.append(_resolve_relative(path, node.module, node.level))
            elif node.module:
                names.append(node.module)
    return names


def test_python_files_found():
    files = _python_files()
    assert any(p.name == "service.py" for p in files)
    assert all(".venv" not in p.parts for p in files)


def test_no_import_of_bridge_or_calendar_packages():
    offenders = []
    for path in _python_files():
        for name in _imported_modules(path):
            if name.split(".")[0] in FORBIDDEN_TOP_LEVEL:
                offenders.append(f"{path.relative_to(ROOT)}: {name}")
    assert offenders == []


def test_core_and_ports_are_framework_free():
    targets = list((ROOT / "sms_notify" / "core").glob("*.py")) + [ROOT / "sms_notify" / "ports.py"]
    offenders = []
    for path in targets:
        for name in _imported_modules(path):
            if name.startswith(CORE_FORBIDDEN_PREFIXES):
                offenders.append(f"{path.relative_to(ROOT)}: {name}")
    assert offenders == []


def test_g1_detects_a_planted_violation(tmp_path):
    planted = ROOT / "sms_notify" / "_g1_probe_tmp.py"
    planted.write_text("import " + "server" + ".app\n", encoding="utf-8")
    try:
        names = _imported_modules(planted)
        assert any(n.split(".")[0] in FORBIDDEN_TOP_LEVEL for n in names)
    finally:
        planted.unlink()


def test_relative_import_resolution():
    path = ROOT / "sms_notify" / "core" / "service.py"
    assert _resolve_relative(path, "limits", 1) == "sms_notify.core.limits"
    assert _resolve_relative(path, "adapters", 2) == "sms_notify.adapters"
