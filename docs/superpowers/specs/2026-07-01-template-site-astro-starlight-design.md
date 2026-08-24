# Design: Migrate the "Building a Software Pack" docs site from Hugo to Astro + Starlight

- **Date:** 2026-07-01
- **Repos touched:** `nebari-dev/nebari-software-pack-template` (primary) and `nebari-dev/starlight` (`@nebari/starlight`, a small additive change)
- **Branch:** `feat/astro-starlight-docs` (off `main`)
- **Sub-project:** C ("per-pack docs template") of the wider Hugo -> Astro+Starlight replatform. Depends on A (`@nebari/starlight`, live on npm) and follows B (the dashboard, already live on Astro+Starlight).

## Goal

Replatform the guide served at `packs.nebari.dev/building-a-software-pack/` from Hugo (`nebari-hugo-theme`) to Astro + Starlight themed with `@nebari/starlight`, so that:

1. Maintainers of this guide write only Markdown; presentation comes from the shared theme.
2. The guide's look matches the portal and every other pack (unified presentation).
3. PR-preview deploys are preserved.
4. Built-in Starlight (Pagefind) search works over the guide, ready to join the portal-wide multisite index later.

This guide also serves as the reference implementation other pack maintainers copy, so its structure should be clean and copyable.

## Current state (Hugo)

- Site root: `docs/site/` (Hugo). Config `docs/site/hugo.toml`: `baseURL = https://packs.nebari.dev/building-a-software-pack/`, `nebari-hugo-theme` as a Hugo Module (pinned `v0.2.0` in `go.mod`), `params.logoLink = https://packs.nebari.dev/` (logo returns users to the portal), `params.editBase`, `params.search = true`, two manually configured sidebar groups.
- Content: `docs/site/content/*.md`, 7 files with `+++` TOML frontmatter:
  - `_index.md` (home / "Building a Software Pack" intro + links)
  - `what-is-a-software-pack.md` (weight 10)
  - `concepts.md` (**orphan** - not in the Hugo sidebar today)
  - `build-your-own.md` (weight 20)
  - `nebariapp-crd-reference.md`
  - `auth-flow.md`
  - `release-readiness.md`
  - Only two files carry `weight`; the sidebar order is driven by the **manual** `[[params.sidebar]]` config, not by weight.
- **No Hugo shortcodes.** The `{{ ... }}` occurrences in `concepts.md` and `nebariapp-crd-reference.md` are **Helm template syntax inside fenced code blocks** (literal example content), not shortcodes.
- **Internal links are all root-absolute** (13 of them), e.g. `](/auth-flow/)`, `](/nebariapp-crd-reference/#2-kubernetes-secret)`. Hugo's theme render-link hook resolves these against `baseURL`; Astro will not (see Base handling).
- CI: `.github/workflows/docs.yml` builds with Hugo and deploys `docs/site/public` to Cloudflare Pages (project `nebari-software-pack-template`, route slug `building-a-software-pack`), computing a per-environment `baseURL` (main -> portal subpath; PR -> `<alias>.<project>.pages.dev/` root) and commenting the preview URL on PRs. Skips deploy for fork PRs (no secrets).

## Approach

Clean re-platform in place: replace the Hugo project rooted at `docs/site/` with an Astro + Starlight project rooted at the same `docs/site/`, so the workflow path filter (`docs/site/**`), the Cloudflare project, and the repo layout are all preserved. Bun is the package manager and build runner (matches A and B). No Hugo, no Go module.

### Scope A - `@nebari/starlight`: add an optional `logoHref`

