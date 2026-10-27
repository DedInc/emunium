from __future__ import annotations

from typing import Any

import pytest

from emunium import errors
from emunium.errors import (
    BridgeDisconnectedError,
    BridgeError,
    BridgeProtocolError,
    BridgeTimeoutError,
    ClickVerificationError,
    CommandError,
    CommandTimeoutError,
    ContentUnavailableError,
    CSPBlockedError,
    ElementNotFoundError,
    EmuniumError,
    ExpectationError,
    InvalidArgumentError,
    LaunchError,
    NativeInputError,
    NavigationError,
    NavigationTimeoutError,
    ScriptError,
    StaleElementError,
    TargetClosedError,
    WaitTimeoutError,
    error_from_reply,
)

# bridge-protocol.md section 3, "Error codes".
CODE_TABLE = [
    ("stale_element", StaleElementError),
    ("no_target", TargetClosedError),
    ("target_closed", TargetClosedError),
    ("content_unavailable", ContentUnavailableError),
    ("navigation_timeout", NavigationTimeoutError),
    ("navigation_failed", NavigationError),
    ("timeout", CommandTimeoutError),
    ("wait_timeout", WaitTimeoutError),
    ("script_error", ScriptError),
    ("csp_blocked", CSPBlockedError),
    ("invalid_argument", InvalidArgumentError),
    ("unknown_condition", InvalidArgumentError),
    ("unknown_method", BridgeProtocolError),
    ("internal", CommandError),
]

# python-internals.md hierarchy: class -> every base it must satisfy.
HIERARCHY = [
    (BridgeError, (EmuniumError, Exception)),
    (BridgeDisconnectedError, (BridgeError, ConnectionError)),
    (BridgeTimeoutError, (BridgeError, TimeoutError)),
    (BridgeProtocolError, (BridgeError,)),
    (CommandError, (EmuniumError,)),
    (StaleElementError, (CommandError,)),
    (TargetClosedError, (CommandError,)),
    (ContentUnavailableError, (CommandError,)),
    (InvalidArgumentError, (CommandError, ValueError)),
    (CommandTimeoutError, (CommandError, TimeoutError)),
    (WaitTimeoutError, (CommandTimeoutError, CommandError, TimeoutError)),
    (NavigationError, (CommandError, RuntimeError)),
    (
        NavigationTimeoutError,
        (NavigationError, CommandError, RuntimeError, TimeoutError),
    ),
    (ScriptError, (CommandError, RuntimeError)),
    (CSPBlockedError, (ScriptError, CommandError, RuntimeError)),
    (NativeInputError, (EmuniumError, RuntimeError)),
    (ClickVerificationError, (NativeInputError, RuntimeError)),
    (LaunchError, (EmuniumError, RuntimeError)),
    (ElementNotFoundError, (EmuniumError, LookupError)),
    (ExpectationError, (EmuniumError, AssertionError)),
]

ALL_CLASSES = [EmuniumError] + [cls for cls, _ in HIERARCHY]


@pytest.mark.parametrize("code,expected", CODE_TABLE)
def test_error_from_reply_maps_code_to_exact_class(code, expected):
    exc = error_from_reply(code, "boom", "querySelector")
    assert type(exc) is expected
    assert exc.code == code
    assert exc.message == "boom"
    assert exc.method == "querySelector"


def test_unknown_code_maps_to_command_error_and_keeps_code():
    exc = error_from_reply("no_such_code", "weird", "pageInfo")
    assert type(exc) is CommandError
    assert exc.code == "no_such_code"
    assert exc.message == "weird"
    assert exc.method == "pageInfo"


def test_method_defaults_to_none():
    exc = error_from_reply("stale_element", "gone")
    assert exc.method is None


def test_error_from_reply_is_raisable_and_catchable_as_builtin():
    with pytest.raises(TimeoutError):
        raise error_from_reply("navigation_timeout", "slow", "navigate")
    with pytest.raises(ValueError):
        raise error_from_reply("unknown_condition", "bad cond", "waitForSelector")
    with pytest.raises(EmuniumError):
        raise error_from_reply("unknown_method", "nope", "frobnicate")


@pytest.mark.parametrize("cls,bases", HIERARCHY)
def test_hierarchy(cls, bases):
    exc = cls("msg")
    assert isinstance(exc, EmuniumError)
    for base in bases:
        assert isinstance(exc, base), f"{cls.__name__} is not a {base.__name__}"


def test_named_builtin_relationships():
    assert isinstance(NavigationTimeoutError("x"), TimeoutError)
    assert isinstance(NavigationTimeoutError("x"), NavigationError)
    assert isinstance(WaitTimeoutError("x"), CommandTimeoutError)
    assert isinstance(WaitTimeoutError("x"), TimeoutError)
    assert isinstance(BridgeDisconnectedError("x"), ConnectionError)
    assert isinstance(BridgeTimeoutError("x"), TimeoutError)
    assert isinstance(InvalidArgumentError("x"), ValueError)
    assert isinstance(CSPBlockedError("x"), ScriptError)
    assert not isinstance(StaleElementError("x"), TimeoutError)
    assert not isinstance(BridgeProtocolError("x"), CommandError)
    assert not isinstance(CommandError("x"), BridgeError)


