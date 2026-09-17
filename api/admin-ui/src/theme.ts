/**
 * Light or dark, chosen by the viewer, defaulting to their system setting.
 *
 * Three states rather than two, even though the control is a single toggle:
 * light, dark, and *nothing chosen yet*. Until someone picks, the panel follows
 * the operating system, which is what the server-rendered pages beside it do.
 * The moment they pick, that choice wins and is remembered.
 *
 * The choice lives in `localStorage`, which is per-browser and never reaches
 * the server. Every access is wrapped: a private window or blocked site data
 * throws on access rather than returning null, and a colour scheme is never
 * worth failing a render over.
 */

export type Theme = "light" | "dark";

export const THEME_KEY = "collab-admin-theme";

function isTheme(value: unknown): value is Theme {
  return value === "light" || value === "dark";
}

export function readTheme(store: Storage, systemPrefersDark: boolean): Theme {
  let stored: string | null = null;
  try {
    stored = store.getItem(THEME_KEY);
  } catch {
    stored = null;
  }
  if (isTheme(stored)) return stored;
  return systemPrefersDark ? "dark" : "light";
}

export function storeTheme(store: Storage, theme: Theme): void {
  try {
    store.setItem(THEME_KEY, theme);
  } catch {
    // The toggle still works for this page view; it just will not be
    // remembered. Refusing to switch would be the worse failure.
  }
}

export function nextTheme(theme: Theme): Theme {
  return theme === "dark" ? "light" : "dark";
}

/**
 * Put the choice where CSS can see it.
 *
 * `data-theme` on the root element, plus `color-scheme` so the browser's own
 * surfaces -- scrollbars, form controls, the canvas behind the page -- match
 * the rest rather than staying stubbornly light.
 */
export function applyTheme(root: HTMLElement, theme: Theme): void {
  root.dataset.theme = theme;
  root.style.colorScheme = theme;
}

export function systemPrefersDark(): boolean {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-color-scheme: dark)").matches === true;
}
