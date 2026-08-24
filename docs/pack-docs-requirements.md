# Official Pack Docs Site Requirements

Every official software pack publishes its documentation as a section of
https://packs.nebari.dev. This document defines what a compliant pack docs
site looks like. A compliant site is a GA requirement in the
[release readiness checklist](release-readiness-checklist.md); starting one
earlier (alpha or beta) is encouraged.

The working reference implementation is the LLM Serving Pack:
https://packs.nebari.dev/llm-serving-pack/.

## Stack

- **Astro** with the shared **`@nebari/starlight`** theme from
  https://github.com/nebari-dev/starlight. The shared theme is what makes
  every pack's docs look and navigate the same; do not substitute another
  Starlight skin or roll custom CSS on top of it.
- Site source lives in the pack repo (conventionally `docs/site/` or
  `docs/astro/`), versioned with the pack. Docs changes ride the same PRs
  and reviews as the code they describe.
- Astro `base` must be `/<slug>/` where `<slug>` is the repository name
  (for example `/llm-serving-pack/`). The site is served behind a path
  prefix on packs.nebari.dev, so root-relative links break without it.

## Layout

Two sidebar sections, in this order, with these baseline pages:

**Documentation**
- Quickstart: the shortest honest path from nothing to the pack doing its
  job, on a current NIC dev cluster.
- Installation: prerequisites (NIC version, namespace opt-in labels,
  sizing, external dependencies), chart install from the published Helm
  registry, NebariApp integration, auth configuration.
- Local Development: how a contributor runs the pack outside a cluster.
- Troubleshooting: real failure modes with symptoms and fixes, not
  hypotheticals.

**Reference**
- Configuration: chart values and application settings.
- Architecture: components, data flow, dependencies.
- CI/CD and Releasing: how the pack tests and cuts releases.

Packs may add pages and sections beyond these; the baseline pages must
exist and carry real content. A GitHub link to the pack repo appears in
the header. Search (Pagefind, which Starlight provides by default) and
the dark/light theme toggle stay enabled: the packs.nebari.dev router
expects each pack site to serve its own `/<slug>/pagefind/` index.

## Publication

Publishing is decentralized: each pack deploys its own site, and the
dashboard routes to it. Three pieces make a pack's docs live:

1. **Cloudflare Pages project named exactly `<slug>`** (the repository
   name). The site must be reachable at `https://<slug>.pages.dev`.
   Deploys run automatically on merge to the pack's default branch,
   either through the Pages GitHub integration or a workflow using
   wrangler.
2. **`docs_site: true` in the pack's `pack-metadata.yaml`.** This is the
   opt-in flag the dashboard reads.
3. **The pack is listed in `tracked-packs.yaml`** in
   [software-pack-dashboard](https://github.com/nebari-dev/software-pack-dashboard).
   The dashboard's `generate_routes.py` then emits a route so
   `https://packs.nebari.dev/<slug>/` proxies to the Pages project. No
   dashboard code changes are needed; route regeneration picks the flag up.

## Content expectations

The site is the durable home for the checklist's documentation items. At
minimum, the content that satisfies the alpha and beta Documentation rows
(prerequisites, known limitations, auth setup, troubleshooting, values
that need customization) belongs on the site by GA, not only in the
README. The README stays the short front door: what the pack does, who it
is for, the deploy command, and a link to the docs site.

## Compliance summary

A docs site is compliant when all of these hold:

- [ ] Astro + `@nebari/starlight`, source in the pack repo, `base: /<slug>/`
- [ ] Baseline layout: Documentation (Quickstart, Installation, Local
      Development, Troubleshooting) and Reference (Configuration,
      Architecture, CI/CD and Releasing)
- [ ] Search and theme toggle enabled; GitHub link in the header
- [ ] Cloudflare Pages project `<slug>`, auto-deploying on merge to the
      default branch
- [ ] `docs_site: true` in `pack-metadata.yaml`; pack listed in
      `tracked-packs.yaml`; site reachable at
      `https://packs.nebari.dev/<slug>/`
- [ ] Checklist documentation content lives on the site by GA
