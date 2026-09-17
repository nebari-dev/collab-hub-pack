/**
 * Who the panel is talking to, and the token its writes must carry.
 *
 * The CSRF secret lives inside the signed, HttpOnly session cookie, which this
 * code cannot read by design, so the server hands it over here. That is also
 * why this call is the panel's first act: nothing can be written until it has
 * answered.
 *
 * The request URL is deliberately relative. The API can be mounted under a
 * rootPath prefix, and an absolute "/admin/api/session" would resolve to the
 * wrong origin-relative path there while working perfectly in every local
 * test -- the class of bug that only ever appears in someone else's cluster.
 */

export const SESSION_URL = "api/session";

export interface AdminSession {
  user: string;
  name: string;
  email: string;
  email_verified: boolean;
  role: string | null;
  csrf_token: string;
  /** The build actually running, read from the package's own metadata. */
  version: string;
}

/**
 * Four outcomes, kept distinct because the panel says something different for
 * each: signed out sends you to sign in, forbidden tells you this account has
 * no admin authority, unavailable is the deployment's problem and not yours,
 * and error is everything else.
 */
export type SessionResult =
  | { state: "ok"; session: AdminSession }
  | { state: "signed-out" }
  | { state: "forbidden" }
  | { state: "unavailable" }
  | { state: "error" };

export async function loadSession(fetchImpl: typeof fetch = fetch): Promise<SessionResult> {
  let response: Response;
  try {
    response = await fetchImpl(SESSION_URL, {
      headers: { accept: "application/json" },
      credentials: "same-origin",
    });
  } catch {
    return { state: "error" };
  }

  if (response.status === 401) return { state: "signed-out" };
  if (response.status === 403) return { state: "forbidden" };
  if (response.status === 503) return { state: "unavailable" };
  if (!response.ok) return { state: "error" };

  try {
    return { state: "ok", session: (await response.json()) as AdminSession };
  } catch {
    // A 200 whose body is not the JSON promised. Treating this as success
    // would put `undefined` into the panel's identity display; treating it as
    // an error is both truthful and the only recoverable answer.
    return { state: "error" };
  }
}
