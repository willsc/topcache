"""Per-process cache monitoring: a top-style engine.

Pipeline:
  1. ProcScanner ranks processes by CPU (cheap, from /proc).
  2. ThreadCollector runs `perf stat --per-thread -p <topN>` and streams accurate
     per-thread cache counters for every event we probed.
  3. ProcessMonitor aggregates threads -> process and derives per-level metrics
     (rates, miss%, MPKI, evictions, IPC, P/E-core mix), refreshing the monitored
     set every few seconds so new heavy processes get picked up.

Why MPKI (misses per 1000 instructions): raw miss rates favour whichever process
simply runs more, so they're a poor way to compare cache *behaviour* across
processes. MPKI normalises by work done and is the standard cross-process metric.
"""

from __future__ import annotations

import csv
import signal
import subprocess
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field

from .events import Channel
from .procscan import ProcInfo, ProcScanner, cpu_of, tgid_of

CACHE_LEVELS = ["L1D", "L1I", "L2", "L3"]
HISTORY = 60

# Selectable ranking metrics (also cycled interactively with 's'/'S').
SORT_KEYS = ["l3_mpki", "l2_mpki", "l1d_mpki", "l3_miss", "l2_miss",
             "l1d_miss", "cpu", "ipc"]


def sort_value(pm: "ProcMetric", key: str) -> float:
    """Numeric ranking value for a process under the given sort key."""
    if key == "cpu":
        return pm.cpu_pct
    if key == "ipc":
        return pm.ipc or 0.0
    if key.endswith("_mpki"):
        lm = pm.levels.get(key[:-5].upper())
        return (lm.mpki or 0.0) if lm else 0.0
    if key.endswith("_miss"):
        lm = pm.levels.get(key[:-5].upper())
        return (lm.miss_rate or 0.0) if lm else 0.0
    return pm.cpu_pct


# ----------------------------------------------------------- thread collector ---

@dataclass
class ThreadFrame:
    t: float
    dt: float
    # tid -> {channel_key: value}
    rows: dict[int, dict[str, float]] = field(default_factory=dict)


