"""cachetop CLI.

Default mode is a `top`-style per-process cache table (L1/L2/L3 MPKI, miss%,
evictions, IPC per process). `--system` shows the aggregate system-wide view.
`--dump N` runs headless (text) for either mode, handy over SSH or in CI.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time

from . import __version__, caps as caps_mod
from .collector import Collector
from .events import probe
from .keyboard import KeyReader, UP, DOWN, LEFT, RIGHT, ENTER, ESC, BACK
from .metrics import MetricsState
from .procmon import ProcessMonitor, SORT_KEYS
from .procui import UIState, sorted_procs
from .resctrl import Resctrl
from . import ui, procui, topology

_SORT_KEYS = SORT_KEYS


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="cachetop",
        description="top-style per-process cache dashboard (perf + resctrl).",
    )
    p.add_argument("-i", "--interval", type=int, default=1000,
                   help="sample interval in milliseconds (default 1000)")
    p.add_argument("--dump", type=int, metavar="N", default=0,
                   help="headless: print N frames as text then exit (0=live UI)")
    p.add_argument("--version", action="version", version=f"cachetop {__version__}")
    p.add_argument("--no-pin", action="store_true",
                   help="do not pin cachetop off the isolated cores (default: "
                        "auto-pin the tool to housekeeping CPUs)")

    g = p.add_argument_group("per-process mode (default)")
    g.add_argument("-n", "--top", type=int, default=20,
                   help="number of processes to monitor/show (default 20)")
    g.add_argument("-s", "--sort", choices=_SORT_KEYS, default="l3_mpki",
                   help="ranking metric (default l3_mpki)")
    g.add_argument("--pids", default=None,
                   help="monitor exactly these pids (comma list); disables auto-discovery")
    g.add_argument("--refresh", type=float, default=4.0,
                   help="seconds between re-selecting the monitored set (default 4)")

    g2 = p.add_argument_group("system-wide mode")
    g2.add_argument("--system", action="store_true",
                    help="show the aggregate system-wide view instead of per-process")
    g2.add_argument("-p", "--pid", type=int, default=None,
                    help="(system mode) attach to a single process")
    g2.add_argument("-C", "--cpu", default=None,
                    help="(system mode) restrict to CPU list, e.g. 0-7")
    g2.add_argument("--no-resctrl", action="store_true",
                    help="(system mode) do not read L3 occupancy at all")
    g2.add_argument("--mount-resctrl", action="store_true",
                    help="(system mode) if /sys/fs/resctrl is not mounted, mount "
                         "it (root; may disturb an existing CAT setup) and "
                         "unmount on exit. Off by default; an already-mounted "
                         "resctrl is always used as-is.")
    return p.parse_args(argv)


def _apply_affinity(args: argparse.Namespace, topo: topology.Topology) -> None:
    """Keep the tool's own threads/children off the isolated trading cores.

    The perf counter-read IPIs that actually measure those cores are unaffected
    (and intended); this only stops cachetop's interpreter/parser/UI from being
    scheduled there. No-op when nothing is isolated or --no-pin is given.
    """
    if args.no_pin:
        return
    if not topo.isolated:
        return
    hk = topo.housekeeping
    if topology.pin_self_to(hk):
        print(f"cachetop: pinned tool to housekeeping CPUs "
              f"{topology.fmt_cpus(hk)}; isolated CPUs "
              f"{topology.fmt_cpus(topo.isolated)} are measured but not run on "
              f"by the tool.", file=sys.stderr)
    else:
        print("cachetop: warning: could not set CPU affinity; the tool may run "
              "on isolated cores. Pin it externally (taskset/cgroup) or pass "
              "--no-pin to silence this.", file=sys.stderr)


def _setup_common(topo: topology.Topology):
    caps = caps_mod.detect()
    caps.isolated_cpus = topo.isolated
    if caps.perf_path is None:
        sys.exit("error: `perf` not found in PATH. Install linux-tools / perf.")
    channels = probe(caps.perf_path, caps.vendor)
    if not channels:
        sys.exit("error: no cache events could be counted. Lower "
                 "/proc/sys/kernel/perf_event_paranoid (try -1) or run as root.")
    return caps, channels


# ------------------------------------------------------------- per-process ---

def _make_monitor(args, caps, channels) -> ProcessMonitor:
    try:
        pinned = ([int(x) for x in args.pids.split(",") if x.strip()]
                  if args.pids else None)
    except ValueError:
        sys.exit("error: --pids must be a comma-separated list of integers, "
                 f"got {args.pids!r}")
    return ProcessMonitor(
        caps.perf_path, channels, interval_ms=args.interval, top_n=args.top,
        refresh_secs=args.refresh, pinned=pinned, sort_key=args.sort,
        isolated=caps.isolated_cpus,
    )


def run_proc_dump(args, topo) -> int:
    caps, channels = _setup_common(topo)
    mon = _make_monitor(args, caps, channels)
    print(f"# cachetop {__version__} per-process  {caps.cpu_model}  "
          f"vendor={caps.vendor}  pmus={caps.pmus}")
    n = 0
    try:
        for snap in mon.snapshots():
            print(procui.render_proc_text(snap, mon, caps))
            print()
            n += 1
            if n >= args.dump:
                break
    except KeyboardInterrupt:
        pass
    finally:
        mon.stop()
    return 0


def _displayed_pids(snap, state, mon) -> list[int]:
    if snap is None:
        return []
    return [pm.pid for pm in sorted_procs(snap, state.sort_key)[: mon.top_n]]


def _handle_key(k: str, state: UIState, snap, mon) -> bool:
    """Mutate UI state for a keypress. Returns False to quit."""
    if k == "q":
        return False
    pids = _displayed_pids(snap, state, mon)

    if k in (UP, "k", DOWN, "j") and pids:
        cur = pids.index(state.selected_pid) if state.selected_pid in pids else 0
        cur += 1 if k in (DOWN, "j") else -1
        state.selected_pid = pids[max(0, min(cur, len(pids) - 1))]
    elif k in (ENTER, RIGHT, "l"):
        if state.selected_pid is None and pids:
            state.selected_pid = pids[0]
        state.view = "detail"
    elif k in (ESC, BACK, LEFT, "h"):
        state.view = "list"
    elif k in ("s", "S"):
        i = SORT_KEYS.index(state.sort_key) if state.sort_key in SORT_KEYS else 0
        i += 1 if k == "s" else -1
        state.sort_key = mon.sort_key = SORT_KEYS[i % len(SORT_KEYS)]
    elif k == "f":
        state.frozen = not state.frozen
    return True


def run_proc_live(args, topo) -> int:
    try:
        from rich.console import Console
        from rich.live import Live
        from rich.text import Text
    except ImportError:
        sys.exit("error: `rich` not installed. Use --dump N, or `pip install rich`.")
    caps, channels = _setup_common(topo)
    mon = _make_monitor(args, caps, channels)
    state = UIState(sort_key=args.sort)

    shared: dict[str, object] = {"snap": None}
    stop = threading.Event()

    def worker() -> None:
        for snap in mon.snapshots():
            shared["snap"] = snap
            if stop.is_set():
                break

    th = threading.Thread(target=worker, daemon=True)
    th.start()

    console = Console()
    displayed = None
    try:
        with KeyReader() as kb, Live(console=console, screen=True,
                                     auto_refresh=False) as live:
            while not stop.is_set():
                for k in kb.drain():
                    if not _handle_key(k, state, displayed, mon):
                        stop.set()
                        break
                if not state.frozen:
                    displayed = shared["snap"]
                if displayed is not None:
                    if state.selected_pid not in _displayed_pids(displayed, state, mon):
                        pids = _displayed_pids(displayed, state, mon)
                        state.selected_pid = pids[0] if pids else None
                    live.update(procui.build_proc_renderable(displayed, mon, caps,
                                                             state), refresh=True)
                else:
                    live.update(Text("  collecting per-process counters…",
                                     style="dim"), refresh=True)
                time.sleep(0.08)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        mon.stop()
        th.join(timeout=2)
    return 0


# ------------------------------------------------------------- system-wide ---

def _setup_system(args, topo):
    caps, channels = _setup_common(topo)
    rc = None
    if not args.no_resctrl and caps.resctrl_supported:
        rc = Resctrl()
        if rc.ensure_mounted(allow_mount=args.mount_resctrl):
            caps.resctrl_mounted = caps.resctrl_l3_mon = True
            if args.pid is not None:
                rc.attach_pid(args.pid)
        else:
            rc = None
            if not args.mount_resctrl:
                print("cachetop: L3 occupancy unavailable (resctrl not mounted). "
                      "Pass --mount-resctrl to mount it.", file=sys.stderr)
    return caps, channels, MetricsState(channels), rc


def _read_occ(rc, caps):
    return rc.read() if (rc and caps.l3_occupancy_available) else None


def run_system(args, topo) -> int:
    caps, channels, state, rc = _setup_system(args, topo)
    collector = Collector(caps.perf_path, channels, args.interval, args.pid, args.cpu)
    if args.dump > 0:
        n = 0
        try:
            for frame in collector.frames():
                print(ui.render_text(state.update(frame), state, caps,
                                     _read_occ(rc, caps)))
                n += 1
                if n >= args.dump:
                    break
        except KeyboardInterrupt:
            pass
        finally:
            collector.stop()
            if rc:
                rc.cleanup()
        return 0
    try:
        from rich.console import Console
        from rich.live import Live
    except ImportError:
        sys.exit("error: `rich` not installed. Use --dump N for headless mode.")
    console = Console()
    try:
        with Live(console=console, screen=True, auto_refresh=False) as live:
            for frame in collector.frames():
                live.update(ui.build_renderable(state.update(frame), state, caps,
                                                _read_occ(rc, caps), args.interval),
                            refresh=True)
    except KeyboardInterrupt:
        pass
    finally:
        collector.stop()
        if rc:
            rc.cleanup()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    topo = topology.detect()
    _apply_affinity(args, topo)
    if args.system:
        return run_system(args, topo)
    return run_proc_dump(args, topo) if args.dump > 0 else run_proc_live(args, topo)


if __name__ == "__main__":
    raise SystemExit(main())
