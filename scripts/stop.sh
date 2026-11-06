#!/usr/bin/env bash
set -euo pipefail
run_dir=".run"
stopped=0
for name in backend frontend; do
  pid_file="$run_dir/$name.pid"
  if [[ -f "$pid_file" ]]; then
    pid="$(cat "$pid_file")"
    command_line=""
    if [[ -r "/proc/$pid/cmdline" ]]; then
      command_line="$(tr '\0' ' ' <"/proc/$pid/cmdline")"
    fi
    is_dexter=0
    if [[ "$name" == "backend" && "$command_line" == *"-m dexter"* ]]; then is_dexter=1; fi
    if [[ "$name" == "frontend" && "$command_line" == *"vite"* ]]; then is_dexter=1; fi
    if [[ "$is_dexter" -eq 1 ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      stopped=1
    fi
    rm -f "$pid_file"
  fi
done
if [[ "$stopped" -eq 1 ]]; then
  echo "Dexter dev processes stopped."
else
  echo "No Dexter dev processes were running."
fi