Today `SiteTitle.astro` links the logo to `import.meta.env.BASE_URL` (the site's own home). The Hugo site links it to the portal root, and every federated pack wants the same "logo returns to the portal" affordance. Add an optional `logoHref` string to the plugin:

```js
nebari({ logoHref: 'https://packs.nebari.dev/' })
```

- Default: `import.meta.env.BASE_URL` (unchanged behavior for current consumers - the dashboard keeps working untouched).
- Threading the value to the component: use a Vite **virtual module** (the same pattern Starlight uses for `virtual:starlight/user-config`); `SiteTitle.astro` imports the resolved `logoHref`, falling back to `BASE_URL`. The exact `addVitePlugin`/virtual-module API is verified against the Astro/Starlight plugin docs at implementation time.
- This is a backward-compatible, opt-in addition -> EffVer **MICRO** bump (`v0.1.6`). Published via the existing Trusted-Publishing release workflow.

The template then pins `@nebari/starlight: ^0.1.6` and passes `logoHref: 'https://packs.nebari.dev/'`.

### Scope C - the template migration

**Repo layout.** Astro project at `docs/site/`:
- `docs/site/package.json` (Bun; deps `astro`, `@astrojs/starlight`, `@nebari/starlight ^0.1.6`)
- `docs/site/astro.config.mjs`
- `docs/site/src/content/docs/*.md` (migrated content)
- `docs/site/src/content.config.ts` (Starlight docs collection)
- Build output `docs/site/dist/`
- Remove: `hugo.toml`, `go.mod`, `go.sum`, `content/`.

**Content migration.**
- Move each `content/*.md` to `src/content/docs/`; rename `_index.md` -> `index.md` (the Starlight home at the base root, a normal `doc` page with the sidebar, not a splash).
- Convert `+++` TOML frontmatter to `---` YAML: `title = "X"` -> `title: X`. Drop `weight` (sidebar order is explicit config).
- **Keep files as `.md`, never `.mdx`.** Astro's Markdown pipeline treats fenced code as literal, so the Helm `{{ ... }}` examples are safe; MDX would parse `{` as a JSX expression and break the build.
- Body prose, headings, and code fences are otherwise copied verbatim.

**Sidebar** (explicit config in `astro.config.mjs`, mirroring the Hugo groups, with `concepts` added):
- **Getting Started:** Introduction (`/`) -> What is a software pack -> **Concepts** (new) -> Build your own
- **Reference:** NebariApp CRD -> Authentication Flow -> Release Readiness

Starlight prepends `base` to sidebar `link` values automatically.

**Base handling (the crux of "base-safe links").**
- Astro's `base` is **dynamic**, resolved from `process.env.BASE_PATH` with a default of `/building-a-software-pack/`. This mirrors the proven Hugo model and is forced by how the portal Worker routes: `worker/src/router.js` (in the dashboard repo) strips the leading `/building-a-software-pack/` segment and proxies the rest to `nebari-software-pack-template.pages.dev`, so the Pages project must serve files at its root. Astro `base` prefixes link/asset URLs but does **not** nest the physical `dist/` output (files stay at `dist/` root; verified against the config reference and the theme's own base-path test). Therefore:
  - **Production (`main`):** base `/building-a-software-pack/`. Files at `dist/` root; body/nav links are prefixed with the subpath. The Worker strips the prefix, so `packs.nebari.dev/building-a-software-pack/auth-flow/` -> Pages `/auth-flow/`.
  - **PR preview:** base `/` (workflow sets `BASE_PATH=/`). Served at `<alias>.pages.dev/` directly (no Worker), so links must be root-relative. Files at `dist/` root.
  - Default (unset) is the production subpath, so local `bun run dev` / `bun run build` / tests match production.
- Confirmed against official docs: **Astro does not auto-prepend `base` to root-absolute Markdown links.** The Astro config reference documents `base` affecting only asset imports and `import.meta.env.BASE_URL`; the Markdown guide says nothing about link rewriting; Starlight's authoring guide only shows a bare `/getting-started/` example (valid only at root). So the 13 existing `/foo/` body links need an explicit mechanism.
- **Mechanism: a small rehype plugin** (`markdown.rehypePlugins` in `astro.config.mjs`) that prepends `base` to internal root-absolute `<a href>` / `<img src>` values (leaving external `//`, `http(s):`, `mailto:`, and in-page `#` links alone; collapsing duplicate slashes). The plugin is a pure factory taking the `base` constant. This preserves "maintainers write natural `/foo/` Markdown" and is testable in isolation. Body-link resolution is verified end-to-end by the build-time link check (journey 3).
  - Alternative considered and rejected: rewriting all body links to relative (`../auth-flow/`). Base-agnostic, but fragile under hierarchy changes, awkward with anchors, and pushes a rule onto maintainers. The rehype plugin keeps content clean.

