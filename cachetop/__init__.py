"""cachetop - a portable terminal cache-activity dashboard.

Measures cache *events* over time (hit/miss/eviction rates per level) via
perf_event_open (through the `perf` binary) and, where the hardware exposes it,
per-task L3 occupancy via the kernel resctrl interface.

The honest scope: x86 does not let software read live cache contents or a
"fullness" gauge for L1/L2. We therefore plot time-series of counter deltas.
The one real occupancy signal (L3 bytes per task) only exists on CPUs with
Intel RDT/CMT or AMD QoS monitoring exposed through resctrl; this tool uses it
automatically when present and falls back to rate-only metrics otherwise.
"""

__version__ = "0.1.0"
