#!/usr/bin/env bash
# `make demo`: every step of README.md in order, each explained, then checked.
#
# Hermes talks to the fake model for the main walk-through, so it costs nothing
# and needs no account; CI runs it. When ANTHROPIC_API_KEY is set, one more step
# relaunches Hermes on Claude and chats with it for real.
set -euo pipefail
cd "$(dirname "$0")"

LOCAL=.local
PORT=${PORT:-8000}
CLAUDE_MODEL=${CLAUDE_MODEL:-claude-opus-5-5}
STEPS=13
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
export COLLAB_HUB_URL=http://127.0.0.1:$PORT COLLAB_HUB_CONFIG_DIR=$PWD/$LOCAL/cli

stop_all() { make -s --no-print-directory shutdown > /dev/null 2>&1 || true; }
trap 'status=$?; if [ $status -ne 0 ]; then stop_all; printf "\n  %sThe demo stopped at step %d. Logs: %s/*.log%s\n" "$RED" "$step" "$LOCAL" "$OFF"; fi' EXIT

printf '%sA Hermes agent'"'"'s whole life as a Cog, on a hub running on this machine.%s\n' "$BOLD" "$OFF"
printf '%sEvery step below is a make target in this directory; README.md explains each one.%s\n' "$DIM" "$OFF"

banner "Check the tools" \
  "Docker for Postgres and Keycloak, uv for the Python environments, pixi for the Cog's own" \
  "environment, Toad for chatting from a terminal. Hermes's environment is installed by make env."
run "make tools" MODEL_SOURCE=fake tools

banner "Start Postgres and Keycloak" \
  "Two containers: the hub's database, and the identity provider you sign in with." \
  "The realm 'nebari' comes with its users. Docker's output goes to $LOCAL/demo-hub.log."
printf '  %s$ make hub%s\n' "$BOLD" "$OFF"
make -s --no-print-directory hub > "$LOCAL/demo-hub.log" 2>&1 || { tail -20 "$LOCAL/demo-hub.log"; fail "the containers did not start"; }
ok "Postgres and Keycloak are up"

banner "Who signs in" \
  "The user this demo signs in as, and a check that Keycloak issues them a token."
run "make credentials" MODEL_SOURCE=fake credentials

banner "Start the hub" \
  "Three processes in the background: a fake model Hermes will talk to (it answers" \
  "'The fake model heard: ...', with no account), the hub's API, which records what you ask," \
  "and the run controller, which starts Cogs. Their logs go to $LOCAL/."
run "make start" MODEL_SOURCE=fake start

banner "Sign in with the CLI" \
  "collab-hub signs in to the hub through Keycloak. In a terminal of your own, make login" \
  "opens your browser; with no browser, the demo asks Keycloak for a token instead."
run "make login-token" MODEL_SOURCE=fake login-token
echo
run "collab-hub whoami" MODEL_SOURCE=fake whoami

banner "The Cogs this hub can launch" \
  "Each is a package with its own environment. hermes is the one this demo runs."
run "collab-hub cog list --launchable" MODEL_SOURCE=fake cogs

banner "Launch Hermes" \
  "The API records the launch; the controller starts the Hermes Cog as a process of its own," \
  "which starts Hermes and opens a session with it. The run's id is kept in $LOCAL/run."
run "collab-hub cog launch hermes --entry session" MODEL_SOURCE=fake launch
wait_running

banner "See it running" \
  "The runs you launched, newest first (the three latest here)."
printf '  %s$ collab-hub run list%s\n\n' "$BOLD" "$OFF"
cli run list > "$LOCAL/runs.out"
head -4 "$LOCAL/runs.out" | sed 's/^/  /'

banner "Chat with Hermes the way Toad does" \
  "Toad is a terminal chat for agents that speak ACP, the Agent Client Protocol; make toad opens it." \
  "Here a scripted ACP client plays Toad's part: each prompt goes from the client to the hub, to" \
  "the controller, to the Hermes Cog, to Hermes and its model, and the answer comes back the same way."
run "make toad   (scripted here)" MODEL_SOURCE=fake acp-check | tee "$LOCAL/acp-check.out"
grep -q "The fake model heard: hello hermes" "$LOCAL/acp-check.out" || fail "Hermes did not answer through the fake model"
ok "Hermes answered, through the hub"

banner "Say one more thing from the CLI" \
  "The same path without a chat client: one turn, one answer."
run 'collab-hub run say RUN "one more thing"' MODEL_SOURCE=fake say TEXT="one more thing"

banner "Stop Hermes" \
  "Terminating the run stops the Cog's process and Hermes with it; the run takes no more turns."
run "collab-hub run terminate RUN" MODEL_SOURCE=fake stop
! cli run say "$(cat $LOCAL/run)" hello > /dev/null 2>&1 || fail "a terminated run still took a turn"
! pgrep -f 'cogs/hermes/[.]pixi/envs' > /dev/null || fail "a Hermes worker outlived its run"
ok "the run ended, and no Hermes process is left"

banner "Chat with Claude, for real" \
  "With ANTHROPIC_API_KEY set, the controller hands Hermes Claude ($CLAUDE_MODEL) instead of the" \
  "fake model, through Hermes's own Anthropic provider. This step costs a few Claude tokens."
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  skip "Skipped: ANTHROPIC_API_KEY is not set, so there is no Claude to talk to."
  skip "To see it: export ANTHROPIC_API_KEY=... (https://platform.claude.com), then make demo again."
elif ! run "make claude" MODEL_SOURCE=claude claude; then
  skip "Skipped: Anthropic did not accept the key (above), so Hermes stays on the fake model."
else
  echo
  printf '  %sRestarting the controller with Claude, and launching Hermes again.%s\n\n' "$DIM" "$OFF"
  make -s --no-print-directory shutdown ONLY=controller > /dev/null
  make -s --no-print-directory start ONLY=controller MODEL_SOURCE=claude | sed -e 's/^  //' -e 's/^/  /'
  echo
  run "collab-hub cog launch hermes --entry session" MODEL_SOURCE=claude launch
  wait_running
  echo
  run "make toad   (scripted here)" MODEL_SOURCE=claude acp-check \
    PROMPTS='"In one sentence: who are you, and which model do you run on?" "What is 17 times 23? Answer with the number only."' \
    | tee "$LOCAL/acp-check-claude.out"
  grep -q "391" "$LOCAL/acp-check-claude.out" || fail "Claude's answer is not 391"
  echo
  run "collab-hub run terminate RUN" MODEL_SOURCE=claude stop
  ok "Hermes chatted with Claude, through the hub, and stopped"
fi

banner "Shut down" \
  "Stop the API, the controller and the fake model. The containers keep running for next time;" \
  "make down stops them, and make clean forgets this demo's sign-in and logs."
run "make shutdown" shutdown
! curl -sf -m 2 "$COLLAB_HUB_URL/health" > /dev/null || fail "the API is still answering"
ok "nothing of the demo is left running"

printf '\n\n%s%sDone.%s Signed in, launched Hermes, saw it running, chatted with it over ACP and the CLI, and stopped it.\n' "$BOLD" "$GREEN" "$OFF"
printf '%sNext: the same by hand, with Toad, as README.md walks through it: make start, make login, make launch, make toad.%s\n' "$DIM" "$OFF"
