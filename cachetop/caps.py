"""Capability detection: CPU vendor, performance-monitoring PMUs, resctrl.

Everything here is best-effort and read-only. The collector and UI adapt to
whatever is reported, so a missing capability degrades gracefully rather than
crashing.
"""

from __future__ import annotations

import os
import platform
import shutil
from dataclasses import dataclass, field

_SYSFS_PMU = "/sys/bus/event_source/devices"
_RESCTRL = "/sys/fs/resctrl"


@dataclass
class Caps:
    vendor: str = "unknown"          # "intel" | "amd" | "unknown"
    cpu_model: str = "unknown"
    hostname: str = "unknown"
    pmus: list[str] = field(default_factory=list)   # e.g. ["cpu_core", "cpu_atom"]
    hybrid: bool = False
    perf_path: str | None = None
    resctrl_supported: bool = False  # kernel knows the fs
    resctrl_mounted: bool = False
    resctrl_l3_mon: bool = False     # L3 occupancy monitoring available
    isolated_cpus: set[int] = field(default_factory=set)  # isolcpus/nohz_full

    @property
    def l3_occupancy_available(self) -> bool:
        return self.resctrl_mounted and self.resctrl_l3_mon


def _read(path: str) -> str:
    try:
        with open(path, "r") as fh:
            return fh.read()
    except OSError:
        return ""


def _detect_vendor_and_model() -> tuple[str, str]:
    vendor, model = "unknown", platform.processor() or "unknown"
    info = _read("/proc/cpuinfo")
    for line in info.splitlines():
        if line.startswith("vendor_id") and ":" in line:
            v = line.split(":", 1)[1].strip()
            if v == "GenuineIntel":
                vendor = "intel"
            elif v == "AuthenticAMD":
                vendor = "amd"
        elif line.startswith("model name") and ":" in line:
            model = line.split(":", 1)[1].strip()
        if vendor != "unknown" and model != "unknown":
            break
    return vendor, model


def _detect_pmus() -> list[str]:
    """Return core PMUs that can count cache events.

    On Intel hybrid parts these are cpu_core / cpu_atom / cpu_lowpower; on most
    other chips it is a single "cpu". We only keep devices that look like CPU
    PMUs (have a numeric `type` and an `events`/`format` directory).
    """
    pmus: list[str] = []
    try:
        names = sorted(os.listdir(_SYSFS_PMU))
    except OSError:
        return ["cpu"]
    for name in names:
        if name != "cpu" and not name.startswith("cpu_"):
            continue
        base = os.path.join(_SYSFS_PMU, name)
        if os.path.isfile(os.path.join(base, "type")) and os.path.isdir(
            os.path.join(base, "format")
        ):
            pmus.append(name)
    return pmus or ["cpu"]


def _detect_resctrl() -> tuple[bool, bool, bool]:
    supported = "resctrl" in _read("/proc/filesystems")
    mounted = os.path.ismount(_RESCTRL) or os.path.isdir(
        os.path.join(_RESCTRL, "info")
    )
    l3_mon = os.path.isdir(os.path.join(_RESCTRL, "info", "L3_MON"))
    if mounted and not l3_mon:
        # mon_features file lists e.g. "llc_occupancy"
        feats = _read(os.path.join(_RESCTRL, "info", "L3_MON", "mon_features"))
        l3_mon = "llc_occupancy" in feats
    return supported, mounted, l3_mon


def detect() -> Caps:
    vendor, model = _detect_vendor_and_model()
    pmus = _detect_pmus()
    supported, mounted, l3_mon = _detect_resctrl()
    return Caps(
        vendor=vendor,
        cpu_model=model,
        hostname=platform.node() or "unknown",
        pmus=pmus,
        hybrid=len(pmus) > 1,
        perf_path=shutil.which("perf"),
        resctrl_supported=supported,
        resctrl_mounted=mounted,
        resctrl_l3_mon=l3_mon,
    )
