# The Hermes harness Cog

[Hermes Agent](https://hermes-agent.nousresearch.com) as a Cog: the hub launches it, keeps a session open with it, and relays turns to it, without knowing it is Hermes. [`examples/cog-local`](../../examples/cog-local/README.md) walks through it end to end.

| File | What it is |
|---|---|
| `serve.py` | The worker the run controller starts. Standard library only |
| `hermes_acp.py` | How the worker starts Hermes: `hermes acp`, with no tools |
| `pixi.toml` | The package: Python 3.12 and `hermes-agent[acp]`, and the `serve` task |
| `pixi.lock` | That environment, pinned for Linux and macOS |

## What the worker does

It serves the seam every Cog worker serves (`GET /healthz`, `POST /invoke`, and `POST /turn` while a session is open, [`op-cog-seam.md`](../../docs/cog-execution/op-cog-seam.md)), and is an [ACP](https://agentclientprotocol.com) client of Hermes: it starts `hermes acp` as a child process and speaks JSON-RPC to it, one message per line.

| Entry point | Input | Does |
|---|---|---|
| `session` | none | Opens a Hermes session and holds it. Each turn the hub delivers is one prompt, answered with what Hermes said. It ends on `bye`, or when the run is terminated and the worker with it |
| `ask` | `{"prompt": "..."}` | Answers one prompt and returns `{"answer": "..."}` |

A turn that arrives while Hermes is still starting waits for the session, up to five minutes.

## Its model

The controller delivers the model in the worker's environment, to this Cog and no other:

| Variable | What |
|---|---|
| `COLLAB_MODEL_BASE_URL` | An OpenAI-compatible endpoint. Without it, the step fails `model-unavailable` |
| `COLLAB_MODEL_NAME` | The model's id at that endpoint |
| `COLLAB_MODEL_API_KEY` | Its key, if it needs one |

`make -C dev controller` delivers them, with [`dev/fake-model`](../../dev/fake-model/fake_model.py) as the default: an endpoint that answers `The fake model heard: ...` with no account and no network. Model bindings resolved by the hub replace this in a later phase of the plan.

## What Hermes sees

- **A home of its own.** Each session gets a temporary workspace, and `HERMES_HOME` inside it holds only a `config.yaml` naming the delivered model. Nothing of the machine's own `~/.hermes` is read or changed.
- **Not the hub's secrets.** The run token and the model key are removed from Hermes's environment; the key reaches Hermes only through its config file, which is readable by its owner alone.
- **No tools.** The first Hermes run is prompt in, answer out (decision 14 of the plan): no command, no file, no browser, no web. Hermes 0.19's ACP adapter enables its whole toolset for every session and reads no setting that narrows it, so the worker starts Hermes through [`hermes_acp.py`](hermes_acp.py), which gives each session no toolsets at all. Its model is then offered no tools, and a tool call it makes anyway fails with `Tool '...' does not exist`; a test checks it with a model that orders a shell command. Should Hermes ask its client's permission for anything, the worker refuses.

## Running it

From the hub, which is the point: `collab-hub cog launch hermes --entry session`, then `collab-hub run say RUN ...` or `toad acp "collab-hub run connect RUN"`.

Its tests run the real worker against a stand-in ACP agent, and Hermes itself against the fake model when this environment is installed: `execution/tests/test_hermes_cog.py`.
