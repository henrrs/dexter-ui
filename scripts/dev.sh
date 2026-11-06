#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"
run_dir=".run"
mkdir -p "$run_dir"

if [[ -n "${DEXTER_PORT:-}" ]]; then
  api_port="$(python3 scripts/select_api_port.py "$DEXTER_PORT")"
else
  api_port="$(python3 scripts/select_api_port.py)"
  if [[ "$api_port" != "8000" ]]; then
    echo "Port 8000 is occupied; using API port ${api_port}."
  fi
fi

DEXTER_PORT="$api_port" PYTHONPATH="backend/src${PYTHONPATH:+:$PYTHONPATH}" .venv/bin/python -m dexter >"$run_dir/backend.log" 2>&1 &
backend_pid=$!
DEXTER_PORT="$api_port" ./node_modules/.bin/vite --host 127.0.0.1 --strictPort >"$run_dir/frontend.log" 2>&1 &
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
echo "Dexter API: http://127.0.0.1:${api_port}/api/health"
echo "Logs:       $run_dir/frontend.log and $run_dir/backend.log"
set +e
wait -n "$backend_pid" "$frontend_pid"
status=$?
set -e
if [[ "$status" -ne 0 ]]; then
  echo "A Dexter dev process exited with status $status. Recent logs:" >&2
  echo "--- backend ---" >&2
  tail -n 40 "$run_dir/backend.log" >&2 || true
  echo "--- frontend ---" >&2
  tail -n 40 "$run_dir/frontend.log" >&2 || true
fi
exit "$status"
