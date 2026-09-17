import { describe, expect, it } from "vitest";

import { SIGNED_OUT_URL, signOut } from "./signout";

describe("signOut", () => {
  it("posts to the existing sign-out route with the CSRF header", async () => {
    const seen: { url?: string; init?: RequestInit } = {};
    const stub = (async (url: string, init: RequestInit) => {
      seen.url = url;
      seen.init = init;
      return new Response(null, { status: 303 });
    }) as unknown as typeof fetch;

    const next = await signOut("csrf-value", stub);

    expect(seen.url).toBe("../web/signout");
    expect(seen.init?.method).toBe("POST");
    expect((seen.init?.headers as Record<string, string>)["X-CSRF-Token"]).toBe("csrf-value");
    expect(next).toBe(SIGNED_OUT_URL);
  });

  it("still sends the person to the signed-out page when the call fails", async () => {
    const failing = (async () => {
      throw new TypeError("network");
    }) as unknown as typeof fetch;

    expect(await signOut("csrf-value", failing)).toBe(SIGNED_OUT_URL);
  });
});
