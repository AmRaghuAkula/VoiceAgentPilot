"""Provider registry (spec section 6.4): `provider` names -> adapter factories.

Adding a vendor means one adapter module, one `register` line in
`default_registry()` and one conformance fixture. The binding loader rejects any
provider name the registry does not know (`load_bindings(raw, registry.names())`).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from calendar_tools.core.ports import CalendarProvider

ProviderFactory = Callable[..., CalendarProvider]

_NAME = re.compile(r"[a-z][a-z0-9_]{1,31}")


class UnknownProviderError(LookupError):
    """`create` was asked for a name nobody registered."""


class ProviderRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, ProviderFactory] = {}

    def register(self, name: str, factory: ProviderFactory) -> None:
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError("provider name must match [a-z][a-z0-9_]{1,31}")
        if not callable(factory):
            raise TypeError("factory must be callable")
        if name in self._factories:
            raise ValueError("provider name already registered")
        self._factories[name] = factory

    def names(self) -> frozenset[str]:
        return frozenset(self._factories)

    def create(self, name: str, /, **deps: Any) -> CalendarProvider:
        # `name` is positional-only so a factory may itself take a `name` dependency.
        try:
            factory = self._factories[name]
        except KeyError:
            raise UnknownProviderError("unknown provider name") from None
        return factory(**deps)


def default_registry() -> ProviderRegistry:
    """The production registry. Empty until UC08b registers `google`."""
    return ProviderRegistry()
