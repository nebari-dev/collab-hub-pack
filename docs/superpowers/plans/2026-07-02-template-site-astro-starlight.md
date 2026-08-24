# "Building a Software Pack" Astro+Starlight Migration - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replatform the guide at `packs.nebari.dev/building-a-software-pack/` from Hugo to Astro + Starlight themed with `@nebari/starlight`, preserving PR-preview deploys and adding built-in search.

**Architecture:** Two parts in two repos. Part 1 (small) adds an opt-in `logoHref` to `@nebari/starlight` so the header logo can return users to the portal, released as v0.1.6. Part 2 replaces the Hugo project rooted at `docs/site/` with an Astro + Starlight project at the same path (static `base`, `@nebari/starlight ^0.1.6`, a rehype pass for base-safe Markdown links), and swaps the CI workflow from Hugo/Go to Bun/Astro.

**Tech Stack:** Astro 5, Starlight 0.33, `@nebari/starlight`, Bun (package manager + `bun:test`), Cloudflare Pages, GitHub Actions.

## Global Constraints

Every task's requirements implicitly include these:

- **Astro `base` is dynamic: `process.env.BASE_PATH ?? '/building-a-software-pack/'`.** Production (`main`) uses the subpath (the portal Worker strips `/building-a-software-pack/` before proxying to this Pages project); PR previews build with `BASE_PATH=/` (served at `<alias>.pages.dev/` root, no Worker). Astro emits files at `dist/` **root** in both - `base` only prefixes link/asset URLs, it does not nest output. Default (unset) is the production subpath, so local builds and tests match production.
- **Content files stay `.md`, never `.mdx`.** `concepts.md` and `nebariapp-crd-reference.md` contain Helm `{{ ... }}` template syntax in fenced code blocks; MDX would parse `{` as JSX and break the build.
- **`@nebari/starlight` is pinned `^0.1.6`** in Part 2. Part 2 cannot `bun install` until Part 1's v0.1.6 is published to npm (see the Release Gate).
- Peer/runtime version floors: `astro >=5.0.0 <6.0.0`, `@astrojs/starlight >=0.33.0 <1.0.0`.
- Bun is the package manager and test runner. Tests are `bun:test`.
- **No em dashes (`-`) in any authored copy or comments.** Use hyphens, colons, or rewrite.
- **Commits carry no AI attribution and no "co-authored-by".** Plain conventional-commit messages.

## Journeys

Copied from the spec (`docs/superpowers/specs/2026-07-01-template-site-astro-starlight-design.md`). Evidence columns are filled only at the verification gate. A **reader** browses the guide at `packs.nebari.dev/building-a-software-pack/`; a **maintainer** edits the docs and opens a PR. **Revised 2026-07-02** (items 1, 5, 7) for the dynamic-base correction: Astro emits files at `dist/` root (not nested under the base); the Worker strips the prefix in production and previews build with a root base.

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

**Journey check-method note (item 5):** a literal "load the Pagefind binary index and resolve a term to a page" is not feasible in pure Node (fragments/indexes are compressed). The automated task below implements the strongest deterministic proxy: assert the Pagefind bundle is emitted under the base **and** the unique term is present in the indexed body of the expected page. The interactive query is exercised by the shared theme's own Playwright search e2e. This refinement is flagged to the user at the verification gate.

---

# Part 1 - `@nebari/starlight`: add `logoHref` (repo: `/home/chuck/devel/starlight`)

### Task 1: Add an opt-in `logoHref` threaded through a Vite virtual module

**Repo / cwd:** `/home/chuck/devel/starlight`

**Files:**
- Modify: `packages/starlight/src/index.ts`
- Create: `packages/starlight/src/virtual.d.ts`
- Modify: `packages/starlight/src/components/SiteTitle.astro`
- Modify: `packages/starlight/package.json` (add `src/virtual.d.ts` to `files`)
- Modify: `docs/astro.config.mjs` (demo passes `logoHref` so the build test can assert it)
- Test: `packages/starlight/test/build.test.ts` (add one test)

**Interfaces:**
- Produces: `nebari(options?: { logoHref?: string }): StarlightPlugin`. When `logoHref` is set, the header logo anchor's `href` is that value; when unset it stays `import.meta.env.BASE_URL` (unchanged for existing consumers, e.g. the dashboard).
- Produces: virtual module `virtual:nebari/config` exporting `logoHref: string | null`.

- [ ] **Step 1: Add the failing test** in `packages/starlight/test/build.test.ts` (append):

