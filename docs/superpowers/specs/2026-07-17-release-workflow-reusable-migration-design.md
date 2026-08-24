# Migrate the template release workflow to the reusable `pack-release.yaml@v1`

- **Date:** 2026-07-17
- **Repo:** nebari-dev/software-pack-template (renamed from nebari-software-pack-template)
- **Status:** Approved (design)

## Goal

Replace the template's hand-rolled `release.yaml` with a thin caller of the
shared reusable workflow `nebari-dev/.github/.github/workflows/pack-release.yaml@v1`,
so the template becomes a faithful, copy-paste-ready reference that mirrors what
real nebari-dev packs (e.g. `llm-serving-pack`) already use. The primary
audience is pack authors who need an easy on-ramp.

This pattern only works for **official packs in the nebari-dev org**: the
reusable workflow syncs the packaged chart into `nebari-dev/helm-repository`
(which then publishes to `quay.io/nebari/charts`) using the org's
`NEBARI_HELM_REPO_TOKEN` secret. Forks outside the org would need their own
publishing infrastructure.

## Background / current state

`.github/workflows/release.yaml` today is a ~106-line manual
(`workflow_dispatch`) workflow that packages a chosen example chart, creates a
GitHub Release, and maintains a **self-hosted Helm repo index on the template's
own `gh-pages` branch**. This is inconsistent with the template's own docs,
which tell authors to publish to the central
`nebari-dev.github.io/helm-repository`. The docs site deploys to Cloudflare
Pages, so the `gh-pages` branch is used only for this Helm index; removing it is
a clean simplification.

The reusable `pack-release.yaml@v1` (verified by reading its source) is called
via `workflow_call` and:

1. Reads the chart version from `<chart-path>/Chart.yaml`.
2. Skips if a GitHub Release for `<chart-name>-<version>` already exists
   (idempotent).
3. Pins each `tag-paths` dotted `values.yaml` key to `sha-<short7>` of the
   release commit, in the working copy only (never committed back).
4. Packages the chart and attaches the `.tgz` to a GitHub Release
   (auto-detects prerelease from a `-` in the version).
5. Syncs the pinned chart source into `nebari-dev/helm-repository` (opens a PR
   there) via the shared `sync-chart` action.

Required inputs: `chart-path`, `chart-name`, `tag-paths`. Required secret:
`NEBARI_HELM_REPO_TOKEN`.

Syntax for `uses:` / `with:` / `secrets:` at job level confirmed against the
official GitHub Actions "Reusing workflows" docs; the `on: push: paths:` +
reusable-workflow-call combination is already proven by the working
`llm-serving-pack` caller.

## Scope decision

Wire **one representative example: `auth-fastapi`**. It is the only example with
a first-party built image, so it is the only one that meaningfully demonstrates
the workflow's `tag-paths` image-pinning feature. `basic-nginx` (upstream
`nginx`) and `wrap-existing-chart` (podinfo subchart) have no first-party image
to pin and remain unchanged.

## Design

### 1. `.github/workflows/release.yaml` (rewrite)

Replace the entire inline workflow with a thin caller mirroring
`llm-serving-pack`:

```yaml
name: Release Chart

# Reusable release workflow for official Nebari packs in the nebari-dev org.
# Publishes the chart to nebari-dev/helm-repository (-> quay.io/nebari/charts)
# using the org's NEBARI_HELM_REPO_TOKEN secret. Forks outside nebari-dev must
# supply their own publishing infrastructure. Pack authors adapt chart-path,
# chart-name, and tag-paths (dotted values.yaml keys to pin to the release sha).

on:
  push:
    branches: [main]
    paths:
      - "examples/auth-fastapi/chart/Chart.yaml"

jobs:
  release:
    uses: nebari-dev/.github/.github/workflows/pack-release.yaml@v1
    with:
      chart-path: examples/auth-fastapi/chart
      chart-name: my-pack
      tag-paths: |
        image.tag
    secrets:
      NEBARI_HELM_REPO_TOKEN: ${{ secrets.NEBARI_HELM_REPO_TOKEN }}
```

Trigger: **push on `Chart.yaml` change** (matches the real pack pattern the
template teaches). Tradeoff accepted: a version bump to the example opens a
human-gated PR adding `my-pack` to the central registry.

### 2. `.github/workflows/build-image.yaml` -> `build-images.yaml` (full migration)

The release workflow pins `image.tag` to `sha-<short7>` of the release commit,
so that image must be built and pushed with the `sha-<short7>` tag on the same
commit that bumps `Chart.yaml`. Rather than patch the hand-rolled workflow,
migrate it fully to the shared reusable `pack-build-image.yaml@v1`, copying the
`llm-serving-pack` implementation so the template demonstrates both reusable
workflows as a matched pair.

Rename the file to `build-images.yaml` (matches the canonical `llm-serving-pack`
name) and replace its body with a single job calling the reusable workflow:

