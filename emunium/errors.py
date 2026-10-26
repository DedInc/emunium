"""Exception hierarchy for emunium.

Contract: ``docs/architecture/python-internals.md`` (errors.py) and the error-code
table in ``docs/architecture/bridge-protocol.md`` section 3.

Every exception carries ``.message`` (also ``str(exc)``), ``.code`` and
``.method``. ``CommandError`` subclasses default ``.code`` to the protocol code
they represent; other classes default ``.code`` to ``None`` unless built by
``error_from_reply``.
"""

from __future__ import annotations

from typing import Dict, Optional, Type

__all__ = [
    "EmuniumError",
    "BridgeError",
    "BridgeDisconnectedError",
    "BridgeTimeoutError",
    "BridgeProtocolError",
    "CommandError",
    "StaleElementError",
    "TargetClosedError",
    "ContentUnavailableError",
    "InvalidArgumentError",
    "CommandTimeoutError",
    "WaitTimeoutError",
    "NavigationError",
    "NavigationTimeoutError",
    "ScriptError",
    "CSPBlockedError",
    "NativeInputError",
    "ClickVerificationError",
    "LaunchError",
    "error_from_reply",
]


class EmuniumError(Exception):
    """Base class for every emunium exception."""

    default_code: Optional[str] = None

    def __init__(
        self,
        message: str = "",
        code: Optional[str] = None,
        method: Optional[str] = None,
    ) -> None:
        message = "" if message is None else str(message)
        super().__init__(message)
        self.message = message
        self.code = self.default_code if code is None else code
        self.method = method

    def __str__(self) -> str:
        # OSError-derived subclasses (TimeoutError, ConnectionError) would
        # otherwise format their own errno/strerror text.
        return self.message

    def __reduce__(self):
        # Keep code/method across pickling; the default only replays ``args``.
        return (type(self), (self.message, self.code, self.method))


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
