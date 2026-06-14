# cachetop

A portable terminal dashboard for **cache activity** on x86 (Intel & AMD), in two
modes:

- **per-process (default)** — a `top`-style live table of the busiest processes
  with their L1/L2/L3 behaviour: MPKI per level, miss %, evictions, IPC, P/E-core
  mix, plus a full per-level detail panel for the hottest process.
- **system-wide (`--system`)** — aggregate cache event rates per level, split by
  core type, with optional L3 occupancy (bytes) where the hardware exposes it.

```
┌─ cwills-NucBox-EVO-T1   Intel(R) Core(TM) Ultra 9 285H ─────────────────────────────┐
│ per-process cache top   monitored=20 procs   sort=l3_mpki   interval=1000ms          │
│ MPKI = cache misses per 1000 instructions (lower is better); P% = cycles on P-cores  │
├──────────────────────────────────────────────────────────────────────────────────────┤
│     PID  COMMAND          CPU%   IPC   P%    L1d   L1i    L2    L3  L3miss%      RSS    │
│ 2548764  claude            0.0  0.69   84   15.9  49.8  60.0   4.7   74.0%  371.8 MiB   │
│ 1871959  clickhouse-serv   4.3  1.03   25    2.6  27.8   0.6   0.6   20.6%  997.7 MiB   │
│ 2671656  python3 stream  100.1  7.19  100    0.1   1.1   0.0   0.0   71.4%   73.3 MiB   │
└──────────────────────────────────────────────────────────────────────────────────────┘
┌─ detail · pid 2548764 · claude  [11 thr, 371.8 MiB, IPC 0.69] ───────────────────────┐
│ level     access/s     miss/s   miss%   MPKI   evict/s                                 │
│ L1 data    434.05K     23.67K    5.5%   15.9    3.25K                                   │
│ L2         139.61K     89.34K   64.0%   60.0    9.19K                                   │
│ L3/LLC       9.55K      7.06K   74.0%    4.7      n/a                                   │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

### Reading the per-process table

- **MPKI (misses per 1000 instructions)** is the headline cross-process metric.
  Raw miss *rates* just favour whichever process runs more; MPKI normalises by
  work done, so it actually compares cache *behaviour*. Columns `L1d/L1i/L2/L3`
  are MPKI, colour-coded green→yellow→red by pressure.
- **L1dmiss% / L2miss% / L3miss%** are the share of lookups that missed at each
  level (for L3, lookups that went to DRAM). L1i has no software-visible *access*
  counter, so it has no miss% column (its MPKI still shows). High **L3miss%**
  **with** high L3 MPKI = a real memory-bound thrasher. High L3miss% but ~0 MPKI
  (common for interpreters) just means few LLC accesses overall, each expensive —
  different story, both worth seeing.
- **L1devict/s** and **L2evict/s** are line-eviction (writeback/replacement)
  rates — a high miss% **with** a high eviction rate is the classic
  cache-thrashing signature. There is no L3 column: the PMU exposes no
  software-visible LLC eviction event (only `l1d.replacement` and
  `l2_lines_out.non_silent`), so L3 eviction is unavailable on all parts.
- **P%** is the share of the process's cycles that ran on P-cores (`cpu_core`)
  vs E-cores — useful on hybrid parts.
- **ISO%** is the share of the process's cycles that ran on **isolated** cores
  (`isolcpus=`/`nohz_full=`). It answers the core question this tool exists for:
  *is this cache-hungry process living on an isolated trading core?* The column
  appears only when the kernel has isolated cores configured, is coloured
  green→yellow→red as that share rises, and is computed by weighting each
  thread's cycles by the CPU it last ran on (exact for pinned threads,
  approximate for ones that migrate). System-wide mode shows the isolated set in
  its header but does not split the aggregate rates by isolation.
- `·` / `n/a` means that counter isn't implemented on the PMU the process ran on
  (e.g. L1D-misses don't exist on the E-cores), not that it was zero.

## The honest mental model: events, not state

x86 hardware does not expose cache *contents* or a live "fullness gauge" to
software. After a few milliseconds every cache way holds *something*, so "is the
cache full?" is trivially yes. The meaningful question is whether it holds
*useful* data, which you infer from **hit/miss and eviction rates over time**.
So this tool plots counter deltas, not a fill bar.

**The one real exception is L3.** Intel RDT/CMT and AMD QoS monitoring (both
surfaced through Linux `resctrl`) report actual per-task L3 occupancy in bytes.
`cachetop` uses it automatically when present. L1/L2 have no occupancy readout
anywhere — there you only get pressure proxies (miss rate, refill/eviction rate).

## What you get on which hardware

| Signal | Intel (RDT parts / EPYC-class) | AMD EPYC / many Ryzen | This box (Core Ultra 9 285H) |
|---|---|---|---|
| L1/L2/L3 hit & miss rates | ✓ | ✓ | ✓ |
| L1/L2 eviction (writeback) rate | ✓ (`l1d.replacement`, `l2_lines_out.non_silent`) | best-effort¹ | ✓ |
| **L3 occupancy (bytes)** | ✓ if CMT exposed | ✓ | ✗ — no `resctrl`/CMT on this chip |
| Per-core-type breakdown | ✓ (hybrid) | n/a | ✓ (P / E / LP-E cores) |

¹ AMD L2/eviction event names vary by Zen generation (consult the PPR for your
part); `cachetop` probes them at startup and silently omits any that don't count.

This is a **client Arrow Lake** part: it does **not** implement RDT/CMT, so the
L3-occupancy gauge is unavailable here. Everything else works. For the occupancy
bar you need an EPYC, many Ryzen parts, or a server Xeon with CMT.

## How it works

**Per-process mode** (the default):

- A `/proc` scan ranks processes by CPU (the same proxy `top` uses) to pick the
  monitored set (`--top N`, default 20). `--pids a,b,c` pins an exact set.
- `perf stat --per-thread -p <set> -x, -I <interval>` streams accurate
  per-*thread* counters for every probed event; threads are aggregated back to
  their process via `/proc/<tid>/status` `Tgid`.
- The monitored set is re-selected every `--refresh` seconds (default 4) so newly
  hot processes get picked up; process metadata (CPU%, RSS) refreshes each frame.
- Sort with `--sort` (`l3_mpki` default; also `l2_mpki`, `l1d_mpki`, `l3_miss`,
  `l2_miss`, `cpu`, `ipc`).
- *Scope note:* this watches the top-N busiest processes, not literally every
  process. A low-CPU process quietly thrashing cache can be missed — raise
  `--top`, or `--pids` it explicitly. (`ulimit -n` here is 1M, so a large `--top`
  is fine.)

**System-wide mode** (`--system`):

- One long-lived `perf stat -a -x, -I <interval>` process is parsed live. Each
  interval line is already a per-interval delta keyed by `pmu/event/`.
- L3 occupancy (when available) is read from `resctrl` `llc_occupancy` files,
  summed across L3 domains; with `-p PID` it creates a monitoring group for just
  that process. resctrl is **never mounted implicitly** — an already-mounted
  resctrl is used as-is, otherwise pass `--mount-resctrl` to have cachetop mount
  it (and unmount it again on exit). See *Running on latency-sensitive hosts*.

**Both modes:**

- Events are **probed at startup** — only counters the kernel reports as
  supported *and* counting are kept, so the same binary adapts across Intel/AMD
  and across hybrid PMUs without per-model config.
- On hybrid Intel parts, symbolic events expand to `cpu_core` / `cpu_atom` /
  `cpu_lowpower`; per-process metrics are summed across the core types the
  process ran on.

For deeper analysis the standard tools remain excellent and complementary:
`perf c2c` (false-sharing / cache-line contention), `likwid-perfctr` (predefined
CACHE/L2/L3 groups + timeline), and the vendor profilers (AMD uProf cache
analysis via IBS; Intel VTune via PEBS).

## Requirements

- Linux with the `perf` tool (`linux-tools-$(uname -r)` / `perf`).
- Python 3.10+. `rich` for the live UI (`pip install rich`); headless `--dump`
  mode needs only the stdlib.
- Permission to read hardware counters: run as root, **or** lower
  `kernel.perf_event_paranoid` (`sudo sysctl kernel.perf_event_paranoid=-1`).
  Reading L3 occupancy needs `resctrl` mounted; `--mount-resctrl` mounts it for
  you (root).

## Running on latency-sensitive hosts

cachetop is built to be pointed *at* isolated trading cores — that's the job:
seeing whether workloads on isolated, shared and housekeeping cores are abusing
the caches. Two things make it safe to run on a live box:

- **It keeps itself off the isolated cores.** On startup it reads the isolated
  set (`isolcpus=` / `nohz_full=`, via `/proc/cmdline` and
  `/sys/devices/system/cpu/{isolated,nohz_full}`) and pins its own threads and
  the `perf` children it spawns to the **housekeeping** cores. The counter-read
  IPIs that actually measure an isolated core are inherent to monitoring it and
  still happen (that's the data you want); what's kept off the hot path is the
  *avoidable* noise — the Python interpreter, the perf CSV parsing, the `/proc`
  scan and the render loop. The IPI rate is set by `--interval`: a larger
  interval = fewer reads into the isolated cores. Pass `--no-pin` to opt out
  (e.g. if you pin externally via `taskset`/cgroups).
- **It does not change system state implicitly.** resctrl is mounted only with
  `--mount-resctrl` (and unmounted on exit if cachetop mounted it), so an
  existing CAT / cache-partitioning setup is left untouched unless you ask.

## Usage

```bash
# per-process cache top (default), 1s interval
sudo ./run.sh

