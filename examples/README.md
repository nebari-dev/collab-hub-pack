# Examples

Each example is a directory you can run from end to end on your own machine, with a README that walks through it step by step and a Makefile with one target per step. `make help` in an example lists its steps, and `make demo` runs them all and checks the result, which is also how CI runs it.

| Example | What it shows | Needs |
|---|---|---|
| [`cog-local`](cog-local/README.md) | A [Hermes](https://hermes-agent.nousresearch.com) agent's whole life as a Cog on a hub running on your machine: sign in with the `collab-hub` CLI, launch Hermes, list it, talk to it from [Toad](https://github.com/batrachianai/toad) over the Agent Client Protocol, and stop it | Docker, uv; `make env` installs the rest. A fake model by default, or any OpenAI-compatible one |

## Conventions

- **Self-contained.** An example keeps its state in its own `.local/` (git-ignored), including the CLI's sign-in, so it never touches your own `collab-hub` profile. `make clean` removes it.
- **Built on `dev/`.** An example starts the hub through the [dev environment](../dev/README.md)'s targets rather than its own copies, so it runs the hub the way development does.
- **Checked in CI.** `make demo` asserts each step, and the `Examples` workflow runs it on every pull request that touches what the example uses, so an example that stops working fails it.
- **Not for production.** Examples use the development realm's users and the development hub's settings. Deploying the hub is [`docs/standalone-deployment.md`](../docs/standalone-deployment.md).
