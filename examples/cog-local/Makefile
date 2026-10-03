# A Cog's whole life on a hub running on your machine, one target per step.
# README.md walks through them; `make help` lists them; `make demo` runs them all.

ROOT  := $(abspath ../..)
DEV   := $(ROOT)/dev
LOCAL := $(CURDIR)/.local
PORT  ?= 8000

# The CLI keeps this example's sign-in in a profile of its own, so yours is left alone.
export COLLAB_HUB_URL        := http://127.0.0.1:$(PORT)
export COLLAB_HUB_CONFIG_DIR := $(LOCAL)/cli
CLI := uv run --quiet --project $(ROOT)/cli collab-hub

# The run the steps below act on: the last one `make launch` started, or RUN=...
RUN  ?= $(shell cat $(LOCAL)/run 2>/dev/null)
TEXT ?= help

.DEFAULT_GOAL := help
.PHONY: help env tools hub credentials api controller start login login-token whoami cogs launch list \
        connect say acp-check stop shutdown down clean demo

help: ## List the steps
	@awk 'BEGIN {FS = ":.*## "} /^[a-z-]+:.*## / {printf "  make %-12s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

$(LOCAL):
	@mkdir -p $@

# --- 1. the hub, and who signs in to it ---------------------------------------------------------

env: ## Install what the example uses: the CLI, the hub's Python, toad, pixi, and the Cog's environment
	@command -v uv >/dev/null || { echo "install uv first: https://docs.astral.sh/uv/getting-started/installation/"; exit 1; }
	cd $(ROOT)/cli && uv sync --quiet
	cd $(ROOT)/api && uv sync --quiet --group runs
	cd $(ROOT)/execution && uv sync --quiet
	uv tool install --quiet -U batrachian-toad --python 3.14
	@command -v pixi >/dev/null || { echo "installing pixi (https://pixi.sh)"; curl -fsSL https://pixi.sh/install.sh | bash; }
	PATH="$$HOME/.pixi/bin:$$PATH" pixi install --manifest-path $(CURDIR)/cogs/hello/pixi.toml
	@echo "Ready. If pixi or toad was just installed, open a new shell, or add ~/.pixi/bin and ~/.local/bin to PATH."

tools: ## Check what the example needs: docker, uv, pixi, and toad for `make connect`
	@for tool in docker uv pixi; do command -v $$tool >/dev/null \
		&& echo "  ok      $$tool" || { echo "  missing $$tool"; missing=1; }; done; \
	command -v toad >/dev/null && echo "  ok      toad" \
		|| echo "  missing toad (only for make connect): make env"; \
	test -z "$$missing"

hub: ## Start Postgres and Keycloak (containers), with the realm and its users
	$(MAKE) -C $(DEV) services keycloak

credentials: | $(LOCAL) ## Show who to sign in as, and check the realm issues them a token
	@echo "  realm    http://localhost:8080/realms/nebari  (client apollo-desktop)"
	@echo "  user     dev"
	@echo "  password dev"
	@$(MAKE) -s --no-print-directory -C $(DEV) token > /dev/null && echo "  the realm signs dev in: ok" \
		|| { echo "  the realm did not issue a token: is it up? (make hub)"; exit 1; }

# --- 2. the two processes: the API accepts runs, the controller runs them ------------------------

api: ## The hub's API, verifying the realm's tokens (foreground: terminal 1)
	$(MAKE) -C $(DEV) api-oidc API_PORT=$(PORT)

controller: ## The run controller, which starts each Cog as a local process (foreground: terminal 2)
	$(MAKE) -C $(DEV) controller

start: | $(LOCAL) ## Both of the above in the background instead, logs in .local/
	@bash -c 'set -m; $(MAKE) -s api > $(LOCAL)/api.log 2>&1 & echo $$! > $(LOCAL)/api.pid'
	@bash -c 'set -m; $(MAKE) -s controller > $(LOCAL)/controller.log 2>&1 & echo $$! > $(LOCAL)/controller.pid'
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

launch: | $(LOCAL) ## Launch hello, holding a session; the run's id goes to .local/run
	$(CLI) cog launch hello --entry session --gate never > $(LOCAL)/run
	@echo "  run $$(cat $(LOCAL)/run)"

list: ## The runs you launched
	$(CLI) run list

connect: ## Talk to the running Cog from Toad, an ACP client
	toad acp --title "hello · $(RUN)" "$(CLI) run connect $(RUN)" $(CURDIR)

say: ## Say one thing to the running Cog from the CLI: make say TEXT="sum 1 2 3"
	$(CLI) run say $(RUN) $(TEXT)

acp-check: ## What Toad does, scripted: an ACP client sends a few prompts through `run connect`
	uv run --quiet --project $(ROOT)/cli python $(CURDIR)/acp_check.py $(RUN) help "sum 1 2 3" whoami

stop: ## Stop the Cog: terminate its run
	$(CLI) run terminate $(RUN)

# --- 4. shutting down ---------------------------------------------------------------------------

shutdown: ## Stop the API and the controller that `make start` started
	@for name in api controller; do test -f $(LOCAL)/$$name.pid || continue; \
		group=$$(cat $(LOCAL)/$$name.pid); kill -TERM -- -$$group 2>/dev/null; \
		for _ in $$(seq 1 50); do kill -0 -- -$$group 2>/dev/null || break; sleep 0.2; done; \
		kill -KILL -- -$$group 2>/dev/null; rm -f $(LOCAL)/$$name.pid; echo "  stopped the $$name"; done; true

down: shutdown ## Stop the containers too (their data is kept)
	$(MAKE) -C $(DEV) down

clean: shutdown ## Forget this example's sign-in, run and logs
	rm -rf $(LOCAL)

# --- all of it ----------------------------------------------------------------------------------

demo: ## Every step in order, with no browser and no terminal UI: what CI runs
	$(MAKE) tools hub credentials start login-token whoami cogs launch list acp-check
	$(MAKE) say TEXT="hello Ada"
	$(MAKE) stop list
	@! $(CLI) run say $(RUN) hello 2>/dev/null || { echo "a terminated run still took a turn"; exit 1; }
	@! pgrep -f 'cog-local/cogs/hello/[.]pixi/envs' > /dev/null || { echo "a hello worker outlived its run"; exit 1; }
	$(MAKE) shutdown
	@echo "Done: signed in, launched, listed, talked to over ACP and the CLI, and stopped."
