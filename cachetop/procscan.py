"""Lightweight /proc scanning: per-process CPU%, memory, threads, names.

Used both to rank candidate processes (top-style, by CPU) and to enrich the
per-process cache table. Stateful: CPU% is computed from jiffy deltas between
successive sample() calls.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

_CLK_TCK = os.sysconf("SC_CLK_TCK") or 100
_PAGE_KB = (os.sysconf("SC_PAGE_SIZE") or 4096) // 1024


@dataclass
class ProcInfo:
    pid: int
    comm: str
    cmd: str
    cpu_pct: float        # % of one core (can exceed 100 for multithreaded)
    rss_kb: int
    nthreads: int


def _read(path: str) -> str:
    try:
        with open(path, "r") as fh:
            return fh.read()
    except OSError:
        return ""


def _stat_fields(pid: int) -> tuple[int, str, int] | None:
    """Return (utime+stime ticks, comm, num_threads) from /proc/pid/stat."""
    raw = _read(f"/proc/{pid}/stat")
    if not raw:
        return None
    # comm is parenthesised and may contain spaces/parens; split around it.
    lo, hi = raw.find("("), raw.rfind(")")
    if lo < 0 or hi < 0:
        return None
    comm = raw[lo + 1:hi]
    rest = raw[hi + 2:].split()
    # rest[0] is field 3 (state); field N -> rest[N-3].
    try:
        utime = int(rest[11])      # field 14
        stime = int(rest[12])      # field 15
        nthreads = int(rest[17])   # field 20
    except (IndexError, ValueError):
        return None
    return utime + stime, comm, nthreads


def _rss_kb(pid: int) -> int:
    parts = _read(f"/proc/{pid}/statm").split()
    if len(parts) < 2:
        return 0
    try:
        return int(parts[1]) * _PAGE_KB
    except ValueError:
        return 0


def _cmdline(pid: int, comm: str) -> str:
    raw = _read(f"/proc/{pid}/cmdline")
    if not raw:
        return comm
    # nul-separated args; also flatten any embedded newlines/tabs (e.g. -c scripts)
    return " ".join(raw.replace("\0", " ").split()) or comm


def cpu_of(tid: int) -> int | None:
    """Last CPU this thread executed on (field 39 'processor' of /proc/tid/stat).

    A point-in-time value: exact for pinned (e.g. isolated trading) threads,
    approximate for ones that migrate. Used only to attribute a thread's cycles
    to isolated vs housekeeping cores.
    """
    raw = _read(f"/proc/{tid}/stat")
    if not raw:
        return None
    hi = raw.rfind(")")
    if hi < 0:
        return None
    rest = raw[hi + 2:].split()
    # rest[0] is field 3; field N -> rest[N-3]. processor is field 39.
    try:
        return int(rest[36])
    except (IndexError, ValueError):
        return None


def tgid_of(tid: int) -> int | None:
    """Map a thread id to its process id via /proc/tid/status Tgid."""
    for line in _read(f"/proc/{tid}/status").splitlines():
        if line.startswith("Tgid:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


class ProcScanner:
    def __init__(self) -> None:
        self._prev_ticks: dict[int, int] = {}
        self._prev_t: float | None = None

    def sample(self) -> dict[int, ProcInfo]:
        """Snapshot all processes; CPU% is over the interval since last call."""
        now = time.monotonic()
        dt = (now - self._prev_t) if self._prev_t is not None else None
        self._prev_t = now

        out: dict[int, ProcInfo] = {}
        new_ticks: dict[int, int] = {}
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            sf = _stat_fields(pid)
            if sf is None:
                continue
            ticks, comm, nthreads = sf
            new_ticks[pid] = ticks
            cpu_pct = 0.0
            if dt and dt > 0 and pid in self._prev_ticks:
                dticks = ticks - self._prev_ticks[pid]
                cpu_pct = 100.0 * dticks / (dt * _CLK_TCK)
            out[pid] = ProcInfo(
                pid=pid, comm=comm, cmd=_cmdline(pid, comm),
                cpu_pct=max(0.0, cpu_pct), rss_kb=_rss_kb(pid),
                nthreads=nthreads,
            )
        self._prev_ticks = new_ticks
        return out

    def top_by_cpu(self, n: int, exclude: set[int] | None = None) -> list[ProcInfo]:
        procs = [p for p in self.sample().values()
                 if not exclude or p.pid not in exclude]
        procs.sort(key=lambda p: p.cpu_pct, reverse=True)
        return procs[:n]
