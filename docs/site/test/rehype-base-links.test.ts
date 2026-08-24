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
