"""Rendering for the per-process (top-style) cache view."""

from __future__ import annotations

from dataclasses import dataclass

from .caps import Caps
from .procmon import ProcMetric, ProcessMonitor, ProcSnapshot, sort_value
from .ui import fmt_bytes, fmt_pct, fmt_rate, sparkline


@dataclass
class UIState:
    """Interactive view state owned by the live render loop."""
    sort_key: str = "l3_mpki"
    view: str = "list"            # "list" | "detail"
    selected_pid: int | None = None
    frozen: bool = False

# MPKI colouring thresholds per level (green < lo, yellow < hi, else red).
_MPKI_BANDS = {
    "L1D": (10.0, 40.0), "L1I": (5.0, 20.0),
    "L2": (5.0, 20.0), "L3": (1.0, 5.0),
}


def _mpki_color(level: str, v: float | None) -> str:
    if v is None:
        return "white"
    lo, hi = _MPKI_BANDS.get(level, (5.0, 20.0))
    return "green" if v < lo else ("yellow" if v < hi else "red")


def fmt_mpki(v: float | None) -> str:
    if v is None:
        return "   ·"
    if v >= 1000:
        return f"{v/1000:5.1f}k"
    return f"{v:6.1f}"


def fmt_kb(kb: int) -> str:
    return fmt_bytes(kb * 1024)


