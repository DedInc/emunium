"""Exception hierarchy for emunium.

Contract: ``docs/architecture/python-internals.md`` (errors.py) and the error-code
table in ``docs/architecture/bridge-protocol.md`` section 3.

Every exception carries ``.message``, ``.code``, ``.method`` and ``.hint`` (a
suggested next step, or None). ``str(exc)`` is the message, plus a
``"\\nhint: ..."`` line when there is a hint (``docs/architecture/dx-api.md``
section 2). ``CommandError`` subclasses default ``.code`` to the protocol code
they represent; other classes default ``.code`` to ``None`` unless built by
``error_from_reply``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Type

__all__ = [
    "BridgeDisconnectedError",
    "BridgeError",
    "BridgeProtocolError",
    "BridgeTimeoutError",
    "CSPBlockedError",
    "ClickVerificationError",
    "CommandError",
    "CommandTimeoutError",
    "ContentUnavailableError",
    "ElementNotFoundError",
    "EmuniumError",
    "ExpectationError",
    "InvalidArgumentError",
    "LaunchError",
    "NativeInputError",
    "NavigationError",
    "NavigationTimeoutError",
    "ScriptError",
    "StaleElementError",
    "TargetClosedError",
    "WaitTimeoutError",
    "error_from_reply",
]


class EmuniumError(Exception):
    """Base class for every emunium exception."""

    default_code: str | None = None

    def __init__(
        self,
        message: str = "",
        code: str | None = None,
        method: str | None = None,
        *,
        hint: str | None = None,
    ) -> None:
        message = "" if message is None else str(message)
        super().__init__(message)
        self.message = message
        self.code = self.default_code if code is None else code
        self.method = method
        self.hint = hint

    def __str__(self) -> str:
        # OSError-derived subclasses (TimeoutError, ConnectionError) would
        # otherwise format their own errno/strerror text.
        if self.hint:
            return f"{self.message}\nhint: {self.hint}"
        return self.message

    def __reduce__(self) -> tuple[Any, ...]:
        # Keep code/method across pickling; the default only replays ``args``.
        # The instance dict travels as state (BaseException.__setstate__): it
        # restores the keyword-only hint and any attribute a subclass set.
        return (
            type(self),
            (self.message, self.code, self.method),
            dict(self.__dict__),
        )


# -- transport ---------------------------------------------------------------


class BridgeError(EmuniumError):
    """Failure of the WebSocket bridge itself, not of a command."""


class BridgeDisconnectedError(BridgeError, ConnectionError):
    """The extension connection dropped or was never established."""


class BridgeTimeoutError(BridgeError, TimeoutError):
    """No reply arrived in time."""


class BridgeProtocolError(BridgeError):
    """Malformed frame or ``unknown_method`` reply."""


# -- command replies ---------------------------------------------------------


class CommandError(EmuniumError):
    """A command reached the extension and replied ``ok: false``."""

    default_code = "internal"


class StaleElementError(CommandError):
    default_code = "stale_element"


class TargetClosedError(CommandError):
    default_code = "target_closed"


class ContentUnavailableError(CommandError):
    default_code = "content_unavailable"


class InvalidArgumentError(CommandError, ValueError):
    default_code = "invalid_argument"


class CommandTimeoutError(CommandError, TimeoutError):
    default_code = "timeout"


class WaitTimeoutError(CommandTimeoutError):
    """``waitForSelector`` timed out."""

    default_code = "wait_timeout"


class NavigationError(CommandError, RuntimeError):
    default_code = "navigation_failed"


class NavigationTimeoutError(NavigationError, TimeoutError):
    default_code = "navigation_timeout"


class ScriptError(CommandError, RuntimeError):
    default_code = "script_error"


class CSPBlockedError(ScriptError):
    default_code = "csp_blocked"


# -- local failures ----------------------------------------------------------


class NativeInputError(EmuniumError, RuntimeError):
    """Native (OS-level) input is unavailable or failed."""


class ClickVerificationError(NativeInputError):
    """A native click did not land on the intended element."""


class LaunchError(EmuniumError, RuntimeError):
    """Chrome or the extension could not be started."""


class ElementNotFoundError(EmuniumError, LookupError):
    """A target resolved to no element to act on (still a ``LookupError``)."""


class ExpectationError(EmuniumError, AssertionError):
    """An ``expect()`` assertion did not hold in time (still an ``AssertionError``).

    ``actual`` is the last value read (full, untruncated); ``expected`` is the
    asserted value. Both are ``None`` on instances not raised by ``expect()``.
    """

    actual: Any = None
    expected: Any = None


# bridge-protocol.md section 3, "Error codes".
_CODE_TO_CLASS: Dict[str, Type[EmuniumError]] = {
    "stale_element": StaleElementError,
    "no_target": TargetClosedError,
    "target_closed": TargetClosedError,
    "content_unavailable": ContentUnavailableError,
    "navigation_timeout": NavigationTimeoutError,
    "navigation_failed": NavigationError,
    "timeout": CommandTimeoutError,
    "wait_timeout": WaitTimeoutError,
    "script_error": ScriptError,
    "csp_blocked": CSPBlockedError,
    "invalid_argument": InvalidArgumentError,
    "unknown_condition": InvalidArgumentError,
    "unknown_method": BridgeProtocolError,
    "internal": CommandError,
}


def error_from_reply(
    code: str, message: str, method: Optional[str] = None
) -> EmuniumError:
    """Build the exception for an ``ok: false`` reply; unknown codes -> CommandError."""
    cls = _CODE_TO_CLASS.get(code, CommandError)
    return cls(message, code=code, method=method)
