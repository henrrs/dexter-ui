#!/usr/bin/env bash
set -euo pipefail
run_dir=".run"
mkdir -p "$run_dir"
PYTHONPATH="backend/src${PYTHONPATH:+:$PYTHONPATH}" .venv/bin/python -m dexter >"$run_dir/backend.log" 2>&1 &
backend_pid=$!
./node_modules/.bin/vite --host 127.0.0.1 --strictPort >"$run_dir/frontend.log" 2>&1 &
frontend_pid=$!
printf '%s\n' "$backend_pid" >"$run_dir/backend.pid"
printf '%s\n' "$frontend_pid" >"$run_dir/frontend.pid"
cleanup() {
  kill "$frontend_pid" "$backend_pid" 2>/dev/null || true
  wait "$frontend_pid" "$backend_pid" 2>/dev/null || true
  rm -f "$run_dir/backend.pid" "$run_dir/frontend.pid"
}
trap cleanup EXIT INT TERM
sleep 1
echo "Dexter UI:  http://127.0.0.1:5173"
echo "Dexter API: http://127.0.0.1:8000/api/health"
echo "Logs:       $run_dir/frontend.log and $run_dir/backend.log"
wait -n "$backend_pid" "$frontend_pid"
