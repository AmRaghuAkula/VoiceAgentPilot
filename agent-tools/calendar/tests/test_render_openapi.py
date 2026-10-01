"""G3: rendering a binding's copy changes only `servers[0].url` (spec section 4.3)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tools import render_openapi

ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = ROOT / "openapi" / "calendar-tools.openapi.yaml"
GOOD_URL = "https://cal-host.example.net/api/v1/bindings/test-alpha"


@pytest.fixture(scope="module")
def doc_text() -> str:
    return DOC_PATH.read_text(encoding="utf-8")


def _without_url(text: str) -> dict:
    data = yaml.safe_load(text)
    del data["servers"][0]["url"]
    return data


def test_render_sets_the_url(doc_text):
    rendered = render_openapi.render(doc_text, GOOD_URL)
    assert yaml.safe_load(rendered)["servers"][0]["url"] == GOOD_URL


def test_render_changes_only_servers_url(doc_text):
    rendered = render_openapi.render(doc_text, GOOD_URL)
    assert _without_url(rendered) == _without_url(doc_text)


def test_non_url_text_lines_identical(doc_text):
    rendered = render_openapi.render(doc_text, GOOD_URL)
    before, after = doc_text.splitlines(), rendered.splitlines()
    assert len(before) == len(after)
    changed = [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]
    assert len(changed) == 1
    assert GOOD_URL in after[changed[0]]
    assert render_openapi.PLACEHOLDER_URL in before[changed[0]]


def test_render_is_idempotent_on_a_rendered_copy(doc_text):
    once = render_openapi.render(doc_text, GOOD_URL)
    other = "https://cal-host.example.net/api/v1/bindings/test-beta"
    twice = render_openapi.render(once, other)
    assert _without_url(twice) == _without_url(doc_text)
    assert yaml.safe_load(twice)["servers"][0]["url"] == other


def test_port_allowed(doc_text):
    url = "https://localhost:7071/api/v1/bindings/test-alpha"
    assert yaml.safe_load(render_openapi.render(doc_text, url))["servers"][0]["url"] == url


@pytest.mark.parametrize(
    "bad",
    [
        "http://cal-host.example.net/api/v1/bindings/test-alpha",
        "https://cal-host.example.net/api/v1/bindings/test-alpha?x=1",
        "https://cal-host.example.net/api/v1/bindings/test-alpha#frag",
        "https://cal-host.example.net/api/v1/bindings/test-alpha/",
        "https://cal-host.example.net/api/v1/bindings/Test-Alpha",
        "https://cal-host.example.net/api/v1/bindings/ab",
        "https://cal-host.example.net/api/v1/bindings/id-" + "1234567",
        "https://cal-host.example.net/api/v2/bindings/test-alpha",
        "https://cal-host.example.net/api/v1/bindings/test-alpha/check-availability",
        "https://user:pw@cal-host.example.net/api/v1/bindings/test-alpha",
        "https:///api/v1/bindings/test-alpha",
        "https://cal-host.example.net/api/v1/bindings/test-alpha\n- url: x",
        " https://cal-host.example.net/api/v1/bindings/test-alpha",
        "",
    ],
    ids=[
        "http",
        "query",
        "fragment",
        "trailing-slash",
        "uppercase-id",
        "short-id",
        "phone-like-id",
        "wrong-version",
        "operation-path",
        "userinfo",
        "no-host",
        "newline-injection",
        "leading-space",
        "empty",
    ],
)
def test_rejects_bad_base_url(doc_text, bad):
    with pytest.raises(ValueError):
        render_openapi.render(doc_text, bad)


def test_rejects_document_without_single_url(doc_text):
    with pytest.raises(ValueError):
        render_openapi.render(doc_text.replace("servers:", "servers_x:"), GOOD_URL)
    duplicated = doc_text + "\n# " + render_openapi.PLACEHOLDER_URL + "\n"
    with pytest.raises(ValueError):
        render_openapi.render(duplicated, GOOD_URL)


def test_cli_writes_output(tmp_path):
    out = tmp_path / "copy.yaml"
    code = render_openapi.main(["--base-url", GOOD_URL, "--output", str(out)])
    assert code == 0
    assert yaml.safe_load(out.read_text(encoding="utf-8"))["servers"][0]["url"] == GOOD_URL


def test_cli_rejects_bad_url(tmp_path, capsys):
    out = tmp_path / "copy.yaml"
    code = render_openapi.main(["--base-url", "http://x/api/v1/bindings/test-alpha", "--output", str(out)])
    assert code == 2
    assert not out.exists()