```yaml
name: Build Docker Images

on:
  push:
    branches: [main]
    paths:
      - "examples/auth-fastapi/app/**"
      - "examples/auth-fastapi/Dockerfile"
      - ".github/workflows/build-images.yaml"
      # The release workflow pins images to sha-<release-commit>, so the
      # release commit (a Chart.yaml bump) must produce images with that
      # sha. Building here on the bump ensures they exist.
      - "examples/auth-fastapi/chart/Chart.yaml"
  pull_request:
    paths:
      - "examples/auth-fastapi/app/**"
      - "examples/auth-fastapi/Dockerfile"
  workflow_dispatch:

jobs:
  auth-fastapi-example:
    uses: nebari-dev/.github/.github/workflows/pack-build-image.yaml@v1
    with:
      image: auth-fastapi-example
      context: examples/auth-fastapi
      push: ${{ github.event_name != 'pull_request' }}
    secrets:
      QUAY_TOKEN: ${{ secrets.QUAY_TOKEN }}
      QUAY_USERNAME: ${{ secrets.QUAY_USERNAME }}
```

The reusable workflow tags `sha-<short7>` + `latest` and pushes to both GHCR and
Quay, deriving the image names from the repository name
(`github.event.repository.name`). The repo has since been **renamed** from
`nebari-software-pack-template` to `software-pack-template`, so with
`image: auth-fastapi-example` the GHCR path is
`ghcr.io/nebari-dev/software-pack-template/auth-fastapi-example` and Quay is
`quay.io/nebari/software-pack-template-auth-fastapi-example`. The reusable
workflow's default `dockerfile` is `<context>/Dockerfile` =
`examples/auth-fastapi/Dockerfile`, which exists.

`values.yaml` stays `tag: "latest"` (the release pins in the working copy only).

### 3. Docs / README

- Rewrite the README "Release (`release.yaml`)" section (~L573) and the
  file-tree comment (~L124) to describe the new flow: calls the reusable
  workflow, pins image tags to the release sha, publishes to the central
  `nebari-dev/helm-repository`, official-packs-only note. Remove the
  self-hosted `gh-pages` index wording.
- Rewrite the README "Build Image" section (~L542) and file-tree comment
  (~L120) for the renamed `build-images.yaml`: it calls the reusable
  `pack-build-image.yaml@v1` and publishes to GHCR and Quay with
  `sha-<short7>` + `latest` tags.
- Update `docs/site/src/content/docs/release-readiness.md` and
  `docs/release-readiness-checklist.md` release-engineering wording to point at
  the reusable workflow where relevant (light touch; the "publishable to
  `nebari-dev.github.io/helm-repository`" criterion already matches the new
  flow).
- Update `docs/site/src/content/docs/build-your-own.md` if it needs a pointer to
  the reusable release workflow.

### 4. Repo-rename reference updates

The rename to `software-pack-template` makes the reusable image workflow publish
to a new GHCR/Quay path, so update every reference coupled to the old name:

- `examples/auth-fastapi/chart/values.yaml` `image.repository` -> new GHCR path.
- `.github/workflows/test.yaml` and `test-integration.yaml` local image
  build/load tags -> new GHCR path (so kind tests use the image the chart
  references).
- `README.md` CI badges, repo file-tree root, and the local `docker run`
  example; `examples/auth-fastapi/README.md` `docker run` example.
- `docs/site/src/content/docs/{build-your-own,index}.md` "use this template"
  links.
- Intentionally left unchanged: `CF_PROJECT: nebari-software-pack-template` in
  `docs.yml` / `docs-preview-cleanup.yml` (the Cloudflare Pages project name is
  independent of the GitHub repo name; renaming it would break docs deploys),
  and historical `docs/superpowers/plans` and `specs` records.

## Prerequisites / risks

- The template repo must have `NEBARI_HELM_REPO_TOKEN` (release sync) and
  `QUAY_TOKEN` / `QUAY_USERNAME` (image push) available. These are provided
  organizationally to all public nebari-dev repos, and this repo is public.
- **Manual step:** the shared image workflow does not create Quay repositories.
  A maintainer must create `quay.io/nebari/software-pack-template-auth-fastapi-example`
  (in general `quay.io/nebari/<repo-name>-<image>`) and grant the CI robot write
  access before the first image push. GHCR auto-creates on push. Documented in
  the README Build Images section.
- Auto-publish tradeoff (see trigger decision above): accepted.

## Definition of done (verification)

CI/CD workflow changes; verified without triggering real releases:

- `actionlint` (or the repo's existing workflow lint) passes on the changed
  workflow YAML.
- The `release.yaml` job's `uses` ref and `with`/`secrets` keys match the
  reusable workflow's declared interface (`chart-path`, `chart-name`,
  `tag-paths`, `NEBARI_HELM_REPO_TOKEN`).
- `helm lint` and `helm package` succeed for `examples/auth-fastapi/chart`.
- `build-images.yaml` is valid YAML, calls `pack-build-image.yaml@v1` with
  inputs matching its interface, and the old `build-image.yaml` is removed.
- README and docs no longer reference the self-hosted `gh-pages` Helm index and
  accurately describe the reusable-workflow flow.

## Delivery sequence

1. Open a tracking issue in `nebari-dev/software-pack-template`.
2. Create a git worktree for an isolated branch.
3. Apply the changes above (including this spec doc) in the worktree.
4. Fresh-eyes review, run the verification checks.
5. Commit, push, open a PR referencing the issue.
