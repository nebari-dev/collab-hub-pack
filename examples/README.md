# Examples

Each example is a directory you can run from end to end on your own machine, with a README that walks through it step by step and a Makefile with one target per step. `make help` in an example lists its steps, and `make demo` runs them all and checks the result, which is also how CI runs it.

| Example | What it shows | Needs |
|---|---|---|
| [`cog-local`](cog-local/README.md) | A Cog's whole life on a hub running on your machine: sign in with the `collab-hub` CLI, launch a Cog, list it, talk to it from [Toad](https://github.com/batrachianai/toad) over the Agent Client Protocol, and stop it | Docker, uv; `make env` installs the rest |

## Conventions

- **Self-contained.** An example keeps its state in its own `.local/` (git-ignored), including the CLI's sign-in, so it never touches your own `collab-hub` profile. `make clean` removes it.
- **Built on `dev/`.** An example starts the hub through the [dev environment](../dev/README.md)'s targets rather than its own copies, so it runs the hub the way development does.
- **Checked in CI.** `make demo` asserts each step, and a workflow runs it, so an example that stops working fails a pull request.
- **Not for production.** Examples use the development realm's users and the development hub's settings. Deploying the hub is [`docs/standalone-deployment.md`](../docs/standalone-deployment.md).
