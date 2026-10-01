"""Errors shared by the core and the adapters (plan P1 error semantics).

Messages never contain a phone number, a message body or a secret.
"""

from __future__ import annotations


class NotifierError(Exception):
    """Base class for notifier failures."""


class NotifierUnavailable(NotifierError):
    """The provider couldn't be reached or didn't answer in time.

    ``maybe_sent`` is True when the request may have reached the provider
    (timeout or dropped connection after sending): the result is ``send_unconfirmed``.
    It is False when nothing went out: the result is ``send_failed``.
    """

    def __init__(self, *, maybe_sent: bool, reason: str = "") -> None:
        super().__init__(reason or ("maybe sent" if maybe_sent else "not sent"))
        self.maybe_sent = maybe_sent


class NotifierAuthError(NotifierError):
    """The provider refused our credentials (send_failed)."""

    def __init__(self, *, error_code: int | None = None) -> None:
        super().__init__(f"provider auth error (code {error_code})")
        self.error_code = error_code


class NotifierRejected(NotifierError):
    """The provider gave a definite error (send_failed). Only its numeric code is kept."""

    def __init__(self, *, status: int | None = None, error_code: int | None = None) -> None:
        super().__init__(f"provider rejected (status {status}, code {error_code})")
        self.status = status
        self.error_code = error_code


class NotifierConfigError(NotifierError):
    """The notifier configuration or its credentials are invalid (unavailable)."""


class SecretStoreUnavailable(Exception):
    """The secret store couldn't be read, or a secret is missing (unavailable)."""


class StateStoreUnavailable(Exception):
    """The state store couldn't be reached (unavailable)."""


class ConfigError(Exception):
    """A setting is missing or invalid (unavailable)."""
