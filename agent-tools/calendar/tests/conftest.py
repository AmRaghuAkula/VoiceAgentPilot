"""Suite-wide test configuration.

`obs.STRICT = True` for the whole suite (plan UC02a, spec rev 3.2 section 10): a
non-OK outcome logged without a diagnostic raises, so a missed path fails a test
instead of quietly logging `unclassified`.

UC02b adds the JWT fixtures: an RSA key pair, `make_token`, and a `respx` JWKS
route (helpers in `tests/fakes/jwt_tokens.py`). No test reaches the network.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from calendar_tools import obs
from tests.fakes import jwt_tokens
from tests.fakes.clock import FakeClock

obs.STRICT = True


@pytest.fixture
def rsa_key() -> rsa.RSAPrivateKey:
    return jwt_tokens.private_key("a")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def make_token(clock: FakeClock) -> Callable[..., str]:
    """`make_token(**claims)`: a token valid at the fake clock's current time."""

    def _make(**kwargs: Any) -> str:
        return jwt_tokens.make_token(clock.now().timestamp(), **kwargs)

    return _make


@pytest.fixture
def jwks_stub() -> jwt_tokens.JwksStub:
    return jwt_tokens.JwksStub()
