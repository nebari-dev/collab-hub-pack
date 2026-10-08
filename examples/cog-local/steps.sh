# What demo.sh and toad.sh share: where things are, and how a step is shown.
# Sourced, not run. Each script sets STEPS before its first banner.

LOCAL=.local
PORT=${PORT:-8000}
CLAUDE_MODEL=${CLAUDE_MODEL:-claude-opus-5-5}
step=0
mkdir -p "$LOCAL"

if [ -t 1 ]; then BOLD=$'\033[1m' DIM=$'\033[2m' CYAN=$'\033[36m' GREEN=$'\033[32m' YELLOW=$'\033[33m' RED=$'\033[31m' OFF=$'\033[0m'
else BOLD='' DIM='' CYAN='' GREEN='' YELLOW='' RED='' OFF=''; fi

# A banner for each step: what it is, and why.
banner() {
  step=$((step + 1))
  local title=$1; shift
  printf '\n\n%s%s━━━ Step %d of %d · %s %s%s\n' "$BOLD" "$CYAN" "$step" "$STEPS" "$title" \
    "$(printf '━%.0s' $(seq 1 $((60 - ${#title}))))" "$OFF"
  for line in "$@"; do printf '%s  %s%s\n' "$DIM" "$line" "$OFF"; done
  echo
}
# The command a reader would type, then the step's own make target, quietly.
run() {
  local shown=$1; shift
  printf '  %s$ %s%s\n\n' "$BOLD" "$shown" "$OFF"
  # Targets indent their own notes by two; everything is shown at the same indent.
  make -s --no-print-directory PORT="$PORT" "$@" 2>&1 | sed -e 's/^  //' -e 's/^/  /'
}
ok() { printf '\n  %s✔ %s%s\n' "$GREEN" "$1" "$OFF"; }
skip() { printf '  %s↷ %s%s\n' "$YELLOW" "$1" "$OFF"; }
fail() { printf '\n  %s✘ %s%s\n' "$RED" "$1" "$OFF"; exit 1; }
cli() { uv run --quiet --project ../../cli collab-hub "$@"; }
# Until the controller has picked the launched run up and Hermes is starting.
wait_running() {
  for _ in $(seq 1 120); do
    cli run show "$(cat $LOCAL/run)" --json | grep -q '"status": "RUNNING"' && return 0
    sleep 0.5
  done
  fail "the controller did not pick the run up"
}
stop_all() { make -s --no-print-directory shutdown > /dev/null 2>&1 || true; }
export COLLAB_HUB_URL=http://127.0.0.1:$PORT COLLAB_HUB_CONFIG_DIR=$PWD/$LOCAL/cli