**Theme + branding.** `starlight({ plugins: [nebari({ logoHref: 'https://packs.nebari.dev/' })], ... })`. The theme supplies colors, fonts, logo (light/dark), Head, and footer. No local CSS needed unless a specific gap appears.

**Search.** Starlight's default Pagefind. A `dist/pagefind/` bundle is produced by the build (at `dist/` root, like every other page). Registering this bundle in the dashboard's multisite `mergeIndex` is **out of scope** here (tracked as a follow-up in the dashboard repo).

**Edit links.** `editLink.baseUrl = https://github.com/nebari-dev/nebari-software-pack-template/edit/main/docs/site/src/content/docs/` (updated for the new content path). Starlight appends the per-page file path.

**Metadata.** Site `title: "Building a Software Pack"`, `description` carried over from `hugo.toml`.

**CI workflow (`docs.yml`) rewrite.** Keep the trigger (`push` to main + `pull_request`, path filter `docs/site/**`, `workflow_dispatch`), the concurrency group, the `pull-requests: write` permission, the Cloudflare project/env vars, the fork-PR deploy skip, and the PR-comment step. Change:
- Replace `setup-go` + `actions-hugo` with `oven-sh/setup-bun` (+ `setup-node` `lts/*` if needed by tooling).
- Compute `BASE_PATH` per environment: `main` -> `/building-a-software-pack/`, PR -> `/`. Also keep a `branch` value for the Cloudflare deploy (main vs head ref).
- Build: `cd docs/site && bun install --frozen-lockfile && BASE_PATH=<computed> bun run build`.
- Deploy: `pages deploy docs/site/dist` (was `docs/site/public`).
- PR comment: link to the deployment alias URL root (`<alias>.pages.dev/`), since previews build with a root base.
- Bump pinned actions off Node-20-deprecated majors where a newer major exists (e.g. `actions/checkout@v5`); keep `wrangler-action` and the comment action at their current latest majors, verified at implementation time.

## Journeys

A **reader** browses the guide at `packs.nebari.dev/building-a-software-pack/`. A **maintainer** edits the docs and opens a PR. (Accepted 2026-07-01. Revised 2026-07-02: items 1, 5, 7 updated for the dynamic-base correction - Astro emits files at `dist/` root, not nested under the base; the Worker strips the prefix in production and PR previews build with a root base.)

