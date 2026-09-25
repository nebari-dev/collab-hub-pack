import { describe, expect, it } from "vitest";

import { loadSession } from "./session";

const OPERATOR = {
  user: "u-1",
  name: "Alice",
  email: "alice@example.com",
  email_verified: true,
  role: "operator",
  csrf_token: "csrf-value",
  version: "0.1.0",
};

function answering(status: number, body?: unknown): typeof fetch {
  return (async () =>
    new Response(body === undefined ? null : JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    })) as unknown as typeof fetch;
}

describe("loadSession", () => {
  it("reports the signed-in operator when the API answers", async () => {
    const result = await loadSession(answering(200, OPERATOR));

    expect(result).toEqual({ state: "ok", session: OPERATOR });
  });

  it("distinguishes signed out from forbidden", async () => {
    expect(await loadSession(answering(401, { error: "authentication required" }))).toEqual({
      state: "signed-out",
    });
    expect(await loadSession(answering(403, { error: "forbidden" }))).toEqual({
      state: "forbidden",
    });
  });

  it("reports an unavailable deployment separately from a plain failure", async () => {
    expect(await loadSession(answering(503, { error: "authorization is unavailable" }))).toEqual({
      state: "unavailable",
    });
    expect(await loadSession(answering(500))).toEqual({ state: "error" });
  });

  it("treats a network failure as an error rather than a sign-out", async () => {
    const failing = (async () => {
      throw new TypeError("network");
    }) as unknown as typeof fetch;

    expect(await loadSession(failing)).toEqual({ state: "error" });
  });

  it("treats a success whose body is not the promised JSON as an error", async () => {
    const malformed = (async () =>
      new Response("not json", {
        status: 200,
        headers: { "content-type": "application/json" },
      })) as unknown as typeof fetch;

    expect(await loadSession(malformed)).toEqual({ state: "error" });
  });
});
