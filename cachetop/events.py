"""Event catalog and runtime probing.

We describe each metric we'd *like* to show as an ordered list of candidate
perf event names tagged by vendor. At startup we ask perf which of them are
actually supported (and on which PMU, since hybrid parts expose different
counters per core type) and keep only the ones that count. This keeps the tool
portable: the generic events (L1-dcache*, LLC*, dTLB*, instructions, cycles)
carry the dashboard on any x86 chip, while the vendor-specific L2/eviction
events light up only where the hardware confirms them.
"""

from __future__ import annotations

import csv
import io
import subprocess
from dataclasses import dataclass, field

# Logical cache levels, in display order.
LEVELS = ["L1D", "L1I", "L2", "L3", "TLB"]

# role is one of: access, miss, evict, ctx
# vendor is one of: any, intel, amd  (filtered against detected vendor)
_CATALOG: list[tuple[str, str, str, list[str]]] = [
    # level, role, vendor, candidate perf events (first supported wins per PMU)
    ("L1D", "access", "any",   ["L1-dcache-loads"]),
    ("L1D", "miss",   "any",   ["L1-dcache-load-misses"]),
    # L1 line replacements ~= L1 evictions (dirty + clean). Intel exposes this
    # directly; AMD has no clean software-visible equivalent, so it shows n/a.
    ("L1D", "evict",  "intel", ["l1d.replacement"]),

    ("L1I", "miss",   "any",   ["L1-icache-load-misses"]),

    # L2: Intel l2_rqsts.* is solid. AMD names vary by Zen generation; these are
    # best-effort and silently dropped if the PPR for your part differs.
    ("L2", "access",  "intel", ["l2_rqsts.references"]),
    ("L2", "access",  "amd",   ["l2_request_g1.all_no_prefetch"]),
    ("L2", "miss",    "intel", ["l2_rqsts.miss"]),
    ("L2", "miss",    "amd",   ["l2_cache_req_stat.ic_dc_miss_in_l2"]),
    # Non-silent L2 line-outs = dirty writebacks/evictions to L3/memory.
    ("L2", "evict",   "intel", ["l2_lines_out.non_silent"]),

    ("L3", "access",  "any",   ["LLC-loads"]),
    ("L3", "miss",    "any",   ["LLC-load-misses"]),

    ("TLB", "miss",   "any",   ["dTLB-load-misses"]),
    ("TLB", "imiss",  "any",   ["iTLB-load-misses"]),

    ("ctx", "instructions", "any", ["instructions"]),
    ("ctx", "cycles",       "any", ["cycles"]),
]


@dataclass
class Channel:
    """A confirmed (pmu, event) pair we will stream and the metric it feeds."""
    level: str
    role: str
    event: str           # base event name, e.g. "L1-dcache-loads"
    pmu: str             # e.g. "cpu_core" or "cpu"
    key: str = field(init=False)

    def __post_init__(self) -> None:
        self.key = f"{self.pmu}/{self.event}/"


def expected_slots(vendor: str) -> list[tuple[str, str]]:
    """Ordered (level, role) pairs the catalog tries for this vendor.

    Used by the --show-events diagnostic to report which metrics the hardware
    supports vs. which are missing on a given server part.
    """
    seen: list[tuple[str, str]] = []
    for level, role, vtag, _ in _CATALOG:
        if vtag != "any" and vtag != vendor:
            continue
        if (level, role) not in seen:
            seen.append((level, role))
    return seen


def _candidates_for(vendor: str) -> list[tuple[str, str, str]]:
    """Flatten the catalog to (level, role, event), filtered by vendor."""
    out: list[tuple[str, str, str]] = []
    for level, role, vtag, events in _CATALOG:
        if vtag != "any" and vtag != vendor:
            continue
        for ev in events:
            out.append((level, role, ev))
    return out


def _parse_supported(text: str) -> dict[str, set[str]]:
    """From `perf stat -x,` output return {event_base: {pmus that counted}}."""
    supported: dict[str, set[str]] = {}
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if not row or row[0].startswith("#"):
            continue
        # Without -I the fields are: value, unit, event, runtime, pct[, metric...]
        # The event field contains the pmu prefix, e.g. cpu_core/L1-dcache-loads/
        value = None
        event = None
        for cell in row:
            c = cell.strip()
            if "/" in c and c.endswith("/"):
                event = c
                break
        if event is None:
            continue
        value = row[0].strip()
        ok = value not in ("<not supported>", "<not counted>", "")
        try:
            float(value.replace(",", "")) if ok else None
        except ValueError:
            ok = False
        inner = event.strip("/")
        if "/" in inner:
            pmu, base = inner.split("/", 1)
        else:
            pmu, base = "cpu", inner
        if ok:
            supported.setdefault(base, set()).add(pmu)
    return supported


def probe(perf_path: str, vendor: str, timeout: float = 5.0) -> list[Channel]:
    """Ask perf which catalog events actually count, return live Channels.

    For each (level, role) we keep, per PMU, only the first candidate that the
    kernel reports as supported and counting.
    """
    cands = _candidates_for(vendor)
    event_list = ",".join(dict.fromkeys(ev for _, _, ev in cands))  # de-dup, keep order

    text = _run_probe(perf_path, event_list, timeout)
    supported = _parse_supported(text)
    if not supported:
        # A single bad event name can abort the whole invocation; fall back to
        # probing events one at a time so the good ones still register.
        supported = _probe_individually(perf_path, event_list, timeout)

    chosen: dict[tuple[str, str, str], Channel] = {}  # (level, role, pmu) -> Channel
    for level, role, ev in cands:
        for pmu in supported.get(ev, set()):
            slot = (level, role, pmu)
            if slot not in chosen:
                chosen[slot] = Channel(level=level, role=role, event=ev, pmu=pmu)
    return list(chosen.values())


def _run_probe(perf_path: str, event_list: str, timeout: float) -> str:
    try:
        proc = subprocess.run(
            [perf_path, "stat", "-a", "-x", ",", "-e", event_list,
             "--", "sleep", "0.2"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=timeout, check=False,
        )
        return proc.stdout
    except (subprocess.TimeoutExpired, OSError):
        return ""


def _probe_individually(perf_path: str, event_list: str, timeout: float) -> dict[str, set[str]]:
    supported: dict[str, set[str]] = {}
    for ev in event_list.split(","):
        text = _run_probe(perf_path, ev, timeout)
        for base, pmus in _parse_supported(text).items():
            supported.setdefault(base, set()).update(pmus)
    return supported
