import { describe, expect, it } from "vitest";

import { THEME_KEY, nextTheme, readTheme, storeTheme } from "./theme";

function storage(initial: Record<string, string> = {}) {
  const data = { ...initial };
  return {
    getItem: (k: string) => (k in data ? data[k] : null),
    setItem: (k: string, v: string) => {
      data[k] = v;
    },
    data,
  } as unknown as Storage & { data: Record<string, string> };
}

const throwing = {
  getItem() {
    throw new Error("blocked");
  },
  setItem() {
    throw new Error("blocked");
  },
} as unknown as Storage;

describe("readTheme", () => {
  it("follows the system preference when nothing has been chosen", () => {
    expect(readTheme(storage(), true)).toBe("dark");
    expect(readTheme(storage(), false)).toBe("light");
  });

  it("lets an explicit choice win over the system preference", () => {
    expect(readTheme(storage({ [THEME_KEY]: "light" }), true)).toBe("light");
    expect(readTheme(storage({ [THEME_KEY]: "dark" }), false)).toBe("dark");
  });

  it("ignores a stored value that is not a theme", () => {
    expect(readTheme(storage({ [THEME_KEY]: "neon" }), true)).toBe("dark");
  });

  it("still answers when storage is unavailable", () => {
    // Private windows and blocked site-data both throw on access rather than
    // returning null, and a theme is never worth failing a render over.
    expect(readTheme(throwing, true)).toBe("dark");
  });
});

describe("storeTheme", () => {
  it("persists the choice", () => {
    const store = storage();
    storeTheme(store, "dark");
    expect(store.data[THEME_KEY]).toBe("dark");
  });

  it("does not throw when storage refuses", () => {
    expect(() => storeTheme(throwing, "dark")).not.toThrow();
  });
});

describe("nextTheme", () => {
  it("flips", () => {
    expect(nextTheme("light")).toBe("dark");
    expect(nextTheme("dark")).toBe("light");
  });
});
