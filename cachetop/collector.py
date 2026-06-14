"""Live counter collection by streaming `perf stat -I`.

We launch one long-lived perf process counting all probed channels system-wide
(or against a target pid) and parse its interval CSV output. Each `-I` line is
already the delta for that interval, keyed by `pmu/event/`. We group lines by
their interval timestamp and yield one Frame per interval.
"""

from __future__ import annotations

import csv
import signal
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field

from .events import Channel


@dataclass
class Frame:
    t: float                                  # perf interval timestamp (seconds)
    dt: float                                 # width of this interval (seconds)
    counts: dict[str, float] = field(default_factory=dict)   # channel.key -> count
    unsupported: set[str] = field(default_factory=set)       # keys reporting n/a


class Collector:
    def __init__(self, perf_path: str, channels: list[Channel],
                 interval_ms: int = 1000, pid: int | None = None,
                 cpu_list: str | None = None) -> None:
        self.perf_path = perf_path
        self.channels = channels
        self.interval_ms = interval_ms
        self.pid = pid
        self.cpu_list = cpu_list
        self._proc: subprocess.Popen | None = None

    def _argv(self) -> list[str]:
        # Pass bare event names and let perf expand each across the PMUs that
        # support it (cpu_core/cpu_atom/... on hybrid, cpu on others). Prefixing
        # a PMU onto a symbolic event name is a perf syntax error.
        events = ",".join(dict.fromkeys(ch.event for ch in self.channels))
        argv = [self.perf_path, "stat", "-x", ",", "-I", str(self.interval_ms),
                "-e", events]
        if self.pid is not None:
            argv += ["-p", str(self.pid)]
        elif self.cpu_list:
            argv += ["-C", self.cpu_list]
        else:
            argv += ["-a"]
        return argv

    def frames(self) -> Iterator[Frame]:
        """Yield one Frame per completed interval until the process ends."""
        self._proc = subprocess.Popen(
            self._argv(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        cur_ts: float | None = None
        prev_ts: float | None = None
        counts: dict[str, float] = {}
        unsupported: set[str] = set()

        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parsed = _parse_line(line)
            if parsed is None:
                continue
            ts, key, value, ok = parsed
            if cur_ts is not None and ts != cur_ts:
                # interval boundary -> emit the frame we just finished
                dt = (cur_ts - prev_ts) if prev_ts is not None else (self.interval_ms / 1000.0)
                yield Frame(t=cur_ts, dt=dt, counts=counts, unsupported=unsupported)
                prev_ts, counts, unsupported = cur_ts, {}, set()
            cur_ts = ts
            if ok:
                counts[key] = value
            else:
                unsupported.add(key)

        if cur_ts is not None:
            dt = (cur_ts - prev_ts) if prev_ts is not None else (self.interval_ms / 1000.0)
            yield Frame(t=cur_ts, dt=dt, counts=counts, unsupported=unsupported)

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.send_signal(signal.SIGINT)
                self._proc.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    self._proc.kill()
                except OSError:
                    pass


def _parse_line(line: str) -> tuple[float, str, float, bool] | None:
    """Parse one `perf stat -x, -I` row -> (ts, channel_key, value, ok)."""
    row = next(csv.reader([line]), None)
    if not row or len(row) < 4:
        return None
    try:
        ts = float(row[0])
    except ValueError:
        return None
    # perf -x, -I columns: ts, value, unit, event, runtime, pct[, metric...]
    if len(row) < 4:
        return None
    raw_val = row[1].strip()
    event = row[3].strip()
    if not event:
        return None
    # On hybrid parts the event carries its PMU, e.g. "cpu_core/L1-dcache-loads/";
    # on single-PMU parts it is the bare name.
    inner = event.strip("/")
    pmu, base = inner.split("/", 1) if "/" in inner else ("cpu", inner)
    key = f"{pmu}/{base}/"
    if raw_val in ("<not supported>", "<not counted>", ""):
        return ts, key, 0.0, False
    try:
        return ts, key, float(raw_val.replace(",", "")), True
    except ValueError:
        return ts, key, 0.0, False
