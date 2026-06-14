"""CPU isolation awareness.

Reads which CPUs the kernel set aside for isolated workloads (`isolcpus=`,
`nohz_full=`) so cachetop can keep its *own* threads and child processes off
those cores. The counter-read IPIs that `perf -I` issues to an isolated core
are inherent to measuring it (and the whole point of the tool); what we avoid
here is the avoidable noise — the Python interpreter, the perf CSV parsing,
the /proc scan and the render loop landing on a trading core and stealing
hot-path cycles.

All reads are best-effort and read-only. On anything unexpected (no isolation
configured, unreadable sysfs, no affinity syscall) we report "no isolation"
and the tool behaves exactly as it did before.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_CPU = "/sys/devices/system/cpu"


def _read(path: str) -> str:
    try:
        with open(path, "r") as fh:
            return fh.read()
    except OSError:
        return ""


def _parse_cpu_list(text: str) -> set[int]:
    """Parse a kernel cpu-list like "0-3,5,7-8" into a set of ints.

    Tolerant of the flag words that modern `isolcpus=` accepts before the list
    (e.g. "domain,managed_irq,2-5"): non-numeric tokens are simply skipped.
    """
    out: set[int] = set()
    for part in text.strip().split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo_s, _, hi_s = part.partition("-")
            try:
                lo, hi = int(lo_s), int(hi_s)
            except ValueError:
                continue
            if 0 <= lo <= hi:
                out.update(range(lo, hi + 1))
        else:
            try:
                out.add(int(part))
            except ValueError:
                continue
    return out


def _from_cmdline() -> set[int]:
    """Fallback: parse isolcpus=/nohz_full= straight from /proc/cmdline."""
    iso: set[int] = set()
    for tok in _read("/proc/cmdline").split():
        if tok.startswith("isolcpus="):
            iso |= _parse_cpu_list(tok.split("=", 1)[1])
        elif tok.startswith("nohz_full="):
            iso |= _parse_cpu_list(tok.split("=", 1)[1])
    return iso


@dataclass
class Topology:
    online: set[int]
    isolated: set[int]   # isolcpus ∪ nohz_full, restricted to online cpus

    @property
    def housekeeping(self) -> set[int]:
        """Online cpus the tool may run on. Never empty (falls back to all)."""
        hk = self.online - self.isolated
        return hk or set(self.online)


def detect() -> Topology:
    online = _parse_cpu_list(_read(f"{_CPU}/online"))
    if not online:
        online = set(range(os.cpu_count() or 1))
    # sysfs exposes the effective lists directly; cmdline is the fallback.
    isolated = _parse_cpu_list(_read(f"{_CPU}/isolated"))
    isolated |= _parse_cpu_list(_read(f"{_CPU}/nohz_full"))
    if not isolated:
        isolated = _from_cmdline()
    return Topology(online=online, isolated=(isolated & online) or isolated)


def pin_self_to(cpus: set[int]) -> bool:
    """Confine this process (current thread + everything spawned after) to `cpus`.

    Threads created later and child processes inherit this affinity, so calling
    it once on the main thread before any worker/subprocess starts is enough.
    """
    if not cpus or not hasattr(os, "sched_setaffinity"):
        return False
    try:
        os.sched_setaffinity(0, cpus)
        return True
    except OSError:
        return False


def fmt_cpus(cpus: set[int]) -> str:
    """Render a cpu set compactly as ranges, e.g. {0,1,2,5} -> "0-2,5"."""
    if not cpus:
        return "(none)"
    nums = sorted(cpus)
    parts: list[str] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = n
    parts.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(parts)
