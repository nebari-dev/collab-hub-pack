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

  it("asks the signed-out page to send sign-in back to the panel", () => {
    // App-relative, as the sign-in redirect expects; the page resolves it
    // against the deployment's root path.
    expect(new URL(SIGNED_OUT_URL, "https://hub.example/admin/").searchParams.get("next")).toBe("/admin/");
  });

  // The signed-out page is static: it cannot tell anyone their session
  // survived. So a sign-out the server did not accept must not end there.
  it("reports a failure when the server did not confirm the sign-out", async () => {
    const failing = (async () => {
      throw new TypeError("network");
    }) as unknown as typeof fetch;
    const refused = (async () => new Response(null, { status: 403 })) as unknown as typeof fetch;

    expect(await signOut("csrf-value", failing)).toBeNull();
    expect(await signOut("csrf-value", refused)).toBeNull();
  });
});
