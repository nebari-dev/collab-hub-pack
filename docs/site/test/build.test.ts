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

// Journey 4
test('Nebari branding: magenta accent, Space Grotesk headings, footer, portal logo link', () => {
  const home = readPage('');
  // Logo returns users to the portal (the logoHref option from Part 1).
  // NOTE: match `nbr-site-title\b` (word boundary), NOT `nbr-site-title"` - Astro appends a
  // scoped-style hash class, so the rendered attribute is `class="nbr-site-title astro-XXXX"`.
  expect(home).toMatch(/<a[^>]*href="https:\/\/packs\.nebari\.dev\/"[^>]*class="nbr-site-title\b/);
  // Branded footer marker.
  expect(home).toContain('data-nebari-footer');
  const css = allCss();
  // Accent mapped onto the Nebari brand token (a magenta-violet derived from
  // --nbr-primary; matches the theme's own assertion in v0.2.0's theme.css).
  expect(css).toMatch(/--sl-color-accent:\s*var\(--nbr-brand\)/);
  // Heading font is Space Grotesk (v0.2.0 typography; Lora for titles, Inter for body).
  expect(css).toMatch(/--nbr-font-heading:\s*["']?Space Grotesk/);
});

// Journey 5
test('Pagefind search bundle is emitted and the unique term is indexable', () => {
  const pf = join(DIST, 'pagefind'); // bundle sits at dist root, like every other page
  expect(existsSync(join(pf, 'pagefind.js'))).toBe(true);
  expect(existsSync(join(pf, 'pagefind-entry.json'))).toBe(true);
  // "NebariApp" is present in the indexed body of the CRD reference page.
  expect(readPage('nebariapp-crd-reference')).toContain('NebariApp');
});

// Journey 6
test('edit links point to the correct GitHub source file', () => {
  const html = readPage('auth-flow');
  expect(html).toContain(
    'https://github.com/nebari-dev/nebari-software-pack-template/edit/main/docs/site/src/content/docs/auth-flow.md',
  );
});
