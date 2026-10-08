#!/usr/bin/env bash
# `make toad`: from nothing to chatting with Hermes on Claude, in Toad, in this terminal.
#
# Each step is explained, as in `make demo`: start the hub, sign in, launch
# Hermes as a Cog talking to Claude, and open Toad on it. When you leave Toad,
# Hermes and the hub keep running; the last lines say how to come back or stop.
set -euo pipefail
cd "$(dirname "$0")"

STEPS=7
. ./steps.sh

trap 'status=$?; if [ $status -ne 0 ]; then stop_all; printf "\n  %sStopped at step %d, and shut down what it had started. Logs: %s/*.log%s\n" "$RED" "$step" "$LOCAL" "$OFF"; fi' EXIT

printf '%sChat with Hermes, running as a Cog on a hub on this machine, talking to Claude.%s\n' "$BOLD" "$OFF"
printf '%sEach step below is a make target in this directory; README.md explains each one.%s\n' "$DIM" "$OFF"

banner "Check the tools" \
  "Docker for Postgres and Keycloak, uv for the Python environments, pixi for the Cog's own" \
  "environment, and Toad, the terminal chat this ends in. make env installs what is missing."
run "make tools" MODEL_SOURCE=claude tools
command -v toad > /dev/null || fail "Toad is not installed: make env installs it (uv tool install batrachian-toad)"

banner "Check the Claude key" \
  "Hermes will talk to Claude ($CLAUDE_MODEL) through its own Anthropic provider, with your" \
  "ANTHROPIC_API_KEY. The key is checked with Anthropic before anything starts."
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  skip "ANTHROPIC_API_KEY is not set, and this needs Claude."
  skip "export ANTHROPIC_API_KEY=... (https://platform.claude.com), then make toad again."
  skip "Without a key, make demo walks through the same steps against a fake model."
  trap - EXIT
  exit 1
fi
run "make claude" MODEL_SOURCE=claude claude || fail "Anthropic did not accept the key (above)"

banner "Start Postgres and Keycloak" \
  "Two containers: the hub's database, and the identity provider you sign in with." \
  "Docker's output goes to $LOCAL/toad-hub.log."
printf '  %s$ make hub%s\n' "$BOLD" "$OFF"
make -s --no-print-directory hub > "$LOCAL/toad-hub.log" 2>&1 || { tail -20 "$LOCAL/toad-hub.log"; fail "the containers did not start"; }
ok "Postgres and Keycloak are up"

banner "Start the hub, on Claude" \
  "The hub's API, which records what you ask, and the run controller, which starts Cogs and hands" \
  "Hermes its model: Claude. Anything a previous make start left running is stopped first."
stop_all
run "make start" MODEL_SOURCE=claude start

banner "Sign in with the CLI" \
  "collab-hub signs in to the hub through Keycloak, as dev. (make login does it in your browser.)"
run "make login-token" MODEL_SOURCE=claude login-token

banner "Launch Hermes, on Claude" \
  "The controller starts the Hermes Cog as a process of its own, which starts Hermes and opens a" \
  "session with it. Hermes has no tools here: it answers, and does nothing else."
run "collab-hub cog launch hermes --entry session --name hermes-on-$CLAUDE_MODEL" MODEL_SOURCE=claude launch
wait_running
echo
printf '  %s$ collab-hub run list%s\n\n' "$BOLD" "$OFF"
cli run list > "$LOCAL/runs.out"
head -4 "$LOCAL/runs.out" | sed 's/^/  /'
ok "Hermes is running on Claude"

banner "Open Toad" \
  "Toad starts collab-hub run connect as its agent: each message you type goes to the hub, which" \
  "hands it to Hermes, which asks Claude; the answer comes back the same way. Type a message and" \
  "press Enter. Leave Toad with ctrl+q: Hermes keeps running."
printf '  %s$ make connect%s\n' "$BOLD" "$OFF"
sleep 2
make -s --no-print-directory MODEL_SOURCE=claude connect

run_id=$(cat "$LOCAL/run")
printf '\n%sYou left Toad. Hermes (%s) and the hub are still running:%s\n' "$BOLD" "$run_id" "$OFF"
printf '  make connect     open Toad on it again\n'
printf '  make say TEXT="..."   one message from the CLI\n'
printf '  make stop        stop Hermes\n'
printf '  make shutdown    stop the hub (make down stops the containers too)\n'
