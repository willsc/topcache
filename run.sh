#!/usr/bin/env bash
# Convenience launcher: ensures counters are readable, then starts cachetop.
# Passes all arguments straight through, e.g.  ./run.sh --interval 250 --pid 1234
set -euo pipefail
cd "$(dirname "$0")"

# Hardware counters need either root or a relaxed paranoid level.
if [[ $EUID -ne 0 ]]; then
  paranoid=$(cat /proc/sys/kernel/perf_event_paranoid 2>/dev/null || echo 4)
  if [[ "$paranoid" -gt 0 ]]; then
    echo "perf_event_paranoid=$paranoid; re-running under sudo for counter access." >&2
    exec sudo "$0" "$@"
  fi
fi

exec python3 -m cachetop "$@"