```ts
test('SiteTitle links the header logo to the configured logoHref', () => {
  const html = allText('.html');
  // The demo config sets logoHref to the Nebari platform home.
  expect(html).toMatch(/<a[^>]*href="https:\/\/nebari\.dev\/"[^>]*class="nbr-site-title"/);
});
```

- [ ] **Step 2: Point the demo at a logoHref** in `docs/astro.config.mjs`:

```js
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import { nebari } from '@nebari/starlight';

export default defineConfig({
  integrations: [
    starlight({
      title: 'Nebari Starlight',
      plugins: [nebari({ logoHref: 'https://nebari.dev/' })],
    }),
  ],
});
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `cd /home/chuck/devel/starlight && bun test packages/starlight/test/build.test.ts`
Expected: FAIL - the anchor still renders `href="/"` (BASE_URL), not the configured URL.

- [ ] **Step 4: Implement `logoHref`** - replace `packages/starlight/src/index.ts` with:

```ts
import type { StarlightPlugin } from '@astrojs/starlight/types';
import type { AstroIntegration } from 'astro';

/// <reference path="./virtual.d.ts" />

export interface NebariThemeOptions {
  /**
   * URL the header logo links to. Defaults to the site's own base
   * (`import.meta.env.BASE_URL`). Set it to the portal root so the logo
   * returns users to `packs.nebari.dev/`.
   */
  logoHref?: string;
}

/** Astro integration that exposes the theme config to components via a virtual module. */
function nebariConfigIntegration(logoHref: string | null): AstroIntegration {
  return {
    name: '@nebari/starlight/config',
    hooks: {
      'astro:config:setup'({ updateConfig }) {
        updateConfig({
          vite: {
            plugins: [
              {
                name: '@nebari/starlight/virtual-config',
                resolveId(id: string) {
                  if (id === 'virtual:nebari/config') return '\0virtual:nebari/config';
                  return undefined;
                },
                load(id: string) {
                  if (id === '\0virtual:nebari/config') {
                    return `export const logoHref = ${JSON.stringify(logoHref)};`;
                  }
                  return undefined;
                },
              },
            ],
          },
        });
      },
    },
  };
}

export function nebari(options: NebariThemeOptions = {}): StarlightPlugin {
  const logoHref = options.logoHref ?? null;
  return {
    name: '@nebari/starlight',
    hooks: {
      'config:setup'({ config, updateConfig, addIntegration }) {
        updateConfig({
          customCss: [
            '@nebari/starlight/fonts/font-face.css',
            '@nebari/starlight/styles/nebari-tokens.css',
            '@nebari/starlight/styles/theme.css',
            ...(config.customCss ?? []),
          ],
          components: {
            SiteTitle: '@nebari/starlight/components/SiteTitle.astro',
            Head: '@nebari/starlight/components/Head.astro',
            Footer: '@nebari/starlight/components/Footer.astro',
            ...(config.components ?? {}),
          },
          social: [
            { icon: 'github', label: 'GitHub', href: 'https://github.com/nebari-dev' },
            ...(config.social ?? []),
          ],
        });
        addIntegration(nebariConfigIntegration(logoHref));
      },
    },
  };
}
```

- [ ] **Step 5: Declare the virtual module** - create `packages/starlight/src/virtual.d.ts`:

```ts
declare module 'virtual:nebari/config' {
  export const logoHref: string | null;
}
```

- [ ] **Step 6: Consume it in the component** - update the frontmatter of `packages/starlight/src/components/SiteTitle.astro`:

```astro
---
import logoLight from '../assets/nebari-horizontal-light.svg?url';
import logoDark from '../assets/nebari-horizontal-dark.svg?url';
import { logoHref } from 'virtual:nebari/config';
const href = logoHref ?? import.meta.env.BASE_URL;
---
```

(The `<a href={href} ...>` markup and `<style>` block below are unchanged.)

- [ ] **Step 7: Ship the declaration** - add `"src/virtual.d.ts"` to the `files` array in `packages/starlight/package.json`.

- [ ] **Step 8: Build and run the full theme test suite**

Run: `cd /home/chuck/devel/starlight && bun run build && bun test packages/starlight/test`
Expected: PASS, including the new logoHref test and all prior tests (footer, logo alt, accent mapping, fonts).

- [ ] **Step 9: Commit**

```bash
cd /home/chuck/devel/starlight
git add packages/starlight/src/index.ts packages/starlight/src/virtual.d.ts \
        packages/starlight/src/components/SiteTitle.astro packages/starlight/package.json \
        docs/astro.config.mjs packages/starlight/test/build.test.ts
