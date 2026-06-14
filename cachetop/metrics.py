"""Turn raw Frames into per-level, per-PMU derived metrics with history.

Keeps short rolling histories per (level, pmu) so the UI can draw sparklines of
miss% and access rate without re-reading anything.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .collector import Frame
from .events import Channel

HISTORY = 60  # samples retained for sparklines


@dataclass
class Cell:
    level: str
    pmu: str
    access_rate: float | None = None   # accesses / second
    miss_rate: float | None = None     # misses / second
    evict_rate: float | None = None    # evictions (writebacks/replacements) / s
    miss_pct: float | None = None      # misses / accesses * 100
    hit_pct: float | None = None


@dataclass
class Snapshot:
    t: float
    cells: dict[tuple[str, str], Cell] = field(default_factory=dict)  # (level,pmu)
    ipc: dict[str, float] = field(default_factory=dict)              # pmu -> IPC


class MetricsState:
    def __init__(self, channels: list[Channel]) -> None:
        # (level, role, pmu) -> channel key
        self._map: dict[tuple[str, str, str], str] = {
            (c.level, c.role, c.pmu): c.key for c in channels
        }
        self.levels = [lv for lv in ["L1D", "L1I", "L2", "L3"]
                       if any(c.level == lv for c in channels)]
        self.pmus = sorted({c.pmu for c in channels})
        self.miss_hist: dict[tuple[str, str], deque[float]] = {}
        self.acc_hist: dict[tuple[str, str], deque[float]] = {}

    def _get(self, frame: Frame, level: str, role: str, pmu: str) -> float | None:
        key = self._map.get((level, role, pmu))
        if key is None:
            return None
        return frame.counts.get(key)

    def update(self, frame: Frame) -> Snapshot:
        dt = frame.dt or 1.0
        snap = Snapshot(t=frame.t)
        for level in self.levels:
            for pmu in self.pmus:
                acc = self._get(frame, level, "access", pmu)
                miss = self._get(frame, level, "miss", pmu)
                evi = self._get(frame, level, "evict", pmu)
                if acc is None and miss is None and evi is None:
                    continue
                cell = Cell(level=level, pmu=pmu)
                if acc is not None:
                    cell.access_rate = acc / dt
                if miss is not None:
                    cell.miss_rate = miss / dt
                if evi is not None:
                    cell.evict_rate = evi / dt
                if acc and miss is not None and acc > 0:
                    cell.miss_pct = 100.0 * miss / acc
                    cell.hit_pct = 100.0 - cell.miss_pct
                snap.cells[(level, pmu)] = cell
                self.miss_hist.setdefault((level, pmu), deque(maxlen=HISTORY)).append(
                    cell.miss_pct if cell.miss_pct is not None else 0.0
                )
                self.acc_hist.setdefault((level, pmu), deque(maxlen=HISTORY)).append(
                    cell.access_rate or 0.0
                )
        # IPC context
        for pmu in self.pmus:
            ins = self._get(frame, "ctx", "instructions", pmu)
            cyc = self._get(frame, "ctx", "cycles", pmu)
            if ins is not None and cyc:
                snap.ipc[pmu] = ins / cyc
        return snap
