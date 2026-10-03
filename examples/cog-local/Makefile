# A Hermes agent's whole life as a Cog, on a hub running on your machine: one target per step.
# README.md walks through them; `make help` lists them; `make demo` runs them all.

ROOT  := $(abspath ../..)
DEV   := $(ROOT)/dev
LOCAL := $(CURDIR)/.local
PORT  ?= 8000

# The CLI keeps this example's sign-in in a profile of its own, so yours is left alone.
export COLLAB_HUB_URL        := http://127.0.0.1:$(PORT)
export COLLAB_HUB_CONFIG_DIR := $(LOCAL)/cli
CLI := uv run --quiet --project $(ROOT)/cli collab-hub

# The Cog to launch, and the run the steps below act on: the last one `make launch` started.
COG  ?= hermes
RUN  ?= $(shell cat $(LOCAL)/run 2>/dev/null)
TEXT ?= hello

# The model Hermes calls, decided once and handed down to every step:
#   yours   COLLAB_MODEL_BASE_URL (with COLLAB_MODEL_NAME, COLLAB_MODEL_API_KEY) is set: that endpoint
#   claude  ANTHROPIC_API_KEY is set: Claude (CLAUDE_MODEL), through Hermes's Anthropic provider
#   fake    neither: the fake model `make start` runs on port 8090
# `make claude` says which, and checks the key. `make demo` always uses the fake one.
CLAUDE_MODEL ?= claude-opus-5-5
ifndef MODEL_SOURCE
  ifneq ($(origin COLLAB_MODEL_BASE_URL),undefined)
    MODEL_SOURCE := yours
  else ifneq ($(strip $(ANTHROPIC_API_KEY)),)
    MODEL_SOURCE := claude
  else
    MODEL_SOURCE := fake
  endif
endif
export MODEL_SOURCE
ifeq ($(MODEL_SOURCE),claude)
export COLLAB_MODEL_PROVIDER := anthropic
export COLLAB_MODEL_NAME     := $(CLAUDE_MODEL)
export COLLAB_MODEL_API_KEY  := $(ANTHROPIC_API_KEY)
endif
ifeq ($(MODEL_SOURCE),fake)
export COLLAB_MODEL_PROVIDER := openai-compatible
export COLLAB_MODEL_BASE_URL := http://127.0.0.1:8090/v1
export COLLAB_MODEL_NAME     := fake-model
export COLLAB_MODEL_API_KEY  :=
endif

.DEFAULT_GOAL := help
.PHONY: help env tools hub credentials claude model api controller start login login-token whoami cogs launch \
        list toad say acp-check stop shutdown down clean demo

help: ## List the steps
	@awk 'BEGIN {FS = ":.*## "} /^[a-z-]+:.*## / {printf "  make %-12s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

$(LOCAL):
	@mkdir -p $@

# --- 0. what the example needs ------------------------------------------------------------------