def _short(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _mpki(pm: ProcMetric, level: str) -> float | None:
    lm = pm.levels.get(level)
    return lm.mpki if lm else None


def _iso_color(frac: float | None) -> str:
    """Colour the isolated-core share: red = mostly on isolated cores."""
    if frac is None:
        return "white"
    pct = 100.0 * frac
    return "red" if pct >= 50 else ("yellow" if pct >= 1 else "green")


def fmt_iso(frac: float | None) -> str:
    return "·" if frac is None else f"{100*frac:.0f}"


# ----------------------------------------------------------------- headless ---

def render_proc_text(snap: ProcSnapshot, mon: ProcessMonitor,
                     caps: Caps | None = None) -> str:
    show_iso = bool(caps and caps.isolated_cpus)
    iso_h = f" {'ISO%':>4}" if show_iso else ""
    lines = [
        f"t={snap.t:7.2f}s  monitored={snap.n_monitored}  "
        f"sort={mon.sort_key}  interval={snap.interval_ms}ms"
        + (f"  isolated={_fmt_isolated(caps)}" if show_iso else ""),
        f"{'PID':>7} {'COMMAND':<20} {'CPU%':>6} {'IPC':>5} {'P%':>4}{iso_h} "
        f"{'L1dMPKI':>8} {'L2MPKI':>7} {'L3MPKI':>7} "
        f"{'L1dmiss%':>8} {'L2miss%':>8} {'L3miss%':>8} "
        f"{'L1dEv/s':>9} {'L2Ev/s':>9} {'RSS':>9}",
    ]
    for pm in snap.procs[:mon.top_n]:
        l1d, l2, l3 = (pm.levels.get("L1D"), pm.levels.get("L2"),
                       pm.levels.get("L3"))
        iso_c = f" {fmt_iso(pm.iso_frac):>4}" if show_iso else ""
        lines.append(
            f"{pm.pid:>7} {_short(pm.comm, 20):<20} {pm.cpu_pct:6.1f} "
            f"{(pm.ipc or 0):5.2f} "
            f"{(100*pm.pcore_frac if pm.pcore_frac is not None else 0):4.0f}{iso_c} "
            f"{fmt_mpki(_mpki(pm,'L1D')):>8} {fmt_mpki(_mpki(pm,'L2')):>7} "
            f"{fmt_mpki(_mpki(pm,'L3')):>7} "
            f"{_misspct_txt(l1d):>8} {_misspct_txt(l2):>8} "
            f"{_misspct_txt(l3):>8} "
            f"{fmt_rate(l1d.evict_rate if l1d else None):>9} "
            f"{fmt_rate(l2.evict_rate if l2 else None):>9} {fmt_kb(pm.rss_kb):>9}"
        )
    return "\n".join(lines)


def _misspct_txt(lm) -> str:
    """Plain miss% string for headless output ('·' when n/a)."""
    return "·" if (lm is None or lm.miss_pct is None) else fmt_pct(lm.miss_pct)


def _fmt_isolated(caps: Caps | None) -> str:
    from .topology import fmt_cpus
    return fmt_cpus(caps.isolated_cpus) if caps else "(none)"


# --------------------------------------------------------------------- rich ---

def sorted_procs(snap: ProcSnapshot, key: str) -> list[ProcMetric]:
    """Re-sort a snapshot's processes by `key` (so interactive sort is instant)."""
    return sorted(snap.procs, key=lambda pm: sort_value(pm, key), reverse=True)


def find_proc(procs: list[ProcMetric], pid: int | None) -> ProcMetric | None:
    if pid is None:
        return None
    for pm in procs:
        if pm.pid == pid:
            return pm
    return None


def build_proc_renderable(snap: ProcSnapshot, mon: ProcessMonitor, caps: Caps,
                          state: UIState | None = None):
    """Top-level renderable; dispatches to the list or drill-in detail view."""
    state = state or UIState(sort_key=mon.sort_key)
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    procs = sorted_procs(snap, state.sort_key)[: mon.top_n]
    frozen = " [FROZEN]" if state.frozen else ""
    header = Text.assemble(
        (f"{caps.hostname}", "bold cyan"),
        (f"  {caps.cpu_model}\n", "white"),
        (f"per-process cache top   monitored={snap.n_monitored} procs   "
         f"sort={state.sort_key}", "dim"),
        (frozen, "bold yellow"),
        (f"   interval={snap.interval_ms}ms   t={snap.t:.0f}s\n", "dim"),
        ("MPKI = cache misses per 1000 instructions (lower is better); "
         "P% = cycles on P-cores", "dim italic"),
        ((f"; ISO% = cycles on isolated cores {_fmt_isolated(caps)}"
          if caps.isolated_cpus else ""), "dim italic"),
    )
    parts = [Panel(header, border_style="cyan")]

    if state.view == "detail":
        target = find_proc(procs, state.selected_pid) or (procs[0] if procs else None)
        if target is not None:
            parts.append(_detail_panel(target, mon, full=True, caps=caps))
        else:
            parts.append(Text("  (process gone)", style="yellow"))
        parts.append(_footer(detail=True))
    else:
        parts.append(Panel(_proc_table(procs, mon, state, caps),
                           title="processes", title_align="left",
                           border_style="blue"))
        sel = find_proc(procs, state.selected_pid) or (procs[0] if procs else None)
        if sel is not None:
            parts.append(_detail_panel(sel, mon, full=False, caps=caps))
        parts.append(_footer(detail=False))
    return Group(*parts)


def _proc_table(procs, mon, state: UIState, caps: Caps | None = None):
    from rich.table import Table
    from rich.text import Text

    show_iso = bool(caps and caps.isolated_cpus)
    table = Table(expand=True, pad_edge=False, header_style="bold")
    table.add_column("PID", justify="right", style="bright_black", no_wrap=True)
    table.add_column("COMMAND", no_wrap=True, ratio=2)
    table.add_column("CPU%", justify="right")
    table.add_column("IPC", justify="right")
    table.add_column("P%", justify="right")
    if show_iso:
        table.add_column("ISO%", justify="right")
    table.add_column("L1d", justify="right")
    table.add_column("L1i", justify="right")
    table.add_column("L2", justify="right")
    table.add_column("L3", justify="right")
    table.add_column("L1dmiss%", justify="right")
    table.add_column("L2miss%", justify="right")
    table.add_column("L3miss%", justify="right")
    table.add_column("L1devict/s", justify="right")
    table.add_column("L2evict/s", justify="right")
    table.add_column("RSS", justify="right")
    table.add_column("trend", ratio=1, no_wrap=True)

    for pm in procs:
        l1d, l2, l3 = (pm.levels.get("L1D"), pm.levels.get("L2"),
                       pm.levels.get("L3"))
        scolor = _mpki_color("L3", _mpki(pm, "L3"))
        row_style = "reverse" if pm.pid == state.selected_pid else None
        cells = [
            str(pm.pid),
            _short(pm.cmd or pm.comm, 36),
            f"{pm.cpu_pct:5.1f}",
            f"{pm.ipc:.2f}" if pm.ipc is not None else "·",
            f"{100*pm.pcore_frac:.0f}" if pm.pcore_frac is not None else "·",
        ]
        if show_iso:
            cells.append(Text(fmt_iso(pm.iso_frac), style=_iso_color(pm.iso_frac)))
        table.add_row(
            *cells,
            Text(fmt_mpki(_mpki(pm, "L1D")), style=_mpki_color("L1D", _mpki(pm, "L1D"))),
            Text(fmt_mpki(_mpki(pm, "L1I")), style=_mpki_color("L1I", _mpki(pm, "L1I"))),
            Text(fmt_mpki(_mpki(pm, "L2")), style=_mpki_color("L2", _mpki(pm, "L2"))),
            Text(fmt_mpki(_mpki(pm, "L3")), style=_mpki_color("L3", _mpki(pm, "L3"))),
            _misspct_cell(l1d.miss_pct if l1d else None),
            _misspct_cell(l2.miss_pct if l2 else None),
            _misspct_cell(l3.miss_pct if l3 else None),
            fmt_rate(l1d.evict_rate if l1d else None),
            fmt_rate(l2.evict_rate if l2 else None),
            fmt_kb(pm.rss_kb),
            Text(sparkline(mon.hist(pm.pid)), style=scolor),
            style=row_style,
        )
    return table


def _footer(detail: bool):
    from rich.text import Text
    if detail:
        keys = "←/h/Esc back   s/S sort   f freeze   q quit"
    else:
        keys = "↑↓/jk select   ⏎/→ drill in   s/S sort   f freeze   q quit"
    return Text(f"  {keys}    colours: green=low yellow=warm red=hot   "
                "·=counter n/a on that PMU", style="dim")


def _detail_panel(pm: ProcMetric, mon: ProcessMonitor, full: bool,
                  caps: Caps | None = None):
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    t = Table(expand=True, show_edge=False, header_style="bold")
    t.add_column("level"); t.add_column("access/s", justify="right")
    t.add_column("miss/s", justify="right"); t.add_column("miss%", justify="right")
    t.add_column("MPKI", justify="right"); t.add_column("evict/s", justify="right")
    names = {"L1D": "L1 data", "L1I": "L1 instr", "L2": "L2", "L3": "L3/LLC"}
    # When isolation is configured, break each level's misses down by the core
    # class the work ran on, so you can see *where* the bad cache behaviour is.
    split = bool(caps and caps.isolated_cpus) and full

    def _level_row(label: str, lm, level: str, style: str | None = None) -> None:
        t.add_row(
            label, fmt_rate(lm.access_rate), fmt_rate(lm.miss_rate),
            Text(fmt_pct(lm.miss_pct), style=_miss_color(lm.miss_pct)),
            Text(fmt_mpki(lm.mpki), style=_mpki_color(level, lm.mpki)),
            fmt_rate(lm.evict_rate), style=style,
        )

    for level in ["L1D", "L1I", "L2", "L3"]:
        lm = pm.levels.get(level)
        if lm is None:
            continue
        _level_row(names[level], lm, level)
        if split:
            li = pm.levels_iso.get(level)
            ls = pm.levels_shr.get(level)
            if li is not None:
                _level_row("  · on isolated", li, level, style="bright_black")
            if ls is not None:
                _level_row("  · on shared", ls, level, style="bright_black")
    ipc = f"{pm.ipc:.2f}" if pm.ipc is not None else "·"
    pcore = f"{100*pm.pcore_frac:.0f}%" if pm.pcore_frac is not None else "·"
    iso = (f"   isolated-core={fmt_iso(pm.iso_frac)}%"
           if (caps and caps.isolated_cpus) else "")
    title = f"detail · pid {pm.pid} · {_short(pm.cmd or pm.comm, 60)}"
    body = [t]
    if full:
        summary = Text.assemble(
            (f"  threads={pm.nthreads}   RSS={fmt_kb(pm.rss_kb)}   "
             f"CPU={pm.cpu_pct:.1f}%   IPC={ipc}   P-core={pcore}{iso}   "
             f"insn/s={fmt_rate(pm.ins_rate)}\n", "white"),
            (f"  sort-metric trend  {sparkline(mon.hist(pm.pid), width=48)}",
             "magenta"),
        )
        body = [summary, t]
    return Panel(Group(*body), title=title, title_align="left",
                 border_style="magenta")


def _miss_color(v: float | None) -> str:
    if v is None:
        return "white"
    return "green" if v < 5 else ("yellow" if v < 20 else "red")


def _misspct_cell(v: float | None):
    """A miss% table cell, coloured by pressure (n/a -> '·')."""
    from rich.text import Text
    return Text("·" if v is None else fmt_pct(v), style=_miss_color(v))
