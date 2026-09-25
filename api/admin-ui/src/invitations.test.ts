import { describe, expect, it } from "vitest";

import { invitationNotice } from "./invitations";

describe("invitationNotice", () => {
  it("reports a delivered invitation as sent", () => {
    expect(invitationNotice({ state: "ok", data: { outcome: "sent" } })).toEqual({
      text: "Invitation sent.",
      bad: false,
    });
  });

  it("does not call a created invitation whose email failed 'sent'", () => {
    const failed = invitationNotice({ state: "ok", data: { outcome: "send_failed" } });
    const unknown = invitationNotice({ state: "ok", data: { outcome: "send_unknown" } });

    expect(failed.bad).toBe(true);
    expect(failed.text).toMatch(/could not be sent/);
    expect(unknown.bad).toBe(true);
    expect(unknown.text).toMatch(/did not confirm delivery/);
  });

  it("uses the server's reason for a refusal", () => {
    expect(invitationNotice({ state: "refused", reason: "already_live" }).text).toMatch(/already has a live invitation/);
  });

  it("falls back when the request never got an answer", () => {
    expect(invitationNotice({ state: "error" })).toEqual({
      text: "That did not go through. Nothing was created.",
      bad: true,
    });
  });
});
