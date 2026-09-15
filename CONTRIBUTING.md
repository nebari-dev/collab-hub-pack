# Contributing to Collab Hub API

Thanks for your interest in contributing. This pack is developed in the open
under the [Apache-2.0 license](LICENSE).

## Development setup

Run the pack from [`dev/`](dev/), which starts whatever a given level needs and
sets the environment for you:

```sh
cd dev
make help
make api      # the API alone: no containers, no token needed
make test     # the API test suite
make lint     # helm lint + kubeconform on the rendered manifests
```

There are four levels, from a bare process up to the chart on a kind cluster;
[`dev/README.md`](dev/README.md) walks through them and through the Keycloak
setup each connector needs. CI exercises all four —
[`.github/workflows/dev-env.yaml`](.github/workflows/dev-env.yaml).

The API itself lives in [`api/`](api/) and uses
[uv](https://docs.astral.sh/uv/), if you would rather drive it directly:

```sh
cd api
uv sync --group test        # install runtime + test deps
uv run pytest               # run the test suite
```

Running it by hand needs **all three** dev-auth switches —
`FRAMES_UNSAFE_AUTH_ENABLED=true`, `DEV_AUTH_ENABLED=true` and
`DEV_AUTH_USER=<name>`. Setting only `DEV_AUTH_USER` authenticates nothing and
every route answers 401, which is why `make api` is the easier path.

The Helm chart is in [`helm/collab-hub/`](helm/collab-hub/):

```sh
helm lint helm/collab-hub
helm template helm/collab-hub | kubeconform -strict -ignore-missing-schemas -
```

## Pull requests

- Open an issue first for anything non-trivial, and link it from the PR with a
  closing keyword (`Fixes #123`).
- CI (`lint`, `test`) must pass. Add tests with your change: unit tests for new
  functionality, regression tests for bug fixes.
- A [code owner](.github/CODEOWNERS) must approve before merge. Take PRs out of
  draft before requesting code-owner review.
- Keep the change focused; describe *how* it addresses the issue.

## Reporting security issues

Do not open a public issue for vulnerabilities. Use GitHub's
[Report a vulnerability](https://github.com/nebari-dev/collab-hub-pack/security/advisories/new)
to open a private advisory with the maintainers.
