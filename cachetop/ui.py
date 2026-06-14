"""Rendering: a live rich dashboard and a plain-text headless renderer."""

from __future__ import annotations

from .caps import Caps
from .metrics import MetricsState, Snapshot
from .resctrl import Occupancy

_SPARK = "▁▂▃▄▅▆▇█"


def sparkline(values, width: int = 32) -> str:
    vals = list(values)[-width:]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    span = hi - lo
    if span <= 0:
        return _SPARK[0] * len(vals)
    out = []
    for v in vals:
        idx = int((v - lo) / span * (len(_SPARK) - 1) + 0.5)
        out.append(_SPARK[idx])
    return "".join(out)


def fmt_rate(v: float | None) -> str:
    if v is None:
        return "  n/a"
    for unit, scale in (("G", 1e9), ("M", 1e6), ("K", 1e3)):
        if v >= scale:
            return f"{v / scale:6.2f}{unit}"
    return f"{v:7.0f}"


def fmt_pct(v: float | None) -> str:
    return "  n/a" if v is None else f"{v:5.1f}%"


def fmt_bytes(v: int) -> str:
    f = float(v)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if f < 1024 or unit == "GiB":
            return f"{f:6.1f} {unit}"
        f /= 1024
    return f"{f:.1f} GiB"


# ---------------------------------------------------------------- headless ---

def render_text(snap: Snapshot, state: MetricsState, caps: Caps,
                occ: Occupancy | None) -> str:
    lines = [f"t={snap.t:8.3f}s  " + "  ".join(
        f"IPC[{p.replace('cpu_', '')}]={snap.ipc.get(p, 0):.2f}" for p in state.pmus)]
    if caps.isolated_cpus:
        from .topology import fmt_cpus
        lines.append(f"  isolated CPUs: {fmt_cpus(caps.isolated_cpus)} "
                     "(counts are system-wide, not split by isolation)")
    for level in state.levels:
        for pmu in state.pmus:
            cell = snap.cells.get((level, pmu))
            if cell is None:
                continue
            lines.append(
                f"  {level:<4} {pmu.replace('cpu_',''):<9} "
                f"acc={fmt_rate(cell.access_rate)}/s  "
                f"miss={fmt_rate(cell.miss_rate)}/s  "
                f"miss%={fmt_pct(cell.miss_pct)}  "
                f"evict={fmt_rate(cell.evict_rate)}/s"
            )
    if occ is not None:
        lines.append(f"  L3 occupancy: {fmt_bytes(occ.total_bytes)} "
                     f"({len(occ.per_domain)} domain(s))")
    return "\n".join(lines)


# ------------------------------------------------------------------- rich ---

def build_renderable(snap: Snapshot, state: MetricsState, caps: Caps,
                     occ: Occupancy | None, interval_ms: int):
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    occ_line = ("L3 occupancy: not available on this CPU "
                "(no resctrl/CMT)" if occ is None
                else f"L3 occupancy: {fmt_bytes(occ.total_bytes)} "
                     f"over {len(occ.per_domain)} domain(s)")
    hybrid = "hybrid " if caps.hybrid else ""
    from .topology import fmt_cpus
    iso_line = (f"isolated CPUs: {fmt_cpus(caps.isolated_cpus)}  "
                f"(counts below are system-wide, not split by isolation)\n"
                if caps.isolated_cpus else "")
    header = Text.assemble(
        (f"{caps.hostname}", "bold cyan"),
        (f"  {caps.cpu_model}\n", "white"),
        (f"vendor={caps.vendor}  {hybrid}pmus={','.join(caps.pmus)}  "
         f"interval={interval_ms}ms  t={snap.t:.1f}s\n", "dim"),
        (iso_line, "magenta"),
        (occ_line, "green" if occ is not None else "yellow"),
    )

    tables = []
    for level in state.levels:
        table = Table(expand=True, pad_edge=False, show_edge=False)
        table.add_column("core-type", style="cyan", no_wrap=True)
        table.add_column("access/s", justify="right")
        table.add_column("miss/s", justify="right")
        table.add_column("miss%", justify="right")
        table.add_column("evict/s", justify="right")
        table.add_column("miss% trend", ratio=1)
        any_row = False
        for pmu in state.pmus:
            cell = snap.cells.get((level, pmu))
            if cell is None:
                continue
            any_row = True
            spark = sparkline(state.miss_hist.get((level, pmu), []))
            color = _miss_color(cell.miss_pct)
            table.add_row(
                pmu.replace("cpu_", ""),
                fmt_rate(cell.access_rate),
                fmt_rate(cell.miss_rate),
                Text(fmt_pct(cell.miss_pct), style=color),
                fmt_rate(cell.evict_rate),
                Text(spark, style=color),
            )
        if any_row:
            title = {"L1D": "L1 Data", "L1I": "L1 Instr",
                     "L2": "L2 Unified", "L3": "L3 / LLC (shared)"}.get(level, level)
            tables.append(Panel(table, title=title, title_align="left",
                                border_style="blue"))

    footer = Text("  q Ctrl-C quit   miss% colored by pressure   "
                  "rates are per-interval deltas", style="dim")
    return Group(Panel(header, border_style="cyan"), *tables, footer)


def _miss_color(miss_pct: float | None) -> str:
    if miss_pct is None:
        return "white"
    if miss_pct < 5:
        return "green"
    if miss_pct < 20:
        return "yellow"
    return "red"
