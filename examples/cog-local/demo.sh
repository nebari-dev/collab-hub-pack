#!/usr/bin/env bash
# Launch a Cog from the `collab-hub` CLI on the local dev hub, end to end, and check each step.
#
#   examples/cog-local/demo.sh            # API on :8000
#   API_PORT=8010 examples/cog-local/demo.sh
#
# It starts the hub's API and its run controller (dev level 1: no container, no
# sign-in), launches the `hello` Cog twice through the CLI — once to completion,
# once to terminate it mid-run — and stops both processes on the way out.
# README.md walks through the same steps by hand.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
PORT=${API_PORT:-8000}
LOGS=$ROOT/dev/.local

for tool in uv pixi; do
  command -v "$tool" >/dev/null || { echo "this example needs $tool on PATH"; exit 1; }
done

# The CLI keeps its profile in a directory of its own here, so the example leaves yours alone.
export COLLAB_HUB_CONFIG_DIR=$(mktemp -d)
export COLLAB_HUB_URL=http://localhost:$PORT
hub() { uv run --quiet --project "$ROOT/cli" collab-hub "$@"; }
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

mkdir -p "$LOGS"
# Job control gives each of the two its own process group, so stopping one stops `make`, `uv`
# and the Python process under it together, whichever of them forwards a signal and whichever does not.
set -m
make -s -C "$ROOT/dev" api API_PORT="$PORT" > "$LOGS/example-api.log" 2>&1 &
api=$!
make -s -C "$ROOT/dev" controller > "$LOGS/example-controller.log" 2>&1 &
controller=$!
set +m
stop() {
  kill -TERM -- "-$api" "-$controller" 2>/dev/null || true
  wait "$api" "$controller" 2>/dev/null || true
  rm -rf "$COLLAB_HUB_CONFIG_DIR"
}
trap stop EXIT

say "Waiting for the hub on $COLLAB_HUB_URL"
for _ in $(seq 1 120); do
  curl -sf "$COLLAB_HUB_URL/health" >/dev/null && break
  kill -0 "$api" 2>/dev/null || { echo "the API stopped:"; tail -5 "$LOGS/example-api.log"; exit 1; }
  sleep 0.5
done
curl -sf "$COLLAB_HUB_URL/health" >/dev/null || { echo "the API never answered"; tail -5 "$LOGS/example-api.log"; exit 1; }

say "collab-hub whoami"
hub whoami

say "collab-hub cog launch hello --input '{\"name\": \"Ada\"}' --watch"
hub cog launch hello --input '{"name": "Ada"}' --watch | tee "$LOGS/example-launch.out"
grep -q '"greeting": "Hello, Ada!"' "$LOGS/example-launch.out" \
  || { echo "the Cog's answer is not in the run"; tail -20 "$LOGS/example-controller.log"; exit 1; }

say "collab-hub cog launch hello --input '{\"name\": \"Ada\", \"seconds\": 120}'   (a run to terminate)"
run=$(hub cog launch hello --input '{"name": "Ada", "seconds": 120}')
echo "$run"
for _ in $(seq 1 120); do
  hub run show "$run" --json | grep -q '"state": "running"' && break
  sleep 0.5
done

say "collab-hub run list"
hub run list | tee "$LOGS/example-list.out"
grep -q "^$run .* RUNNING" "$LOGS/example-list.out" || { echo "$run is not listed as running"; exit 1; }

say "collab-hub run terminate $run"
hub run terminate "$run"

say "collab-hub run list"
hub run list | tee "$LOGS/example-list.out"
grep -q "^$run .* CANCELLED" "$LOGS/example-list.out" || { echo "$run did not end cancelled"; exit 1; }
if pgrep -f 'examples/cog-local/cogs/hello/\.pixi/envs' >/dev/null; then
  echo "a hello worker is still running after its run was terminated"; exit 1
fi

trap - EXIT
stop
if curl -sf -m 2 "$COLLAB_HUB_URL/health" >/dev/null; then
  echo "the API is still answering after it was stopped"; exit 1
fi

say "Done: one run completed, one terminated, and nothing left running."
