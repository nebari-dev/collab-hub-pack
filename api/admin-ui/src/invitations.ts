import type { Resource } from "./api";

const ISSUE_OUTCOMES: Record<string, string> = {
  sent: "Invitation sent.",
  send_unknown:
    "The invitation was created, but the mail provider did not confirm delivery. Check before sending another.",
  send_failed:
    "The invitation was created, but the email could not be sent. They will need the link another way.",
  already_live: "That address already has a live invitation, so a second one was not created.",
  invalid_email: "That does not look like an email address.",
  unavailable: "This hub cannot issue invitations right now.",
};

/**
 * What to tell an operator after issuing an invitation.
 *
 * The server names the outcome, on a success as well as a refusal: a 201 can
 * still mean the email did not go out. This only maps its word to a sentence,
 * and falls back for a failure that never reached the endpoint at all.
 */
export function invitationNotice(result: Resource<unknown>): { text: string; bad: boolean } {
  const outcome =
    result.state === "ok"
      ? (result.data as { outcome?: unknown } | null)?.outcome
      : "reason" in result
        ? result.reason
        : undefined;
  if (typeof outcome === "string" && outcome in ISSUE_OUTCOMES) {
    return { text: ISSUE_OUTCOMES[outcome], bad: outcome !== "sent" };
  }
  if (result.state === "ok") return { text: ISSUE_OUTCOMES.sent, bad: false };
  return { text: "That did not go through. Nothing was created.", bad: true };
}
