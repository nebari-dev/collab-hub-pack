# Contributing to Collab Hub API

Thanks for your interest in contributing. This pack is developed in the open
under the [Apache-2.0 license](LICENSE).

## Development setup

The API lives in [`api/`](api/) and uses [uv](https://docs.astral.sh/uv/).

```sh
cd api
uv sync --group test        # install runtime + test deps
uv run pytest               # run the test suite
uv run python -m collab_hub_api   # run locally (set DEV_AUTH_USER for unsafe local auth)
```

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
