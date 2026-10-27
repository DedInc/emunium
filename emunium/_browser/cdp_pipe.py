"""CDP over ``--remote-debugging-pipe``: load the extension into Google Chrome.

Google Chrome ignores ``--load-extension``; ``Extensions.loadUnpacked`` over the
debugging pipe still works when ``--enable-unsafe-extension-debugging`` is set.
Messages are JSON terminated by ``\\0``. Chrome reads commands from the first
pipe and writes replies to the second. Closing the pipe makes Chrome exit, so
one ``CdpPipe`` stays open for the life of the browser.

Lifecycle: ``prepare()`` -> ``Popen(wrap_args(args), **popen_kwargs())`` ->
``after_spawn()`` -> ``call()`` / ``load_unpacked()`` -> ``close()``.

Windows hands Chrome two inheritable handles named by
``--remote-debugging-io-pipes=<read>,<write>`` and restricted to those handles
by the ``handle_list`` startup attribute.

POSIX Chromium wants the pipe on child fds 3 and 4. ``subprocess.Popen`` keeps
``pass_fds`` at their own numbers and only renumbers with ``preexec_fn``, which
is unsafe with threads. So Chrome's ends are kept at fds >= 10 and a
``/bin/sh`` trampoline reopens them onto 3 and 4, then replaces itself with
Chrome through ``exec`` (the pid stays Chrome's). The trampoline names them as ``/dev/fd/<n>`` paths:
dash, Debian's ``/bin/sh``, rejects fd numbers above 9 in ``<&n`` and ``n<&-``
("Bad fd number"). For the same reason it cannot close the originals, so
Chrome inherits two spare copies of its own ends; Chromium closes descriptors
it does not map when it launches its own children.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from emunium.errors import LaunchError

CDP_PIPE_FLAGS = ("--remote-debugging-pipe", "--enable-unsafe-extension-debugging")
IO_PIPES_FLAG = "--remote-debugging-io-pipes"
DEFAULT_CALL_TIMEOUT = 15.0
READ_CHUNK_BYTES = 65536
MESSAGE_TERMINATOR = b"\0"
# POSIX: Chrome reads commands from fd 3 and writes replies to fd 4.
CHILD_READ_FD = 3
CHILD_WRITE_FD = 4
# Chrome's ends live at or above this fd in our process, clear of 0-4.
TRAMPOLINE_MIN_FD = 10
TRAMPOLINE_SHELL = "/bin/sh"


def trampoline_args(args: list[str], read_fd: int, write_fd: int) -> list[str]:
    """``args`` behind a ``/bin/sh`` that puts ``read_fd``/``write_fd`` on 3/4."""
    if min(read_fd, write_fd) < TRAMPOLINE_MIN_FD:
        raise ValueError(f"trampoline fds must be >= {TRAMPOLINE_MIN_FD}")
    # Only integers are formatted in; Chrome's argv travels as "$@".
    script = (
        f"exec {CHILD_READ_FD}</dev/fd/{int(read_fd)} "
        f'{CHILD_WRITE_FD}>/dev/fd/{int(write_fd)} && exec "$@"'
    )
    return [TRAMPOLINE_SHELL, "-c", script, "sh", *args]


class CdpPipe:
    """One debugging pipe pair to one Chrome process."""

    def __init__(self) -> None:
        # Our ends: we write commands, we read replies.
        self._command_write: int | None = None
        self._reply_read: int | None = None
        # Chrome's ends: it reads commands, it writes replies.
        self._child_read: int | None = None
        self._child_write: int | None = None
        self._child_handles: tuple[int, int] | None = None
        self._cond = threading.Condition()
        self._write_lock = threading.Lock()
        self._replies: dict[int, dict[str, Any]] = {}
        self._pending: set[int] = set()
        self._next_id = 0
        self._reader: threading.Thread | None = None
        self._peer_closed = False
        self._closed = False

    # -- setup -----------------------------------------------------------------

    def prepare(self) -> list[str]:
        """Create the pipes; return the flags Chrome needs to use them."""
        if self._closed:
            raise LaunchError("CDP pipe is closed")
        if self._child_read is not None or self._reader is not None:
            raise LaunchError("CDP pipe is already prepared")
        child_read, child_write = self._create_pipes()
        if sys.platform == "win32":
            try:
                handles = (
                    _inheritable_handle(child_read),
                    _inheritable_handle(child_write),
                )
            except OSError as exc:
                self._close_all()
                raise LaunchError(f"could not share the CDP pipe: {exc}") from exc
            self._child_handles = handles
            return [*CDP_PIPE_FLAGS, f"{IO_PIPES_FLAG}={handles[0]},{handles[1]}"]
        try:
            self._child_read = _move_to_high_fd(child_read)
            self._child_write = _move_to_high_fd(child_write)
        except OSError as exc:
            self._close_all()
            raise LaunchError(f"could not share the CDP pipe: {exc}") from exc
        return list(CDP_PIPE_FLAGS)

    def wrap_args(self, args: list[str]) -> list[str]:
        """The Chrome command line to spawn: unchanged on Windows."""
        if sys.platform == "win32":
            return list(args)
        if self._child_read is None or self._child_write is None:
            raise LaunchError("CDP pipe is not prepared")
        return trampoline_args(args, self._child_read, self._child_write)

    def popen_kwargs(self) -> dict[str, Any]:
        """Extra ``subprocess.Popen`` arguments that hand Chrome its pipe ends."""
        if sys.platform == "win32":
            if self._child_handles is None:
                raise LaunchError("CDP pipe is not prepared")
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.lpAttributeList = {"handle_list": list(self._child_handles)}
            # A non-empty handle_list makes Popen inherit handles anyway, limited
            # to the list (plus std handles); close_fds=False only adds a warning.
            return {"close_fds": True, "startupinfo": startupinfo}
        if self._child_read is None or self._child_write is None:
            raise LaunchError("CDP pipe is not prepared")
        # Popen clears close-on-exec on pass_fds in the child; close_fds stays
        # True so nothing else of ours reaches Chrome.
        return {"pass_fds": (self._child_read, self._child_write)}

    def after_spawn(self) -> None:
        """Drop Chrome's ends here and start reading replies."""
        if self._reply_read is None or self._reader is not None:
            raise LaunchError("CDP pipe is not prepared")
        # Chrome now holds its own copies; ours would mask its exit as no EOF.
        self._close_child_ends()
        self._reader = threading.Thread(
            target=self._read_loop,
            args=(self._reply_read,),
            name="emunium-cdp-pipe",
            daemon=True,
        )
        self._reader.start()

    # -- commands --------------------------------------------------------------

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = DEFAULT_CALL_TIMEOUT,
    ) -> dict[str, Any]:
        """Send one command and return its ``result``."""
        with self._cond:
            self._raise_if_unusable(method)
            self._next_id += 1
            message_id = self._next_id
            self._pending.add(message_id)
        message = {"id": message_id, "method": method, "params": params or {}}
        try:
            self._send(json.dumps(message).encode("utf-8") + MESSAGE_TERMINATOR, method)
            reply = self._wait_reply(message_id, method, timeout)
        finally:
            with self._cond:
                self._pending.discard(message_id)
                self._replies.pop(message_id, None)
        error = reply.get("error")
        if error is not None:
            detail = error.get("message") if isinstance(error, dict) else None
            raise LaunchError(f"CDP {method} failed: {detail or error}", method=method)
        result = reply.get("result")
        return result if isinstance(result, dict) else {}

    def load_unpacked(self, path: str | os.PathLike[str]) -> str:
        """Load an unpacked extension directory; return its extension id."""
        method = "Extensions.loadUnpacked"
        result = self.call(method, {"path": str(Path(path).resolve())})
        extension_id = result.get("id")
        if not isinstance(extension_id, str) or not extension_id:
            raise LaunchError(f"CDP {method} returned no extension id", method=method)
        return extension_id

    def close(self) -> None:
        """Close our ends; Chrome exits once it sees the command pipe close."""
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._cond.notify_all()
        self._close_child_ends()
        with self._write_lock:
            if self._command_write is not None:
                _close_quietly(self._command_write)
                self._command_write = None
        if self._reader is None and self._reply_read is not None:
            _close_quietly(self._reply_read)
            self._reply_read = None
        # Otherwise the reader owns the read end and closes it at EOF: closing
        # an fd another thread is blocked reading could hand that number to an
        # unrelated file before the read returns.

    # -- internals -------------------------------------------------------------

    def _raise_if_unusable(self, method: str) -> None:
        if self._closed:
            raise LaunchError(f"CDP {method}: pipe is closed", method=method)
        if self._reader is None:
            raise LaunchError(f"CDP {method}: pipe is not connected", method=method)
        if self._peer_closed:
            raise LaunchError(f"CDP {method}: browser closed the pipe", method=method)

    def _send(self, payload: bytes, method: str) -> None:
        with self._write_lock:
            fd = self._command_write
            if fd is None:
                raise LaunchError(f"CDP {method}: pipe is closed", method=method)
            view = memoryview(payload)
            try:
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
            except OSError as exc:
                raise LaunchError(
                    f"CDP {method}: browser closed the pipe ({exc})", method=method
                ) from exc

    def _wait_reply(
        self, message_id: int, method: str, timeout: float
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        with self._cond:
            while message_id not in self._replies:
                self._raise_if_unusable(method)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LaunchError(
                        f"CDP {method}: no reply within {timeout:g}s", method=method
                    )
                self._cond.wait(remaining)
            return self._replies[message_id]

    def _read_loop(self, fd: int) -> None:
        buffer = b""
        try:
            while True:
                try:
                    chunk = os.read(fd, READ_CHUNK_BYTES)
                except OSError:
                    return
                if not chunk:
                    return
                buffer += chunk
                *frames, buffer = buffer.split(MESSAGE_TERMINATOR)
                for frame in frames:
                    self._deliver(frame)
        finally:
            _close_quietly(fd)
            with self._cond:
                self._reply_read = None
                self._peer_closed = True
                self._cond.notify_all()

    def _deliver(self, frame: bytes) -> None:
        try:
            message = json.loads(frame)
        except ValueError:
            return
        # Events carry no id; replies to abandoned (timed-out) calls are dropped.
        if not isinstance(message, dict):
            return
        message_id = message.get("id")
        with self._cond:
            if isinstance(message_id, int) and message_id in self._pending:
                self._replies[message_id] = message
                self._cond.notify_all()

    def _create_pipes(self) -> tuple[int, int]:
        """Create both pipes; return Chrome's ends ``(read, write)``."""
        child_read, command_write = os.pipe()
        try:
            reply_read, child_write = os.pipe()
        except BaseException:
            # EMFILE and the like: the first pipe is not recorded yet, so
            # _close_all would never see it.
            _close_quietly(child_read)
            _close_quietly(command_write)
            raise
        self._child_read, self._command_write = child_read, command_write
        self._reply_read, self._child_write = reply_read, child_write
        return child_read, child_write

    def _close_child_ends(self) -> None:
        for fd in (self._child_read, self._child_write):
            if fd is not None:
                _close_quietly(fd)
        self._child_read = self._child_write = None

    def _close_all(self) -> None:
        self._close_child_ends()
        for fd in (self._command_write, self._reply_read):
            if fd is not None:
                _close_quietly(fd)
        self._command_write = self._reply_read = None


def _inheritable_handle(fd: int) -> int:
    if sys.platform != "win32":
        raise OSError("OS handles exist only on Windows")
    import msvcrt

    handle = msvcrt.get_osfhandle(fd)
    os.set_handle_inheritable(handle, True)
    return handle


def _move_to_high_fd(fd: int) -> int:
    """Re-home ``fd`` at >= TRAMPOLINE_MIN_FD (close-on-exec); close the original."""
    if sys.platform == "win32":
        raise OSError("fd renumbering is for POSIX")
    import fcntl

    moved = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, TRAMPOLINE_MIN_FD)
    os.close(fd)
    return moved


def _close_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass
