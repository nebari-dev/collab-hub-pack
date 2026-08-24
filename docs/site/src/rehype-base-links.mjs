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
