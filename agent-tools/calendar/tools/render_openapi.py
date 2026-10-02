"""Render one binding's copy of the canonical OpenAPI document (spec section 4.3).

Only `servers[0].url` changes (guard G3), so every agent gets byte-for-byte the
same operations, schemas and descriptions. The edit is textual (one line), so
comments, ordering and quoting elsewhere are preserved exactly.

Usage:
    python -m tools.render_openapi --base-url https://<host>/api/v1/bindings/<binding_id> \
        [--input openapi/calendar-tools.openapi.yaml] [--output <file>]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from calendar_tools.core.bindings import is_valid_binding_id

PLACEHOLDER_URL = "https://example.invalid/api/v1/bindings/BINDING_ID"
DEFAULT_INPUT = Path(__file__).resolve().parents[1] / "openapi" / "calendar-tools.openapi.yaml"

_BASE_URL = re.compile(
    r"https://(?P<host>[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?)(?::(?P<port>[0-9]{1,5}))?"
    r"/api/v1/bindings/(?P<binding>[^/?#\s]+)"
)


def validate_base_url(base_url: str) -> str:
    """`https://<host>[:port]/api/v1/bindings/<valid binding id>`, nothing else:
    no user info, query, fragment, trailing slash or whitespace."""
    if not isinstance(base_url, str):
        raise ValueError("base URL must be a string")
    match = _BASE_URL.fullmatch(base_url)
    if not match or not is_valid_binding_id(match.group("binding")):
        raise ValueError("base URL must be https://<host>/api/v1/bindings/<binding_id>")
    port = match.group("port")
    if port is not None and not 1 <= int(port) <= 65535:
        raise ValueError("base URL port must be 1-65535")
    parts = urlsplit(base_url)
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError("base URL must not carry a query, fragment or user info")
    return base_url


def render(doc_text: str, base_url: str) -> str:
    validate_base_url(base_url)
    data = yaml.safe_load(doc_text)
    try:
        current = data["servers"][0]["url"]
    except (KeyError, IndexError, TypeError):
        raise ValueError("document has no servers[0].url") from None
    if not isinstance(current, str) or doc_text.count(current) != 1:
        raise ValueError("servers[0].url must appear exactly once in the document")
    rendered = doc_text.replace(current, base_url, 1)

    # Self-check: the result parses and differs from the input only in that URL.
    before, after = yaml.safe_load(doc_text), yaml.safe_load(rendered)
    if after["servers"][0]["url"] != base_url:
        raise ValueError("rendered servers[0].url does not match")
    before["servers"][0]["url"] = after["servers"][0]["url"] = None
    if before != after:
        raise ValueError("rendering changed more than servers[0].url")
    return rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        rendered = render(args.input.read_text(encoding="utf-8"), args.base_url)
    except ValueError as exc:
        print(f"render_openapi: {exc}", file=sys.stderr)
        return 2
    if args.output is None:
        sys.stdout.write(rendered)
    else:
        args.output.write_text(rendered, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
