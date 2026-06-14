"""L3 occupancy via the kernel resctrl interface (Intel RDT/CMT, AMD QoS).

This is the one true "how full is my slice" signal x86 exposes, and only for
the shared L3. It reports, in bytes, how much L3 a task group currently
occupies. Many parts (including most client CPUs) do not implement it; callers
must check `available` first.

Mounting note: on a low-latency box `/sys/fs/resctrl` is *not* mounted
implicitly. resctrl is where CAT/cache-partitioning lives, so mounting it (or
creating groups) can disturb an existing allocation setup. We therefore only
mount when the caller explicitly opts in, do it via a direct `mount(2)` (no
shell), and unmount on exit only if *we* were the ones who mounted it. If
resctrl is already mounted we use it as-is and never touch the mount.
"""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass

_RESCTRL = "/sys/fs/resctrl"


@dataclass
class Occupancy:
    total_bytes: int
    per_domain: dict[str, int]


def _libc() -> ctypes.CDLL | None:
    try:
        return ctypes.CDLL("libc.so.6", use_errno=True)
    except OSError:
        return None


def _mount_resctrl() -> bool:
    libc = _libc()
    if libc is None:
        return False
    # mount(source, target, fstype, flags=0, data=NULL)
    rc = libc.mount(b"resctrl", _RESCTRL.encode(), b"resctrl", 0, None)
    return rc == 0


def _umount_resctrl() -> None:
    libc = _libc()
    if libc is None:
        return
    libc.umount2(_RESCTRL.encode(), 0)


def _usable() -> bool:
    """True once L3 occupancy monitoring is actually exposed under the mount."""
    return os.path.isdir(os.path.join(_RESCTRL, "info", "L3_MON"))


class Resctrl:
    def __init__(self) -> None:
        self._mon_group: str | None = None
        self._we_mounted = False

    def ensure_mounted(self, allow_mount: bool) -> bool:
        """Make resctrl usable. Only mounts when `allow_mount` is set.

        Returns True if L3 occupancy monitoring is available afterwards.
        """
        if _usable():
            return True
        if not allow_mount:
            return False
        if "resctrl" not in _read("/proc/filesystems"):
            return False
        try:
            os.makedirs(_RESCTRL, exist_ok=True)
        except OSError:
            return False
        if not _mount_resctrl():
            return False
        if _usable():
            self._we_mounted = True
            return True
        # mounted but no L3_MON: undo our mount, we gained nothing.
        _umount_resctrl()
        return False

    def attach_pid(self, pid: int) -> bool:
        """Create a monitoring group for `pid` so we read just its occupancy."""
        name = f"cachetop_{os.getpid()}"
        path = os.path.join(_RESCTRL, "mon_groups", name)
        try:
            os.makedirs(path, exist_ok=True)
            with open(os.path.join(path, "tasks"), "w") as fh:
                fh.write(str(pid))
            self._mon_group = name
            return True
        except OSError:
            self._mon_group = None
            return False

    def _mon_data_dir(self) -> str:
        if self._mon_group:
            return os.path.join(_RESCTRL, "mon_groups", self._mon_group, "mon_data")
        return os.path.join(_RESCTRL, "mon_data")

    def read(self) -> Occupancy | None:
        """Sum llc_occupancy across all L3 domains for the active group."""
        base = self._mon_data_dir()
        if not os.path.isdir(base):
            return None
        per_domain: dict[str, int] = {}
        for dom in sorted(os.listdir(base)):
            if not dom.startswith("mon_L3_"):
                continue
            val = _read(os.path.join(base, dom, "llc_occupancy")).strip()
            try:
                per_domain[dom] = int(val)
            except ValueError:
                continue
        if not per_domain:
            return None
        return Occupancy(total_bytes=sum(per_domain.values()), per_domain=per_domain)

    def cleanup(self) -> None:
        if self._mon_group:
            try:
                os.rmdir(os.path.join(_RESCTRL, "mon_groups", self._mon_group))
            except OSError:
                pass
            self._mon_group = None
        # Only undo the mount if this process is the one that created it.
        if self._we_mounted:
            _umount_resctrl()
            self._we_mounted = False


def _read(path: str) -> str:
    try:
        with open(path, "r") as fh:
            return fh.read()
    except OSError:
        return ""
