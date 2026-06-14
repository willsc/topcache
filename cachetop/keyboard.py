"""Non-blocking single-keypress reader for the live UI.

Puts the terminal in cbreak mode and reads keys on a background thread, pushing
normalised tokens onto a queue the render loop drains. No-op (and reports
unavailable) when stdin isn't a tty, so headless/piped use still works.
"""

from __future__ import annotations

import os
import queue
import select
import sys
import threading

try:
    import termios
    import tty
    _HAVE_TERMIOS = True
except ImportError:  # non-POSIX
    _HAVE_TERMIOS = False

# Normalised key tokens
UP, DOWN, LEFT, RIGHT = "up", "down", "left", "right"
ENTER, ESC, BACK = "enter", "esc", "back"

_ARROWS = {b"[A": UP, b"[B": DOWN, b"[C": RIGHT, b"[D": LEFT,
           b"OA": UP, b"OB": DOWN, b"OC": RIGHT, b"OD": LEFT}


class KeyReader:
    def __init__(self) -> None:
        self.available = _HAVE_TERMIOS and sys.stdin.isatty()
        self._fd = sys.stdin.fileno() if self.available else -1
        self._old = None
        self._q: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "KeyReader":
        if not self.available:
            return self
        self._old = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)  # disables ICANON + ECHO
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self.available and self._old is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)

    def _run(self) -> None:
        while not self._stop.is_set():
            r, _, _ = select.select([self._fd], [], [], 0.1)
            if not r:
                continue
            ch = os.read(self._fd, 1)
            if not ch:
                continue
            self._q.put(self._classify(ch))

    def _classify(self, ch: bytes) -> str:
        if ch == b"\x1b":
            # Could be a lone Esc or the start of an arrow sequence.
            r, _, _ = select.select([self._fd], [], [], 0.06)
            if r:
                seq = os.read(self._fd, 2)
                return _ARROWS.get(seq, ESC)
            return ESC
        if ch in (b"\r", b"\n"):
            return ENTER
        if ch in (b"\x7f", b"\x08"):
            return BACK
        try:
            return ch.decode("utf-8", "ignore")
        except Exception:
            return ""

    def drain(self) -> list[str]:
        out = []
        try:
            while True:
                out.append(self._q.get_nowait())
        except queue.Empty:
            pass
        return out