env: ## Install what the example uses: the CLI, the hub, Toad, pixi, and Hermes's environment
	@command -v uv >/dev/null || { echo "install uv first: https://docs.astral.sh/uv/getting-started/installation/"; exit 1; }
	cd $(ROOT)/cli && uv sync --quiet
	cd $(ROOT)/api && uv sync --quiet --group runs
	cd $(ROOT)/execution && uv sync --quiet
	uv tool install --quiet -U batrachian-toad --python 3.14
	@command -v pixi >/dev/null || { echo "installing pixi (https://pixi.sh)"; curl -fsSL https://pixi.sh/install.sh | bash; }
	PATH="$$HOME/.pixi/bin:$$PATH" pixi install --manifest-path $(ROOT)/cogs/hermes/pixi.toml
	PATH="$$HOME/.pixi/bin:$$PATH" pixi install --manifest-path $(CURDIR)/cogs/hello/pixi.toml
	@echo "Ready. If pixi or toad was just installed, open a new shell, or add ~/.pixi/bin and ~/.local/bin to PATH."

tools: ## Check what the example needs: docker, uv, pixi, and toad for `make toad`
	@for tool in docker uv pixi; do command -v $$tool >/dev/null \
		&& echo "  ok      $$tool" || { echo "  missing $$tool"; missing=1; }; done; \
	command -v toad >/dev/null && echo "  ok      toad" || echo "  missing toad (only for make toad): make env"; \
	test -d $(ROOT)/cogs/hermes/.pixi/envs/default && echo "  ok      Hermes's environment" \
		|| echo "  missing Hermes's environment (the first launch installs it, slowly): make env"; \
	test -z "$$missing"

# --- 1. the hub, and who signs in to it ---------------------------------------------------------

hub: ## Start Postgres and Keycloak (containers), with the realm and its users
	$(MAKE) -C $(DEV) services keycloak

credentials: | $(LOCAL) ## Show who to sign in as, and check the realm issues them a token
	@echo "  realm    http://localhost:8080/realms/nebari  (client apollo-desktop)"
	@echo "  user     dev"
	@echo "  password dev"
	@$(MAKE) -s --no-print-directory -C $(DEV) token > /dev/null && echo "  the realm signs dev in: ok" \
		|| { echo "  the realm did not issue a token: is it up? (make hub)"; exit 1; }

# --- 2. the processes: a model, the API that accepts runs, the controller that runs them ---------

claude: ## Check ANTHROPIC_API_KEY, and that with it Hermes chats with Claude for real
	@if [ -z "$$ANTHROPIC_API_KEY" ]; then \
		echo "  ANTHROPIC_API_KEY is not set: Hermes uses the fake model."; \
		echo "  export ANTHROPIC_API_KEY=... (https://platform.claude.com), then make claude again."; exit 1; fi
	@answer=$$(curl -s -w '\n%{http_code}' https://api.anthropic.com/v1/models/$(CLAUDE_MODEL) \
		-H "x-api-key: $$ANTHROPIC_API_KEY" -H "anthropic-version: 2023-06-01"); \
	code=$$(printf '%s' "$$answer" | tail -1); \
	if [ "$$code" != 200 ]; then \
		echo "  ANTHROPIC_API_KEY is set, but Anthropic answered HTTP $$code for $(CLAUDE_MODEL):"; \
		printf '%s\n' "$$answer" | sed '$$d' | head -c 400; echo; exit 1; fi; \
	name=$$(printf '%s' "$$answer" | sed '$$d' | python3 -c 'import json,sys; print(json.load(sys.stdin)["display_name"])'); \
	echo "  ANTHROPIC_API_KEY is set, and Anthropic accepts it: $$name ($(CLAUDE_MODEL)) is available."
	@if [ "$(MODEL_SOURCE)" = claude ]; then \
		echo "  Hermes will chat with Claude for real: start (or restart) the controller from here,"; \
		echo "  with make controller or make start, and launch Hermes."; \
	else echo "  But COLLAB_MODEL_BASE_URL is set, and takes precedence: unset it to use Claude."; fi

model: ## The fake model Hermes calls, on port 8090 (foreground); not needed with Claude or your own
	$(MAKE) -C $(DEV) fake-model

api: ## The hub's API, verifying the realm's tokens (foreground)
	$(MAKE) -C $(DEV) api-oidc API_PORT=$(PORT)

controller: ## The run controller, which starts each Cog as a local process (foreground)
	$(MAKE) -C $(DEV) controller

start: | $(LOCAL) ## The fake model (if used), the API and the controller in the background; logs in .local/
	@echo "  Hermes's model: $(MODEL_SOURCE)$(if $(filter claude,$(MODEL_SOURCE)), ($(CLAUDE_MODEL)))"
	@for name in $(if $(filter fake,$(MODEL_SOURCE)),model) api controller; do \
		bash -c "set -m; $(MAKE) -s $$name > $(LOCAL)/$$name.log 2>&1 & echo \$$! > $(LOCAL)/$$name.pid"; done
	@printf "waiting for the hub on $(COLLAB_HUB_URL) "; for _ in $$(seq 1 240); do \
		curl -sf $(COLLAB_HUB_URL)/health > /dev/null && { echo "up"; exit 0; }; printf .; sleep 1; done; \
		echo " it never answered:"; tail -20 $(LOCAL)/api.log; exit 1

# --- 3. the CLI ---------------------------------------------------------------------------------

login: ## Sign in through the browser, as dev / dev
	$(CLI) login

login-token: ## Sign in without a browser: a token from the realm for dev / dev
	$(MAKE) -s --no-print-directory -C $(DEV) token | $(CLI) login --with-token

whoami: ## Who the hub says you are
	$(CLI) whoami

cogs: ## The Cogs this hub can launch
	$(CLI) cog list --launchable

launch: | $(LOCAL) ## Launch Hermes (COG=hermes), holding a session; the run's id goes to .local/run
	$(CLI) cog launch $(COG) --entry session --gate never > $(LOCAL)/run
	@echo "  run $$(cat $(LOCAL)/run)"

list: ## The runs you launched
	$(CLI) run list

toad: ## Talk to the running Hermes from Toad, an ACP client
	toad acp --title "$(COG) · $(RUN)" "$(CLI) run connect $(RUN)" $(CURDIR)

say: ## Say one thing to it from the CLI instead: make say TEXT="what can you do?"
	$(CLI) run say $(RUN) $(TEXT)

acp-check: ## What Toad does, scripted: an ACP client sends a few prompts through `run connect`
	uv run --quiet --project $(ROOT)/cli python $(CURDIR)/acp_check.py $(RUN) "hello hermes" "what can you do?"

stop: ## Stop the Cog: terminate its run
	$(CLI) run terminate $(RUN)

# --- 4. shutting down ---------------------------------------------------------------------------

shutdown: ## Stop what `make start` started
	@tree() { echo $$1; for child in $$(pgrep -P $$1); do tree $$child; done; }; \
	for name in api controller model; do test -f $(LOCAL)/$$name.pid || continue; \
		pids=$$(tree $$(cat $(LOCAL)/$$name.pid)); kill -TERM $$pids 2>/dev/null; \
		for _ in $$(seq 1 50); do alive=; for pid in $$pids; do kill -0 $$pid 2>/dev/null && alive=1; done; \
			test -n "$$alive" || break; sleep 0.2; done; \
		kill -KILL $$pids 2>/dev/null; rm -f $(LOCAL)/$$name.pid; echo "  stopped the $$name"; done; true

down: shutdown ## Stop the containers too (their data is kept)
	$(MAKE) -C $(DEV) down

clean: shutdown ## Forget this example's sign-in, run and logs
	rm -rf $(LOCAL)

# --- all of it ----------------------------------------------------------------------------------

demo: | $(LOCAL) ## Every step in order, against the fake model, with no browser and no terminal UI: what CI runs
	$(MAKE) MODEL_SOURCE=fake tools hub credentials start login-token whoami cogs launch list
	$(MAKE) -s MODEL_SOURCE=fake acp-check | tee $(LOCAL)/acp-check.out
	@grep -q "The fake model heard: hello hermes" $(LOCAL)/acp-check.out \
		|| { echo "Hermes did not answer through the fake model"; exit 1; }
	$(MAKE) say TEXT="one more thing"
	$(MAKE) stop list
	@! $(CLI) run say $(RUN) hello 2>/dev/null || { echo "a terminated run still took a turn"; exit 1; }
	@! pgrep -f 'cogs/hermes/[.]pixi/envs' > /dev/null || { echo "a Hermes worker outlived its run"; exit 1; }
	$(MAKE) shutdown
	@! curl -sf -m 2 $(COLLAB_HUB_URL)/health > /dev/null || { echo "the API is still answering"; exit 1; }
	@echo "Done: signed in, launched Hermes, listed it, talked to it over ACP and the CLI, and stopped it."