@pytest.mark.parametrize("cls", ALL_CLASSES)
def test_every_instance_has_code_method_message(cls):
    exc = cls("only message")
    for attr in ("code", "method", "message"):
        assert hasattr(exc, attr), f"{cls.__name__} instance lacks .{attr}"
    assert exc.message == "only message"
    assert exc.method is None


def test_message_is_the_exception_text():
    exc = error_from_reply(
        "script_error", "ReferenceError: x is not defined", "executeScript"
    )
    assert "ReferenceError: x is not defined" in str(exc)


@pytest.mark.parametrize("code", ["timeout", "navigation_timeout", "wait_timeout"])
def test_oserror_derived_str_is_exactly_the_message(code):
    assert str(error_from_reply(code, "too slow", "navigate")) == "too slow"
    assert str(BridgeDisconnectedError("socket gone")) == "socket gone"


def test_command_error_code_defaults_to_its_protocol_code():
    assert CommandError("x").code == "internal"
    assert StaleElementError("x").code == "stale_element"
    assert WaitTimeoutError("x").code == "wait_timeout"
    assert CSPBlockedError("x").code == "csp_blocked"
    for cls in (BridgeError, BridgeProtocolError, NativeInputError, LaunchError):
        assert cls("x").code is None


def test_pickle_keeps_code_and_method():
    import pickle

    exc = pickle.loads(pickle.dumps(error_from_reply("no_target", "closed", "click")))
    assert type(exc) is TargetClosedError
    assert (exc.code, exc.method, exc.message) == ("no_target", "click", "closed")


def test_reply_codes_cover_the_protocol_table():
    for code, expected in CODE_TABLE:
        assert issubclass(type(error_from_reply(code, "")), expected)
    assert set(errors.__dict__) >= {cls.__name__ for cls in ALL_CLASSES}


# -- hints and the 3.1 classes (dx-api.md section 2) ---------------------------


@pytest.mark.parametrize("cls", ALL_CLASSES)
def test_hint_defaults_to_none_and_str_stays_the_message(cls):
    exc = cls("plain")
    assert exc.hint is None
    assert str(exc) == "plain"
    assert exc.args == ("plain",)


@pytest.mark.parametrize("cls", ALL_CLASSES)
def test_hint_is_appended_to_str_only(cls):
    exc = cls("boom", "some_code", "click", hint="try this")
    assert exc.hint == "try this"
    assert exc.message == "boom"
    assert str(exc) == "boom\nhint: try this"
    assert exc.args == ("boom",)
    assert (exc.code, exc.method) == ("some_code", "click")


def test_hint_is_keyword_only():
    positional: tuple[Any, ...] = ("boom", None, None, "positional hint")
    with pytest.raises(TypeError):
        EmuniumError(*positional)


@pytest.mark.parametrize("cls", ALL_CLASSES)
def test_pickle_and_copy_keep_the_hint(cls):
    import copy
    import pickle

    exc = cls("boom", "c", "m", hint="look here")
    for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
        loaded = pickle.loads(pickle.dumps(exc, protocol))
        assert type(loaded) is cls
        assert (loaded.message, loaded.code, loaded.method, loaded.hint) == (
            "boom",
            "c",
            "m",
            "look here",
        )
        assert str(loaded) == str(exc)
    assert copy.copy(exc).hint == "look here"
    assert copy.deepcopy(exc).hint == "look here"
    # A pickle without a hint (made before 3.1 or without one) loads with None.
    assert pickle.loads(pickle.dumps(cls("x"))).hint is None


class SelectorError(EmuniumError, LookupError):
    """A subclass with its own constructor and attribute (module level: picklable)."""

    def __init__(self, message="", code=None, method=None, *, hint=None, selector=None):
        super().__init__(message, code, method, hint=hint)
        self.selector = selector


def test_pickle_keeps_subclass_attributes():
    import copy
    import pickle

    exc = SelectorError("boom", "c", "m", hint="h", selector="#go")
    for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
        loaded = pickle.loads(pickle.dumps(exc, protocol))
        assert type(loaded) is SelectorError
        assert (loaded.message, loaded.code, loaded.method) == ("boom", "c", "m")
        assert (loaded.hint, loaded.selector) == ("h", "#go")
        assert str(loaded) == "boom\nhint: h"
    assert copy.deepcopy(exc).selector == "#go"


def test_subclasses_defined_elsewhere_accept_a_hint():
    from emunium._attach.build import ExtensionBuildError
    from emunium._bridge.transport import _NotSent

    for cls in (ExtensionBuildError, _NotSent):
        exc = cls("boom", hint="h")
        assert str(exc) == "boom\nhint: h"
        assert isinstance(exc, EmuniumError)


def test_old_builtin_excepts_still_catch_the_new_classes():
    with pytest.raises(LookupError):
        raise ElementNotFoundError("'#go' matched without an element")
    with pytest.raises(AssertionError):
        raise ExpectationError("expected visible")
    with pytest.raises(TimeoutError):
        raise WaitTimeoutError("Element not found after 1s: '#go'", hint="h")
    try:
        raise ElementNotFoundError("gone")
    except LookupError as caught:
        assert caught.code is None and caught.hint is None


def test_new_names_are_exported():
    import emunium

    for name in ("ElementNotFoundError", "ExpectationError"):
        assert name in errors.__all__
        assert name in emunium.__all__
        assert getattr(emunium, name) is getattr(errors, name)
