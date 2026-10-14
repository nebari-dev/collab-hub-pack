import { describe, expect, it } from "vitest";

import { offences } from "./check-bundles.mjs";

const files = new Set(["assets/index-abc.js", "assets/index-abc.css"]);
const exists = (path: string) => files.has(path);

describe("offences", () => {
  it("passes the shape Vite emits: one module script and one stylesheet, both under ./assets/", () => {
    const html =
      '<script type="module" crossorigin src="./assets/index-abc.js"></script>' +
      '<link rel="stylesheet" crossorigin href="./assets/index-abc.css"><div id="root"></div>';
    expect(offences(html, exists)).toEqual([]);
  });

  it("names an inline script, which the page's script-src 'self' would refuse", () => {
    expect(offences("<script>window.x = 1</script>", exists)).toEqual([
      "an inline <script> (no src), which script-src 'self' refuses",
    ]);
  });

  it("names a reference outside ./assets/ and one to a file the build did not write", () => {
    const html =
      '<script type="module" src="https://cdn.example.com/x.js"></script>' +
      '<link rel="stylesheet" href="./assets/missing.css">';
    expect(offences(html, exists)).toEqual([
      "https://cdn.example.com/x.js is outside ./assets/",
      "./assets/missing.css is not in the build",
    ]);
  });

  it("names an inline style attribute and a <style> element, which style-src 'self' refuses", () => {
    expect(offences('<div style="color:red"></div><style>p{}</style>', exists)).toEqual([
      "a style attribute, which style-src 'self' refuses",
      "an inline <style>, which style-src 'self' refuses",
    ]);
  });
});