| # | Item | Proof | Check method | Evidence |
|---|------|-------|--------------|----------|
| 1 | All 7 pages render, with base-prefixed links | A production-base build emits `dist/index.html` plus `dist/<slug>/index.html` for each of the 7 content files (`what-is-a-software-pack`, `concepts`, `build-your-own`, `auth-flow`, `nebariapp-crd-reference`, `release-readiness`, and the home); each contains its expected title, and its internal links are prefixed with `/building-a-software-pack/` | automated: build + a test that asserts the 7 expected `dist/` paths exist and each contains its title | *(empty)* |
| 2 | Sidebar shows the 2 groups with `concepts` in the right slot | Rendered sidebar HTML contains both group labels; "Getting Started" lists `What is a software pack` then `Concepts` (new) then `Build your own`; every sidebar `href` resolves to a built page | automated: test parses a built page's sidebar `<nav>`, asserts group labels + ordered links + each href maps to a file in `dist` | *(empty)* |
| 3 | Internal cross-page links are base-safe (no 404s) | Every internal link in the 7 pages points under `/building-a-software-pack/` and resolves to an emitted file; zero internal links resolve to a path missing from `dist` | automated: test extracts `<a href>` from built pages, filters internal, asserts each target exists in `dist` | *(empty)* |
| 4 | Nebari branding is applied via `@nebari/starlight` | A built page's CSS resolves `--sl-color-text-accent` to Nebari magenta (oklch hue ~311, not Starlight blue ~264); Poppins is the heading font; the Nebari footer is present; the header logo links to `https://packs.nebari.dev/` | automated: reuse the theme's light-mode-accent assertion against a built page + assert `SiteTitle` anchor href | *(empty)* |
| 5 | Search works across the guide | After build, a Pagefind bundle exists at `dist/pagefind/`; a query for a term unique to one page (e.g. "NebariApp") returns that page in the Pagefind index | automated: build, then a test that loads the Pagefind index and asserts a known term maps to the expected page | *(empty)* |
| 6 | "Edit this page" links point to the correct GitHub source | A built page's edit link resolves to the file's real path in `nebari-dev/nebari-software-pack-template` on the default branch | automated: test asserts the edit-link href matches `github.com/nebari-dev/nebari-software-pack-template/edit/main/docs/site/src/content/docs/<file>` for a sample page | *(empty)* |
| 7 | A maintainer's PR gets a working preview deploy | Opening a PR that touches `docs/site/**` triggers `docs.yml`, which builds the Astro site with a root base (`BASE_PATH=/`) and deploys to Cloudflare Pages; the PR comment links to the deployment alias root (`<alias>.pages.dev/`); visiting that URL shows the migrated site with working nav | narrated: open a test PR, capture the Actions run log + the PR comment + a screenshot of the loaded preview URL | *(empty)* |
| 8 | Production serves at `packs.nebari.dev/building-a-software-pack/` on merge to main | After merge, `docs.yml` deploys to the production Cloudflare branch; the live URL returns the migrated Starlight home and a spot-checked inner page | narrated: capture the post-merge Actions run + `curl`/screenshot of the live home and one inner page | *(empty)* |

## Testing strategy

- **Automated (bun test):** journeys 1-6 are deterministic post-build checks over `dist/` (glob emitted pages, parse sidebar, resolve internal links, assert theme CSS + logo href, load the Pagefind index, assert edit-link shape). The rehype base-rewrite plugin gets a focused unit test (internal absolute -> prefixed; external/anchor/protocol-relative left untouched; no double slashes).
- **Narrated:** journeys 7-8 require the real CI + Cloudflare deploy; captured at the verification gate (test PR for 7; post-merge for 8).
- Local dev sanity: `bun run dev` serves under `/building-a-software-pack/`; `bun run build && bun run preview` for the automated checks.

## Out of scope / follow-ups

- Registering this guide's Pagefind bundle in the dashboard's `mergeIndex` (dashboard-repo follow-up).
- `starlight-versions` (versioned-by-release docs) - a later sub-project once the base migration is proven on this pilot.
- Generalizing this into a reusable scaffold/`create-` template for other packs (this migration is the reference; extraction comes after).
- Bumping the remaining Node-20 actions across other workflows.

## Risks

- **Virtual-module API for `logoHref`:** if the intended Astro plugin API differs, fall back to a `vite.define` build-time constant. Either way the value is resolved at build; verified against docs before coding.
- **Helm `{{ }}` in code fences:** mitigated by staying on `.md` (not `.mdx`); a build smoke over the two files with Helm blocks confirms no parse errors.
- **Base mismatch between environments:** mitigated by the dynamic base (subpath in prod where the Worker strips it, root in previews where there is no Worker) and by building tests with the production base so they exercise the deployed config.
- **Cross-repo sequencing:** `@nebari/starlight v0.1.6` must publish before the template can pin it; the plan sequences A before C.

## References (verified 2026-07-01)

- Astro config reference - `base`, `import.meta.env.BASE_URL`, trailing-slash interaction: https://docs.astro.build/en/reference/configuration-reference/
- Astro Markdown guide (no base link-rewriting documented): https://docs.astro.build/en/guides/markdown-content/
- Starlight authoring content (root-relative link example): https://starlight.astro.build/guides/authoring-content/
- Starlight pages guide: https://starlight.astro.build/guides/pages/
- EffVer: https://jacobtomlinson.dev/effver/