git commit -m "feat: add optional logoHref to point the header logo at the portal"
```

### Task 2: Version bump to 0.1.6 and prep release

**Repo / cwd:** `/home/chuck/devel/starlight`

**Files:**
- Modify: `packages/starlight/package.json` (`version`)
- Modify: `README.md` (document `logoHref`)

**Advances journeys:** none (rationale: dependency/release plumbing for the theme; no user-visible docs behavior on its own).

- [ ] **Step 1: Bump the version** in `packages/starlight/package.json`: `"version": "0.1.5"` -> `"version": "0.1.6"`.

- [ ] **Step 2: Document the option** - in `README.md`, under "Use", add after the base subpath section:

```md
### Pointing the logo at the portal

By default the header logo links to the site's own base. On the Nebari portal,
point it at the portal root so it returns users to the pack catalog:

```js
starlight({ plugins: [nebari({ logoHref: 'https://packs.nebari.dev/' })] })
```
```

- [ ] **Step 3: Commit**

```bash
cd /home/chuck/devel/starlight
git add packages/starlight/package.json README.md
git commit -m "chore: release v0.1.6 (logoHref option)"
```

---

## Release Gate (human step, between Part 1 and Part 2)

Part 2 pins `@nebari/starlight ^0.1.6`, which must exist on npm before `bun install` there can succeed.

1. Push Part 1 to `main` on `nebari-dev/starlight` (via PR or direct, per repo policy).
2. Cut a GitHub Release tagged `v0.1.6`; the existing `release.yml` publishes to npm via Trusted Publishing (OIDC, provenance).
3. Confirm `npm view @nebari/starlight version` shows `0.1.6`.

**Do not begin Task 3 until v0.1.6 is live on npm.** This gate is an outward-facing publish and requires explicit user go-ahead.

---

# Part 2 - Template migration (repo: `/home/chuck/devel/nebari-software-pack-template`, branch `feat/astro-starlight-docs`)

### Task 3: Scaffold the Astro+Starlight site, migrate content, configure the sidebar

**Repo / cwd:** `/home/chuck/devel/nebari-software-pack-template`

**Files:**
- Create: `docs/site/package.json`, `docs/site/tsconfig.json`, `docs/site/.gitignore`
- Create: `docs/site/astro.config.mjs`
- Create: `docs/site/src/content.config.ts`
- Create: `docs/site/src/rehype-base-links.mjs` (stub in this task; tested in Task 4)
- Move + edit frontmatter: `docs/site/content/*.md` -> `docs/site/src/content/docs/*.md`
- Delete: `docs/site/hugo.toml`, `docs/site/go.mod`, `docs/site/go.sum`
- Test: `docs/site/test/build.test.ts`

**Interfaces:**
- Produces: a buildable Astro site; `bun run build` (cwd `docs/site`) emits pages at `docs/site/dist/` root (`dist/index.html`, `dist/<slug>/index.html`), with link/asset URLs prefixed by the base.
- Produces: `rehypeBaseLinks({ base })` export (implemented fully in Task 4; a pass-through stub here so the config imports resolve).

**Advances journeys:** 1, 2

- [ ] **Step 1: Create `docs/site/package.json`:**

```json
{
  "name": "building-a-software-pack-docs",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "astro dev",
    "build": "astro build",
    "preview": "astro preview",
    "test": "bun test test"
  },
  "dependencies": {
    "@astrojs/starlight": "^0.33.0",
    "@nebari/starlight": "^0.1.6",
    "astro": "^5.0.0",
    "unist-util-visit": "^5.0.0"
  }
}
```

- [ ] **Step 2: Create `docs/site/tsconfig.json`:**

```json
{
  "extends": "astro/tsconfigs/strict",
  "include": [".astro/types.d.ts", "**/*"],
  "exclude": ["dist"]
}
```

- [ ] **Step 3: Create `docs/site/.gitignore`:**

```gitignore
dist/
.astro/
node_modules/
```

- [ ] **Step 4: Create `docs/site/src/content.config.ts`:**

```ts
import { defineCollection } from 'astro:content';
import { docsLoader } from '@astrojs/starlight/loaders';
import { docsSchema } from '@astrojs/starlight/schema';

export const collections = {
  docs: defineCollection({ loader: docsLoader(), schema: docsSchema() }),
};
```

- [ ] **Step 5: Create the rehype stub** `docs/site/src/rehype-base-links.mjs` (full implementation in Task 4):

```js
// Prefixes internal root-absolute Markdown links/images with the Astro base.
// Fully implemented and tested in Task 4.
export function rehypeBaseLinks({ base }) {
  return () => {};
}
```

- [ ] **Step 6: Create `docs/site/astro.config.mjs`:**

```js
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import { nebari } from '@nebari/starlight';
import { rehypeBaseLinks } from './src/rehype-base-links.mjs';

// Dynamic base. Production (main) uses the subpath: the portal Worker strips
// /building-a-software-pack/ before proxying to this Pages project, so files are served
// from its root. PR previews build with BASE_PATH=/ because they are served at
// <alias>.pages.dev/ directly (no Worker). Astro emits files at dist/ root either way;
// base only prefixes link/asset URLs. Default is the production subpath so local
// builds and tests match production.
const base = process.env.BASE_PATH ?? '/building-a-software-pack/';

export default defineConfig({
  site: 'https://packs.nebari.dev',
  base,
  // Astro does not prefix `base` onto root-absolute links written in Markdown body
  // content, so this rehype pass does it for internal links and images.
  markdown: { rehypePlugins: [[rehypeBaseLinks, { base }]] },
  integrations: [
    starlight({
      title: 'Building a Software Pack',
      description:
        'A deep-dive guide to building, deploying, and maintaining Nebari Software Packs - Kubernetes applications with routing, TLS, and OIDC wired in.',
      plugins: [nebari({ logoHref: 'https://packs.nebari.dev/' })],
      editLink: {
        // Starlight appends the source path (src/content/docs/<file>.md) to this base,
        // so it must point at the Astro project root inside the repo.
        baseUrl: 'https://github.com/nebari-dev/nebari-software-pack-template/edit/main/docs/site/',
      },
      sidebar: [
        {
          label: 'Getting Started',
          items: [
            { label: 'Introduction', link: '/' },
            { label: 'What is a software pack', link: '/what-is-a-software-pack/' },
            { label: 'Concepts', link: '/concepts/' },
            { label: 'Build your own', link: '/build-your-own/' },
          ],
        },
        {
          label: 'Reference',
          items: [
            { label: 'NebariApp CRD', link: '/nebariapp-crd-reference/' },
            { label: 'Authentication Flow', link: '/auth-flow/' },
            { label: 'Release Readiness', link: '/release-readiness/' },
          ],
        },
      ],
    }),
  ],
});
```

- [ ] **Step 7: Move content and convert frontmatter.** Move each file with `git mv`, then replace **only** the `+++ ... +++` TOML frontmatter block with the YAML block shown; leave every file's body (prose, code fences, root-absolute links) unchanged.

```bash
cd /home/chuck/devel/nebari-software-pack-template/docs/site
mkdir -p src/content/docs
git mv content/_index.md                  src/content/docs/index.md
git mv content/what-is-a-software-pack.md  src/content/docs/what-is-a-software-pack.md
git mv content/concepts.md                 src/content/docs/concepts.md
git mv content/build-your-own.md           src/content/docs/build-your-own.md
git mv content/nebariapp-crd-reference.md  src/content/docs/nebariapp-crd-reference.md
git mv content/auth-flow.md                src/content/docs/auth-flow.md
git mv content/release-readiness.md        src/content/docs/release-readiness.md
```

Then set each file's frontmatter to exactly:

- `src/content/docs/index.md`:
```yaml
---
title: Building a Software Pack
---
```
- `src/content/docs/what-is-a-software-pack.md`:
```yaml
---
title: What is a software pack
---
```
- `src/content/docs/concepts.md`:
```yaml
---
title: Concepts
---
```
- `src/content/docs/build-your-own.md`:
```yaml
---
title: Build your own pack
---
```
- `src/content/docs/nebariapp-crd-reference.md`:
```yaml
---
title: NebariApp CRD Reference
---
```
- `src/content/docs/auth-flow.md`:
```yaml
---
title: Authentication Flow
---
```
- `src/content/docs/release-readiness.md`:
```yaml
---
title: Release Readiness
---
```

- [ ] **Step 8: Remove the Hugo project files:**

```bash
cd /home/chuck/devel/nebari-software-pack-template/docs/site
git rm hugo.toml go.mod go.sum
```

- [ ] **Step 9: Install dependencies** (requires v0.1.6 on npm - see the Release Gate):

Run: `cd /home/chuck/devel/nebari-software-pack-template/docs/site && bun install`
Expected: resolves `@nebari/starlight@0.1.6`, writes `bun.lock`.

- [ ] **Step 10: Write the journey-1 and journey-2 tests** - create `docs/site/test/build.test.ts`:

```ts
import { test, expect, beforeAll, setDefaultTimeout } from 'bun:test';
import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { $ } from 'bun';

setDefaultTimeout(180_000);

const SITE = join(import.meta.dir, '..');
const DIST = join(SITE, 'dist');
const BASE = '/building-a-software-pack'; // production base (URL prefix), no trailing slash

const TITLES: Record<string, string> = {
  '': 'Building a Software Pack',
  'what-is-a-software-pack': 'What is a software pack',
  concepts: 'Concepts',
  'build-your-own': 'Build your own pack',
  'nebariapp-crd-reference': 'NebariApp CRD Reference',
  'auth-flow': 'Authentication Flow',
  'release-readiness': 'Release Readiness',
};

// Astro emits files at dist/ root (base only prefixes URLs, it does not nest output).
function pagePath(slug: string): string {
  return slug === '' ? join(DIST, 'index.html') : join(DIST, slug, 'index.html');
}
function readPage(slug: string): string {
  return readFileSync(pagePath(slug), 'utf8');
}
function allHtmlFiles(): string[] {
  const out: string[] = [];
  const walk = (dir: string) => {
    for (const e of readdirSync(dir, { withFileTypes: true })) {
      const p = join(dir, e.name);
      if (e.isDirectory()) walk(p);
      else if (e.name.endsWith('.html')) out.push(p);
    }
  };
  walk(DIST);
  return out;
}
function allCss(): string {
  const out: string[] = [];
  const walk = (dir: string) => {
    for (const e of readdirSync(dir, { withFileTypes: true })) {
      const p = join(dir, e.name);
      if (e.isDirectory()) walk(p);
      else if (e.name.endsWith('.css')) out.push(readFileSync(p, 'utf8'));
    }
  };
  walk(DIST);
  return out.join('\n');
}

beforeAll(async () => {
  await $`bun run build`.cwd(SITE);
});

// Journey 1
test('all 7 pages render at dist root with titles and base-prefixed links', () => {
  for (const [slug, title] of Object.entries(TITLES)) {
    expect(existsSync(pagePath(slug))).toBe(true);
    expect(readPage(slug)).toContain(title);
  }
  // Nav links carry the production base prefix (Starlight prepends base to nav).
  expect(readPage('')).toContain(`href="${BASE}/`);
});

// Journey 2
test('sidebar has both groups with Concepts in the Getting Started slot', () => {
  const html = readPage('what-is-a-software-pack');
  expect(html).toContain('Getting Started');
  expect(html).toContain('Reference');
  // All seven sidebar hrefs resolve to a built page.
  const links = [
    '', 'what-is-a-software-pack', 'concepts', 'build-your-own',
    'nebariapp-crd-reference', 'auth-flow', 'release-readiness',
  ];
  for (const slug of links) {
    const href = slug === '' ? `${BASE}/` : `${BASE}/${slug}/`;
    expect(html).toContain(`href="${href}"`);
    expect(existsSync(pagePath(slug))).toBe(true);
  }
  // Order within Getting Started: What is a software pack -> Concepts -> Build your own.
  const idxWhat = html.indexOf(`href="${BASE}/what-is-a-software-pack/"`);
  const idxConcepts = html.indexOf(`href="${BASE}/concepts/"`);
  const idxBuild = html.indexOf(`href="${BASE}/build-your-own/"`);
  expect(idxConcepts).toBeGreaterThan(idxWhat);
  expect(idxBuild).toBeGreaterThan(idxConcepts);
});
```

- [ ] **Step 11: Build and run the tests**

Run: `cd /home/chuck/devel/nebari-software-pack-template/docs/site && bun test test/build.test.ts`
Expected: the two tests PASS. (`bun test` runs the build once in `beforeAll`.)

- [ ] **Step 12: Commit**

```bash
cd /home/chuck/devel/nebari-software-pack-template
git add docs/site/
git commit -m "feat: migrate the guide site to Astro + Starlight"
```

### Task 4: Base-safe internal links (rehype plugin)

**Repo / cwd:** `/home/chuck/devel/nebari-software-pack-template`

**Files:**
- Modify: `docs/site/src/rehype-base-links.mjs` (replace the stub)
- Test: `docs/site/test/rehype-base-links.test.ts` (unit), `docs/site/test/build.test.ts` (append the dist link-resolution test)

**Interfaces:**
- Consumes: `base` (`/building-a-software-pack/`) from `astro.config.mjs`.
- Produces: `rehypeBaseLinks({ base })` - a rehype plugin that prefixes internal root-absolute `<a href>` / `<img src>` values with `base`, leaving external, protocol-relative, in-page, and relative links untouched, idempotently.

**Advances journeys:** 3

- [ ] **Step 1: Write the unit tests** - create `docs/site/test/rehype-base-links.test.ts`:

```ts
import { test, expect } from 'bun:test';
import { rehypeBaseLinks } from '../src/rehype-base-links.mjs';

function el(tagName, properties) {
  return { type: 'element', tagName, properties, children: [] };
}
function run(children) {
  const tree = { type: 'root', children };
  rehypeBaseLinks({ base: '/building-a-software-pack/' })(tree);
  return tree.children;
}

test('prefixes internal root-absolute anchor hrefs', () => {
  const [a] = run([el('a', { href: '/auth-flow/' })]);
  expect(a.properties.href).toBe('/building-a-software-pack/auth-flow/');
});

test('preserves anchors on internal links', () => {
  const [a] = run([el('a', { href: '/auth-flow/#app-native-oauth' })]);
  expect(a.properties.href).toBe('/building-a-software-pack/auth-flow/#app-native-oauth');
});

test('prefixes internal image sources', () => {
  const [img] = run([el('img', { src: '/img/diagram.png' })]);
  expect(img.properties.src).toBe('/building-a-software-pack/img/diagram.png');
});

test('leaves external, protocol-relative, in-page, and relative links untouched', () => {
  const [ext, proto, hash, rel] = run([
    el('a', { href: 'https://github.com/nebari-dev' }),
    el('a', { href: '//cdn.example.com/x' }),
    el('a', { href: '#section' }),
    el('a', { href: 'sibling/page/' }),
  ]);
  expect(ext.properties.href).toBe('https://github.com/nebari-dev');
  expect(proto.properties.href).toBe('//cdn.example.com/x');
  expect(hash.properties.href).toBe('#section');
  expect(rel.properties.href).toBe('sibling/page/');
});

test('is idempotent (does not double-prefix)', () => {
  const [a] = run([el('a', { href: '/building-a-software-pack/auth-flow/' })]);
  expect(a.properties.href).toBe('/building-a-software-pack/auth-flow/');
});
```

- [ ] **Step 2: Run the unit tests to verify they fail**

Run: `cd /home/chuck/devel/nebari-software-pack-template/docs/site && bun test test/rehype-base-links.test.ts`
Expected: FAIL - the stub does nothing, so hrefs are unchanged.

- [ ] **Step 3: Implement the plugin** - replace `docs/site/src/rehype-base-links.mjs`:

```js
import { visit } from 'unist-util-visit';

const LINK_ATTR = { a: 'href', img: 'src' };

/**
 * Prefix internal root-absolute links and image sources with the Astro `base`.
 * Astro does not do this for links written in Markdown body content, so under a
 * subpath deployment a bare `/foo/` link would 404. External (`http(s):`),
 * protocol-relative (`//`), in-page (`#`), and relative links are left alone.
 */
