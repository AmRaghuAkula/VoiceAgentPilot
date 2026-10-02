"""Provider registry (spec section 6.4)."""

from __future__ import annotations

import copy
import json

import pytest

from calendar_tools.core.bindings import BindingConfigError, load_bindings
from calendar_tools.providers.fake import FakeCalendarProvider
from calendar_tools.providers.registry import (
    ProviderRegistry,
    UnknownProviderError,
    default_registry,
)
from tests.test_bindings import BASE


def test_register_and_create():
    reg = ProviderRegistry()
    reg.register("fake", FakeCalendarProvider)
    provider = reg.create("fake")
    assert isinstance(provider, FakeCalendarProvider)
    assert provider.name == "fake"


def test_create_passes_dependencies():
    reg = ProviderRegistry()
    reg.register("fake_b", lambda **deps: FakeCalendarProvider(name=deps["name"]))
    assert reg.create("fake_b", name="fake_b").name == "fake_b"


def test_duplicate_names_rejected():
    reg = ProviderRegistry()
    reg.register("fake", FakeCalendarProvider)
    with pytest.raises(ValueError):
        reg.register("fake", FakeCalendarProvider)


def test_unknown_name_on_create_raises():
    with pytest.raises(UnknownProviderError):
        ProviderRegistry().create("nope")


@pytest.mark.parametrize("bad", ["", "Fake", "has space", "x" * 40, None])
def test_invalid_names_rejected(bad):
    with pytest.raises(ValueError):
        ProviderRegistry().register(bad, FakeCalendarProvider)  # type: ignore[arg-type]


def test_non_callable_factory_rejected():
    with pytest.raises(TypeError):
        ProviderRegistry().register("fake", "not callable")  # type: ignore[arg-type]


def test_names_feed_load_bindings():
    reg = ProviderRegistry()
    reg.register("fake", FakeCalendarProvider)
    reg.register("fake_two", FakeCalendarProvider)
    assert reg.names() == frozenset({"fake", "fake_two"})
    binding = copy.deepcopy(BASE)
    binding["provider"] = "fake_two"
    raw = json.dumps({"bindings": {"test-alpha": binding}})
    assert load_bindings(raw, reg.names())["test-alpha"].provider == "fake_two"
    binding["provider"] = "google"
    with pytest.raises(BindingConfigError) as info:
        load_bindings(json.dumps({"bindings": {"test-alpha": binding}}), reg.names())
    assert info.value.field == "provider"


def test_default_registry_is_empty_until_an_adapter_lands():
    assert default_registry().names() == frozenset()


def test_names_is_a_snapshot():
    reg = ProviderRegistry()
    names = reg.names()
    reg.register("fake", FakeCalendarProvider)
    assert names == frozenset()
