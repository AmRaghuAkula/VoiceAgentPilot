"""Runtime wiring, the Functions shell and packaging."""

from __future__ import annotations

import importlib
import json
import re
import tomllib
from pathlib import Path

import httpx
import pytest

from sms_notify.core.errors import ConfigError
from sms_notify.http.dispatcher import ROUTE, Dispatcher, Request
from sms_notify.runtime import RuntimeFactory
from tests.fakes import BASE_ENV, FakeClock

ROOT = Path(__file__).resolve().parents[1]


class NoTokens:
    async def get_token(self, scope, *, timeout):
        raise AssertionError("not called")


def _factory(env):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    return RuntimeFactory(lambda: env, FakeClock(), client=client, tokens=NoTokens())


def test_runtime_builds_once_and_rereads_settings():
    env = dict(BASE_ENV)
    factory = _factory(env)
    first = factory()
    env["SMS_MAX_PER_HOUR"] = "3"
    second = factory()
    assert second.deps is first.deps and second.jwks is first.jwks
    assert second.settings.max_per_hour == 3


def test_runtime_rebuilds_when_endpoints_change():
    env = dict(BASE_ENV)
    factory = _factory(env)
    first = factory()
    env["KEY_VAULT_URI"] = "https://kv-other.vault.azure.net/"
    assert factory().deps is not first.deps


def test_runtime_config_error_propagates():
    with pytest.raises(ConfigError):
        _factory({})()


async def test_missing_config_answers_200_unavailable_through_the_dispatcher():
    dispatcher = Dispatcher(_factory({}), FakeClock())
    response = await dispatcher.handle(Request("POST", ROUTE, {}, b"{}"))
    assert response.status == 200
    assert json.loads(response.body) == {"ok": False, "code": "unavailable", "retry": False}


def test_function_app_registers_one_anonymous_post_route():
    module = importlib.import_module("function_app")
    functions = module.app.get_functions()
    assert len(functions) == 1
    bindings = json.loads(functions[0].get_function_json())["bindings"]
    trigger = next(b for b in bindings if b["type"] == "httpTrigger")
    assert trigger["route"] == "v1/follow-up-sms"
    assert [m.lower() for m in trigger["methods"]] == ["post"]
    assert trigger["authLevel"].lower() == "anonymous"


def test_host_json_route_prefix_matches_dispatcher():
    host = json.loads((ROOT / "host.json").read_text("utf-8"))
    assert ROUTE.startswith("/" + host["extensions"]["http"]["routePrefix"] + "/")


def test_requirements_txt_covers_every_runtime_dependency():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    requirements = (ROOT / "requirements.txt").read_text("utf-8").lower()
    for dep in project["dependencies"]:
        name = re.split(r"[\[<>=~ ;]", dep, maxsplit=1)[0].lower()
        assert re.search(rf"^{re.escape(name)}==", requirements, re.MULTILINE), name


def test_local_settings_is_ignored():
    ignored = (ROOT / ".gitignore").read_text("utf-8").splitlines()
    assert "local.settings.json" in ignored


def test_requirements_txt_is_hash_pinned():
    lines = (ROOT / "requirements.txt").read_text("utf-8").splitlines()
    pins = [line for line in lines if re.match(r"^[A-Za-z0-9_.-]+==", line)]
    assert pins and all(line.rstrip().endswith("\\") for line in pins)
    assert sum("--hash=sha256:" in line for line in lines) >= len(pins)


def test_bicep_has_the_labels_setting_without_a_default():
    main = (ROOT / "infra" / "main.bicep").read_text("utf-8")
    assert "param smsAllowedLabels string\n" in main
    assert "SMS_ALLOWED_LABELS: smsAllowedLabels" in main
    params = json.loads((ROOT / "infra" / "main.parameters.json").read_text("utf-8"))["parameters"]
    assert params["smsAllowedLabels"]["value"] == "${SMS_ALLOWED_LABELS}"