class ThreadCollector:
    def __init__(self, perf_path: str, channels: list[Channel], pids: list[int],
                 interval_ms: int) -> None:
        self.perf_path = perf_path
        self.events = ",".join(dict.fromkeys(c.event for c in channels))
        self.pids = pids
        self.interval_ms = interval_ms
        self._proc: subprocess.Popen | None = None

    def _argv(self) -> list[str]:
        return [self.perf_path, "stat", "--per-thread",
                "-p", ",".join(str(p) for p in self.pids),
                "-x", ",", "-I", str(self.interval_ms), "-e", self.events]

    def frames(self) -> Iterator[ThreadFrame]:
        self._proc = subprocess.Popen(
            self._argv(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        cur_ts: float | None = None
        prev_ts: float | None = None
        rows: dict[int, dict[str, float]] = {}
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parsed = _parse_thread_line(line)
            if parsed is None:
                continue
            ts, tid, key, value = parsed
            if cur_ts is not None and ts != cur_ts:
                dt = (cur_ts - prev_ts) if prev_ts is not None else (self.interval_ms / 1000.0)
                yield ThreadFrame(t=cur_ts, dt=dt, rows=rows)
                prev_ts, rows = cur_ts, {}
            cur_ts = ts
            if value is not None:
                rows.setdefault(tid, {})[key] = value
        if cur_ts is not None and rows:
            dt = (cur_ts - prev_ts) if prev_ts is not None else (self.interval_ms / 1000.0)
            yield ThreadFrame(t=cur_ts, dt=dt, rows=rows)

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


def _parse_thread_line(line: str) -> tuple[float, int, str, float | None] | None:
    """perf --per-thread -x, -I columns: ts, comm-tid, value, unit, event, ..."""
    row = next(csv.reader([line]), None)
    if not row or len(row) < 5:
        return None
    try:
        ts = float(row[0])
    except ValueError:
        return None
    comm_tid = row[1]
    # comm may contain '-'; the tid is the trailing integer.
    head, _, tail = comm_tid.rpartition("-")
    if not tail.isdigit():
        return None
    tid = int(tail)
    raw_val = row[2].strip()
    event = row[4].strip()
    if not event:
        return None
    inner = event.strip("/")
    pmu, base = inner.split("/", 1) if "/" in inner else ("cpu", inner)
    key = f"{pmu}/{base}/"
    if raw_val in ("<not supported>", "<not counted>", ""):
        return ts, tid, key, None
    try:
        return ts, tid, key, float(raw_val.replace(",", ""))
    except ValueError:
        return ts, tid, key, None


# ------------------------------------------------------------- process metrics ---

@dataclass
class LevelMetric:
    access_rate: float | None = None
    miss_rate: float | None = None
    evict_rate: float | None = None
    miss_pct: float | None = None
    mpki: float | None = None       # misses per 1000 instructions


@dataclass
class ProcMetric:
    pid: int
    comm: str
    cmd: str
    cpu_pct: float
    nthreads: int
    rss_kb: int
    ins_rate: float = 0.0
    ipc: float | None = None
    pcore_frac: float | None = None   # share of cycles on P-cores (cpu_core)
    iso_frac: float | None = None     # share of cycles on isolated cores
    levels: dict[str, LevelMetric] = field(default_factory=dict)
    # Same per-level metrics, split by the core the work ran on. Populated only
    # when isolated cores are configured; threads whose last CPU we can't read
    # fall into neither bucket (they still count toward `levels`).
    levels_iso: dict[str, LevelMetric] = field(default_factory=dict)
    levels_shr: dict[str, LevelMetric] = field(default_factory=dict)


@dataclass
class ProcSnapshot:
    t: float
    procs: list[ProcMetric]
    n_monitored: int
    interval_ms: int


class ProcessMonitor:
    def __init__(self, perf_path: str, channels: list[Channel],
                 interval_ms: int = 1000, top_n: int = 20,
                 refresh_secs: float = 4.0, pinned: list[int] | None = None,
                 sort_key: str = "l3_mpki",
                 isolated: set[int] | None = None) -> None:
        self.perf_path = perf_path
        self.channels = channels
        self.interval_ms = interval_ms
        self.top_n = top_n
        self.refresh_secs = refresh_secs
        self.pinned = pinned
        self.sort_key = sort_key
        self.isolated = isolated or set()
        self.scanner = ProcScanner()
        self._own = _self_pids()
        self._map = {(c.level, c.role, c.pmu): c.key for c in channels}
        self._pmus = sorted({c.pmu for c in channels})
        self._cycle_keys = [self._map[("ctx", "cycles", pmu)] for pmu in self._pmus
                            if ("ctx", "cycles", pmu) in self._map]
        self._info: dict[int, ProcInfo] = {}
        self._tid2pid: dict[int, int] = {}
        self._mpki_hist: dict[int, deque[float]] = {}
        self._stop = False

    # -- discovery -------------------------------------------------------------
    def _discover(self) -> list[int]:
        if self.pinned:
            sample = self.scanner.sample()
            self._info = {p: sample[p] for p in self.pinned if p in sample}
            return list(self._info)
        tops = self.scanner.top_by_cpu(self.top_n, exclude=self._own)
        self._info = {p.pid: p for p in tops}
        return list(self._info)

    # -- aggregation -----------------------------------------------------------
    def _pid_of(self, tid: int) -> int | None:
        pid = self._tid2pid.get(tid)
        if pid is None:
            pid = tgid_of(tid)
            if pid is not None:
                self._tid2pid[tid] = pid
        return pid

    def _aggregate(self, tf: ThreadFrame) -> ProcSnapshot:
        # Sum thread counters into their owning process. When isolation is
        # configured, also keep separate counter sums for threads that last ran
        # on isolated vs other cores, so we can split cache misses by core class
        # (and weight ISO% by cycles).
        per_pid: dict[int, dict[str, float]] = {}
        per_pid_iso: dict[int, dict[str, float]] = {}
        per_pid_shr: dict[int, dict[str, float]] = {}
        iso_cyc: dict[int, float] = {}
        tot_cyc: dict[int, float] = {}
        for tid, counts in tf.rows.items():
            pid = self._pid_of(tid)
            if pid is None:
                continue
            _accum(per_pid.setdefault(pid, {}), counts)
            if not self.isolated:
                continue
            cpu = cpu_of(tid)
            if cpu is None:
                continue
            on_iso = cpu in self.isolated
            _accum((per_pid_iso if on_iso else per_pid_shr).setdefault(pid, {}),
                   counts)
            tcyc = sum(counts.get(k, 0.0) for k in self._cycle_keys)
            if tcyc > 0:
                tot_cyc[pid] = tot_cyc.get(pid, 0.0) + tcyc
                if on_iso:
                    iso_cyc[pid] = iso_cyc.get(pid, 0.0) + tcyc

        metrics: list[ProcMetric] = []
        dt = tf.dt or 1.0
        for pid, counts in per_pid.items():
            info = self._info.get(pid)
            pm = self._derive(pid, counts, dt, info)
            if pm is None:
                continue
            if tot_cyc.get(pid, 0.0) > 0:
                pm.iso_frac = iso_cyc.get(pid, 0.0) / tot_cyc[pid]
            if pid in per_pid_iso:
                pm.levels_iso = self._level_metrics(per_pid_iso[pid], dt)
            if pid in per_pid_shr:
                pm.levels_shr = self._level_metrics(per_pid_shr[pid], dt)
            metrics.append(pm)
        metrics.sort(key=self._sort_value, reverse=True)
        return ProcSnapshot(t=tf.t, procs=metrics, n_monitored=len(self._info),
                            interval_ms=self.interval_ms)

    def _sum(self, counts: dict[str, float], level: str, role: str) -> float | None:
        total, seen = 0.0, False
        for pmu in self._pmus:
            key = self._map.get((level, role, pmu))
            if key is not None and key in counts:
                total += counts[key]
                seen = True
        return total if seen else None

    def _derive(self, pid: int, counts: dict[str, float], dt: float,
                info: ProcInfo | None) -> ProcMetric | None:
        ins = self._sum(counts, "ctx", "instructions") or 0.0
        cyc = self._sum(counts, "ctx", "cycles") or 0.0
        pm = ProcMetric(
            pid=pid,
            comm=(info.comm if info else str(pid)),
            cmd=(info.cmd if info else ""),
            cpu_pct=(info.cpu_pct if info else 0.0),
            nthreads=(info.nthreads if info else 0),
            rss_kb=(info.rss_kb if info else 0),
            ins_rate=ins / dt,
            ipc=(ins / cyc if cyc > 0 else None),
        )
        # P-core share of cycles (cpu_core vs E-core PMUs)
        pcyc = counts.get(self._map.get(("ctx", "cycles", "cpu_core"), ""), 0.0)
        if cyc > 0:
            pm.pcore_frac = pcyc / cyc
        pm.levels = self._level_metrics(counts, dt)
        # sparkline history of the sort metric
        self._mpki_hist.setdefault(pid, deque(maxlen=HISTORY)).append(
            self._sort_value(pm) or 0.0)
        return pm

    def _level_metrics(self, counts: dict[str, float], dt: float) -> dict[str, LevelMetric]:
        """Per-level cache metrics from one bucket of summed counters.

        MPKI uses this bucket's own instruction count, so the isolated/shared
        splits are each normalised by the work actually done on those cores.
        """
        ins = self._sum(counts, "ctx", "instructions") or 0.0
        out: dict[str, LevelMetric] = {}
        for level in CACHE_LEVELS:
            acc = self._sum(counts, level, "access")
            miss = self._sum(counts, level, "miss")
            evi = self._sum(counts, level, "evict")
            if acc is None and miss is None and evi is None:
                continue
            lm = LevelMetric()
            if acc is not None:
                lm.access_rate = acc / dt
            if miss is not None:
                lm.miss_rate = miss / dt
            if evi is not None:
                lm.evict_rate = evi / dt
            if acc and miss is not None and acc > 0:
                lm.miss_pct = 100.0 * miss / acc
            if miss is not None and ins > 0:
                lm.mpki = 1000.0 * miss / ins
            out[level] = lm
        return out

    def hist(self, pid: int) -> deque[float]:
        return self._mpki_hist.get(pid, deque())

    def _sort_value(self, pm: ProcMetric) -> float:
        return sort_value(pm, self.sort_key)

    # -- main loop -------------------------------------------------------------
    def snapshots(self) -> Iterator[ProcSnapshot]:
        self.scanner.sample()   # prime CPU% baseline
        time.sleep(0.15)
        while not self._stop:
            pids = self._discover()
            if not pids:
                time.sleep(0.5)
                continue
            coll = ThreadCollector(self.perf_path, self.channels, pids,
                                   self.interval_ms)
            t0 = time.monotonic()
            try:
                for tf in coll.frames():
                    self._refresh_info()
                    yield self._aggregate(tf)
                    if self._stop or (time.monotonic() - t0) >= self.refresh_secs:
                        break
            finally:
                coll.stop()

    def _refresh_info(self) -> None:
        """Update CPU%/RSS/threads for the monitored set each frame (set stays
        fixed within a refresh window; only the metadata moves)."""
        sample = self.scanner.sample()
        for pid in list(self._info):
            if pid in sample:
                self._info[pid] = sample[pid]

    def stop(self) -> None:
        self._stop = True


def _accum(bucket: dict[str, float], counts: dict[str, float]) -> None:
    """Add one thread's counters into a per-process bucket in place."""
    for key, val in counts.items():
        bucket[key] = bucket.get(key, 0.0) + val


def _self_pids() -> set[int]:
    import os
    return {os.getpid(), os.getppid()}
