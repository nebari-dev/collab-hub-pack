import { defineConfig } from 'astro/config';
import { unified } from '@astrojs/markdown-remark';
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
  // content, so this rehype pass does it for internal links and images. Astro 7
  // deprecated the top-level `markdown.rehypePlugins` field in favor of passing the
  // pipeline to `unified()` from @astrojs/markdown-remark (GFM + SmartyPants stay on
  // by default).
  markdown: { processor: unified({ rehypePlugins: [[rehypeBaseLinks, { base }]] }) },
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