# watch the top 30, sorted by L3 miss *rate* (memory traffic) not MPKI
sudo ./run.sh --top 30 --sort l3_miss

# pin specific processes (e.g. your bot and its children)
sudo ./run.sh --pids $(pgrep -d, -f my_trading_bot)

# faster sampling
sudo ./run.sh --interval 250

# headless: print 5 per-process frames as text then exit (SSH / logging / CI)
sudo python3 -m cachetop --dump 5 --interval 1000

# aggregate system-wide view instead, with L3 occupancy where supported
sudo ./run.sh --system
# add L3 occupancy by mounting resctrl (unmounted again on exit)
sudo python3 -m cachetop --system --mount-resctrl --pid $(pgrep -f my_trading_bot)
```

`run.sh` re-execs under sudo if counters need it.

### Interactive keys (per-process live UI)

| Key | Action |
|---|---|
| `↑`/`↓` or `k`/`j` | move the row selection |
| `Enter` / `→` / `l` | drill into the selected process (full per-level detail) |
| `Esc` / `←` / `h` | back to the list |
| `s` / `S` | cycle the sort metric forward / backward |
| `f` | freeze / unfreeze the display (selection still navigable) |
| `q` | quit (`Ctrl-C` also works) |

Sorting and selection re-apply instantly (the UI re-sorts the current frame
rather than waiting for the next perf interval), so the display stays responsive
even at a 1s sample interval. The selected process is highlighted and its full
breakdown is shown in the detail panel beneath the table.

### Reading the numbers

- **MPKI** (per-process `L1d/L1i/L2/L3` columns) and **miss%** are colored by
  pressure: green = low, yellow = warm, red = hot.
- A high L2/L3 **miss%** with a high **eviction rate** is the classic
  cache-thrashing signature — the working set doesn't fit at that level.
- **IPC** is context: cache stalls usually show up as falling IPC.
- `·` / `n/a` means that counter isn't implemented on the PMU the process ran on
  (common for L1D-misses on E-cores), not that it's zero.

## Layout

```
cachetop/
  caps.py        # detect vendor, PMUs, resctrl capability
  topology.py    # isolated/housekeeping core detection + self-affinity pinning
  events.py      # event catalog (vendor-tagged) + startup probing
  procscan.py    # /proc scan: per-process CPU%, RSS, threads, tid->pid
  procmon.py     # per-thread perf collector + top-style process aggregation
  procui.py      # per-process (top) rich table + detail panel + UIState
  keyboard.py    # non-blocking cbreak keypress reader for the live UI
  collector.py   # system-wide `perf stat -I` stream & parse
  metrics.py     # system-wide per-level/per-PMU rates + sparkline history
  resctrl.py     # L3 occupancy (Intel RDT/CMT, AMD QoS)
  ui.py          # system-wide dashboard + shared formatting helpers
  __main__.py    # CLI (per-process default, --system for aggregate)
```
