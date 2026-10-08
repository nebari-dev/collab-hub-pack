# The harness Cogs

A harness Cog wraps an agent harness, so the hub can launch it, hold a session with it and relay turns to it without knowing which harness it is. Each one is a directory here: a pixi package whose `serve` task starts a worker that serves the hub's seam (`GET /healthz`, `POST /invoke`, `POST /turn` while a session is open; [`op-cog-seam.md`](../docs/cog-execution/op-cog-seam.md)) and drives the harness behind it.

| Cog | Harness | How it drives it | Model |
|---|---|---|---|
| [`hermes`](hermes/README.md) | [Hermes Agent](https://hermes-agent.nousresearch.com) 0.19 | as its [ACP](https://agentclientprotocol.com) client, `hermes acp` with no tools | any OpenAI-compatible endpoint, or Claude through Hermes's Anthropic provider |

## Running one

The run controller finds packages here by name (`make -C dev controller` reads this directory), so `collab-hub cog launch hermes --entry session` launches the Hermes Cog. [`examples/cog-local`](../examples/cog-local/README.md) does it end to end; `make toad` there leaves you chatting with Hermes in Toad.

## Conventions

- **A package of its own.** `pixi.toml` pins the harness and declares the `serve` task, and `pixi.lock` pins its environment; the controller refuses a package without its lock. The harness never shares the hub's environment.
- **Its model comes from the controller.** The worker reads it from the environment it is delivered (`COLLAB_MODEL_*`), and hands the harness that model alone: no other key, and no credentials of the machine's own.
- **Tested without the harness, and with it.** Each Cog's tests run its worker against a stand-in agent in the execution suite, and the harness itself where its environment is installed.
