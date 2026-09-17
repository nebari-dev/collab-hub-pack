import { describe, expect, it } from "vitest";

import { getJson } from "./api";

function answering(status: number, body?: unknown): typeof fetch {
  return (async () =>
    new Response(body === undefined ? null : JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    })) as unknown as typeof fetch;
}

describe("getJson", () => {
  it("returns the parsed body on success", async () => {
    const result = await getJson<{ users_total: number }>("api/usage", answering(200, { users_total: 3 }));

    expect(result).toEqual({ state: "ok", data: { users_total: 3 } });
  });

  it("separates a deployment that cannot answer from one that refuses", async () => {
    expect(await getJson("api/audit", answering(503, { error: "audit_log_unavailable" }))).toEqual({
      state: "unavailable",
      reason: "audit_log_unavailable",
    });
    expect(await getJson("api/audit", answering(403, { error: "forbidden" }))).toEqual({
      state: "forbidden",
    });
  });

  it("surfaces an upstream failure as its own state, not as an empty result", async () => {
    expect(await getJson("api/model-access", answering(502, { error: "group_unavailable" }))).toEqual({
      state: "upstream-error",
      reason: "group_unavailable",
    });
  });

  it("treats a network failure as an error", async () => {
    const failing = (async () => {
      throw new TypeError("network");
    }) as unknown as typeof fetch;

    expect(await getJson("api/usage", failing)).toEqual({ state: "error" });
  });
});

describe("refusals carry the server's own word", () => {
  it("keeps the outcome from a declined request", async () => {
    expect(await getJson("api/invitations", answering(409, { outcome: "already_live" }))).toEqual({
      state: "refused",
      reason: "already_live",
    });
    expect(await getJson("api/invitations", answering(400, { outcome: "invalid_email" }))).toEqual({
      state: "refused",
      reason: "invalid_email",
    });
  });

  it("prefers outcome over error when both could apply", async () => {
    expect(await getJson("x", answering(503, { outcome: "unavailable", error: "other" }))).toEqual({
      state: "unavailable",
      reason: "unavailable",
    });
  });
});
