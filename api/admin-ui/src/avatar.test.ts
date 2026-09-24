import { describe, expect, it } from "vitest";

import { initials } from "./avatar";

describe("initials", () => {
  it("uses the first letters of a full name", () => {
    expect(initials({ name: "Alice Example", email: "alice@example.com" })).toBe("AE");
  });

  it("falls back to the address when there is no name", () => {
    expect(initials({ name: "", email: "alice@example.com" })).toBe("AL");
  });

  it("falls back to the subject when there is neither", () => {
    expect(initials({ name: "", email: "", user: "u-local-admin" })).toBe("U-");
  });

  it("ignores middle names rather than crowding the circle", () => {
    expect(initials({ name: "Alice Beatrice Example", email: "" })).toBe("AE");
  });

  it("answers something for every caller, so the circle is never blank", () => {
    expect(initials({ name: "", email: "", user: "" })).toBe("?");
  });
});