export function rehypeBaseLinks({ base }) {
  const prefix = base.endsWith('/') ? base.slice(0, -1) : base;
  return (tree) => {
    visit(tree, 'element', (node) => {
      const attr = LINK_ATTR[node.tagName];
      if (!attr) return;
      const value = node.properties?.[attr];
      if (typeof value !== 'string') return;
      if (!value.startsWith('/') || value.startsWith('//')) return;
      if (value === prefix || value.startsWith(prefix + '/')) return;
      node.properties[attr] = prefix + value;
    });
  };
}
```

- [ ] **Step 4: Run the unit tests to verify they pass**

Run: `cd /home/chuck/devel/nebari-software-pack-template/docs/site && bun test test/rehype-base-links.test.ts`
Expected: PASS (all 5).

- [ ] **Step 5: Append the journey-3 dist test** to `docs/site/test/build.test.ts`:

```ts
// Journey 3
test('every internal link is base-prefixed and resolves to a file at dist root', () => {
  const hrefRe = /(?:href|src)="([^"]+)"/g;
  for (const file of allHtmlFiles()) {
    const html = readFileSync(file, 'utf8');
    let m: RegExpExecArray | null;
    while ((m = hrefRe.exec(html)) !== null) {
      const url = m[1];
      if (!url.startsWith('/') || url.startsWith('//')) continue; // external / protocol-relative
      // Internal links must carry the production base prefix.
      expect(url === BASE || url.startsWith(`${BASE}/`)).toBe(true);
      // Strip the base prefix (the Worker does this in prod), then resolve at dist root.
      const afterBase = url.slice(BASE.length); // '' | '/auth-flow/' | '/_astro/x.css'
      const clean = afterBase.split('#')[0].split('?')[0].replace(/\/$/, '');
      const rel = clean.replace(/^\//, '');
      const asIndex = rel === '' ? join(DIST, 'index.html') : join(DIST, rel, 'index.html');
      const asFile = join(DIST, rel);
      expect(existsSync(asIndex) || existsSync(asFile)).toBe(true);
    }
  }
});
```

- [ ] **Step 6: Run the full build test suite**

Run: `cd /home/chuck/devel/nebari-software-pack-template/docs/site && bun test`
Expected: PASS - build tests (journeys 1-3) and the rehype unit tests.

- [ ] **Step 7: Commit**

```bash
cd /home/chuck/devel/nebari-software-pack-template
git add docs/site/src/rehype-base-links.mjs docs/site/test/
git commit -m "feat: make Markdown body links base-safe via a rehype pass"
```

### Task 5: Verify branding, search, and edit links

**Repo / cwd:** `/home/chuck/devel/nebari-software-pack-template`

**Files:**
- Test: `docs/site/test/build.test.ts` (append three tests)

**Interfaces:**
- Consumes: the built `dist/` from the `beforeAll` build; the `allCss()` / `readPage()` helpers from Task 3.

**Advances journeys:** 4, 5, 6

- [ ] **Step 1: Append the journey-4 branding test** to `docs/site/test/build.test.ts`:

```ts
// Journey 4
test('Nebari branding: magenta accent, Poppins, footer, portal logo link', () => {
  const home = readPage('');
  // Logo returns users to the portal (the logoHref option from Part 1).
  // NOTE: match `nbr-site-title\b` (word boundary), NOT `nbr-site-title"` - Astro appends a
  // scoped-style hash class, so the rendered attribute is `class="nbr-site-title astro-XXXX"`.
  expect(home).toMatch(/<a[^>]*href="https:\/\/packs\.nebari\.dev\/"[^>]*class="nbr-site-title\b/);
  // Branded footer marker.
  expect(home).toContain('data-nebari-footer');
  const css = allCss();
  // Accent mapped onto the Nebari primary token (matches the theme's own assertion).
  expect(css).toMatch(/--sl-color-accent:\s*var\(--nbr-primary\)/);
  // Heading font is Poppins.
  expect(css).toMatch(/Poppins/);
});
```

- [ ] **Step 2: Append the journey-5 search test:**

```ts
// Journey 5
test('Pagefind search bundle is emitted and the unique term is indexable', () => {
  const pf = join(DIST, 'pagefind'); // bundle sits at dist root, like every other page
  expect(existsSync(join(pf, 'pagefind.js'))).toBe(true);
  expect(existsSync(join(pf, 'pagefind-entry.json'))).toBe(true);
  // "NebariApp" is present in the indexed body of the CRD reference page.
  expect(readPage('nebariapp-crd-reference')).toContain('NebariApp');
});
```

- [ ] **Step 3: Append the journey-6 edit-link test:**

```ts
// Journey 6
test('edit links point to the correct GitHub source file', () => {
  const html = readPage('auth-flow');
  expect(html).toContain(
    'https://github.com/nebari-dev/nebari-software-pack-template/edit/main/docs/site/src/content/docs/auth-flow.md',
  );
});
```

- [ ] **Step 4: Run the full test suite**

Run: `cd /home/chuck/devel/nebari-software-pack-template/docs/site && bun test`
Expected: PASS - journeys 1-6 covered by automated tests plus the rehype unit tests.

- [ ] **Step 5: Commit**

```bash
cd /home/chuck/devel/nebari-software-pack-template
git add docs/site/test/build.test.ts
git commit -m "test: verify branding, search bundle, and edit links"
```

### Task 6: Swap the CI workflow from Hugo/Go to Bun/Astro

**Repo / cwd:** `/home/chuck/devel/nebari-software-pack-template`

**Files:**
- Modify: `.github/workflows/docs.yml`

**Interfaces:**
- Consumes: `docs/site/` Astro project, build output `docs/site/dist`.
- Produces: a workflow that computes `BASE_PATH` per environment, builds with Bun/Astro, deploys `docs/site/dist` to the same Cloudflare project, and comments the deployment alias root URL.

**Advances journeys:** 7, 8

- [ ] **Step 1: Replace `.github/workflows/docs.yml`:**

```yaml
name: Docs
on:
  push:
    branches: [main]
    paths: ['docs/site/**', '.github/workflows/docs.yml']
  pull_request:
    paths: ['docs/site/**', '.github/workflows/docs.yml']
  workflow_dispatch:
permissions:
  contents: read
  pull-requests: write
concurrency:
  group: docs-${{ github.ref }}
  cancel-in-progress: true
env:
  # Route slug on the portal; also the production Astro base path (/building-a-software-pack/).
  PACK_SLUG: building-a-software-pack
  # CF project name differs from the route slug for the template guide.
  CF_PROJECT: nebari-software-pack-template
jobs:
  docs:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: oven-sh/setup-bun@v2
        with:
          bun-version: latest

      - name: Compute base path and deploy branch
        id: cf
        env:
          GH_REF: ${{ github.ref }}
          GH_HEAD_REF: ${{ github.head_ref || github.ref_name }}
        run: |
          if [ "$GH_REF" = "refs/heads/main" ]; then
            echo "base_path=/${PACK_SLUG}/" >> "$GITHUB_OUTPUT"
            echo "branch=main" >> "$GITHUB_OUTPUT"
          else
            # PR previews serve at the pages.dev alias root (no Worker), so build at root base.
            echo "base_path=/" >> "$GITHUB_OUTPUT"
            echo "branch=${GH_HEAD_REF}" >> "$GITHUB_OUTPUT"
          fi

      - name: Install
        run: cd docs/site && bun install --frozen-lockfile

      - name: Build
        # Dynamic base: subpath on main, root on PR previews (astro.config reads BASE_PATH).
        run: cd docs/site && BASE_PATH="${{ steps.cf.outputs.base_path }}" bun run build

      # Fork PRs cannot read secrets; skip deploy there (build above still gates).
      - name: Deploy to Cloudflare Pages
        id: deploy
        if: ${{ github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository }}
        uses: cloudflare/wrangler-action@v3
        with:
          apiToken: ${{ secrets.CLOUDFLARE_API_TOKEN }}
          accountId: ${{ secrets.CLOUDFLARE_ACCOUNT_ID }}
          command: pages deploy docs/site/dist --project-name=${{ env.CF_PROJECT }} --branch=${{ steps.cf.outputs.branch }}

      - name: Comment preview URL
        if: ${{ github.event_name == 'pull_request' && steps.deploy.outcome == 'success' }}
        uses: thollander/actions-comment-pull-request@v3
        with:
          comment-tag: docs-preview
          message: |
            📄 **Docs preview** for `${{ github.event.pull_request.head.ref }}`:
            ${{ steps.deploy.outputs.pages-deployment-alias-url }}
```

- [ ] **Step 2: Validate the workflow YAML locally**

Run: `cd /home/chuck/devel/nebari-software-pack-template && python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/docs.yml')); print('ok')"`
Expected: `ok`. (If `actionlint` is installed, also run `actionlint .github/workflows/docs.yml`.)

- [ ] **Step 3: Confirm a clean production-parity build**

Run: `cd /home/chuck/devel/nebari-software-pack-template/docs/site && rm -rf dist .astro && bun run build`
Expected: build succeeds (default base = production subpath); `docs/site/dist/index.html` exists and its internal links are prefixed with `/building-a-software-pack/`.

- [ ] **Step 4: Commit**

```bash
cd /home/chuck/devel/nebari-software-pack-template
git add .github/workflows/docs.yml
git commit -m "ci: build docs with Bun/Astro and deploy site/dist"
```

**Journeys 7 and 8 are narrated and verified at the gate:** open a PR (capture the Actions run, the preview-URL comment, and a screenshot of the loaded `<alias>.pages.dev/` root), and after merge capture the production run plus the live `packs.nebari.dev/building-a-software-pack/` home and one inner page.

---

## Self-Review

**Spec coverage:**
- `@nebari/starlight` `logoHref` + release -> Task 1, 2, Release Gate. ✓
- Astro project at `docs/site`, dynamic base (files at dist root), `.md` files, `_index.md`->`index.md`, `+++`->`---` -> Task 3. ✓
- Explicit 2-group sidebar with `concepts` added -> Task 3 (config) + Task 3 Step 10 (test). ✓
- Base-safe links via rehype -> Task 4. ✓
- Branding, search, edit links -> Task 5. ✓
- CI swap Hugo->Bun/Astro, per-env `BASE_PATH`, deploy `dist`, PR comment (alias root) -> Task 6. ✓
- Out of scope (dashboard mergeIndex, starlight-versions, scaffold extraction) -> not planned, correct. ✓
- All 8 journeys map to a task (1,2->T3; 3->T4; 4,5,6->T5; 7,8->T6; 4 enabled by T1). ✓

**Placeholder scan:** the only "stub" is the Task-3 rehype pass-through, explicitly replaced with full code in Task 4. No TBD/TODO. ✓

**Type/name consistency:** `rehypeBaseLinks({ base })` signature identical in stub (T3), impl (T4), and config import (T3). `nebari({ logoHref })` matches `NebariThemeOptions`. `virtual:nebari/config` export name `logoHref` matches the `.d.ts` and the `SiteTitle.astro` import. Test helpers (`readPage`, `pagePath`, `allHtmlFiles`, `allCss`, `BASE`) defined in T3 and reused in T4/T5; `BASE` is the URL prefix `/building-a-software-pack` and files resolve at `dist/` root. ✓
