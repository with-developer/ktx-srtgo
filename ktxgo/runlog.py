"""Keep a copy of everything a run prints in ~/.ktxgo/logs/.

The search loop redraws the terminal on every attempt, so once a run ends —
or its terminal is closed — nothing tells what happened overnight. `start()`
mirrors stdout and stderr into one file per run, each line stamped with the
wall-clock time it was printed. Colour codes and screen clears are dropped.

`paused()` keeps output out of the file while it is active: inquirer redraws
its menus on every key press, and the search loop reprints an identical table
every second or so.
"""

from __future__ import annotations

import atexit
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TextIO

from .config import DATA_DIR

LOG_DIR = DATA_DIR / "logs"

# CSI sequences (colours, cursor moves, clears), OSC sequences, and the
# remaining two-byte escapes.
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")


def _clean(line: str) -> str:
    line = _ANSI_RE.sub("", line).rstrip("\r")
    # Text before a bare carriage return was overwritten on screen.
    return line.rsplit("\r", 1)[-1].rstrip()


class _Writer:
    def __init__(self, path: Path) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self.path = path
        self._file = open(fd, "a", encoding="utf-8")  # noqa: SIM115
        self.paused = 0

    def emit(self, line: str) -> None:
        line = _clean(line)
        if not line:
            return
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            self._file.write(f"{stamp} {line}\n")
            self._file.flush()
        except (OSError, ValueError):
            # A full disk or a file closed at exit must not stop the run.
            pass

    def close(self) -> None:
        try:
            self._file.close()
        except OSError:
            pass


class _Tee:
    """A text stream that writes through to `stream` and copies lines to the log."""

    def __init__(self, stream: TextIO, writer: _Writer) -> None:
        self._stream = stream
        self._writer = writer
        self._partial = ""

    def write(self, text: str) -> int:
        written = self._stream.write(text)
        if isinstance(text, str) and not self._writer.paused:
            *lines, self._partial = (self._partial + text).split("\n")
            for line in lines:
                self._writer.emit(line)
        return written

    def flush(self) -> None:
        self._stream.flush()
        # Every partial line this program flushes is a prompt waiting for
        # input; the answer is echoed by the terminal, never written here.
        self.commit()

    def commit(self) -> None:
        if self._partial:
            line, self._partial = self._partial, ""
            self._writer.emit(line)

    def __getattr__(self, name: str) -> object:
        return getattr(self._stream, name)


_writer: _Writer | None = None
_tees: list[_Tee] = []


def start() -> Path | None:
    """Start copying stdout/stderr to a new log file. Returns its path.

    Returns None, and the run goes on without a log, if the file cannot be
    created.
    """
    global _writer
    if _writer is not None:
        return _writer.path
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        writer = _Writer(LOG_DIR / f"{datetime.now():%Y%m%d-%H%M%S}.log")
    except OSError as exc:
        print(f"[ktxgo] 로그 파일을 만들 수 없어 기록 없이 진행합니다: {exc}", file=sys.stderr)
        return None
    _writer = writer
    stdout, stderr = _Tee(sys.stdout, writer), _Tee(sys.stderr, writer)
    _tees.extend((stdout, stderr))
    sys.stdout, sys.stderr = stdout, stderr
    writer.emit(f"=== 실행 시작 (pid {os.getpid()}) ===")
    atexit.register(_finish)
    return writer.path


def note(text: str) -> None:
    """Write `text` to the log only, not to the screen."""
    if _writer is None:
        return
    _commit_all()
    for line in text.split("\n"):
        _writer.emit(line)


@contextmanager
def paused(active: bool = True) -> Iterator[None]:
    """Keep what is printed inside the block out of the log (when `active`)."""
    if _writer is None or not active:
        yield
        return
    _commit_all()
    _writer.paused += 1
    try:
        yield
    finally:
        _writer.paused -= 1


def _commit_all() -> None:
    for tee in _tees:
        tee.commit()


def _finish() -> None:
    if _writer is None:
        return
    _commit_all()
    # A run killed by the terminal closing (SIGHUP) never gets here, so a log
    # without this line ended abruptly.
    _writer.emit("=== 실행 종료 ===")
    _writer.close()
