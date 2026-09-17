/**
 * Ending the session, through the route the rest of the surface already uses.
 *
 * `POST /web/signout` is the existing sign-out, and it clears the session
 * cookie server-side. The server-rendered pages reach it with an HTML form; the
 * panel posts it with `fetch` instead, because its Content-Security-Policy sets
 * `form-action 'none'` and should keep doing so -- a surface that submits no
 * forms is a surface a markup-injection bug cannot use to post anywhere.
 *
 * `require_csrf` reads the `X-CSRF-Token` header before it falls back to form
 * fields, so this needs nothing new on the server.
 */

export const SIGNOUT_URL = "../web/signout";
export const SIGNED_OUT_URL = "../web/signed-out";

export async function signOut(csrfToken: string, fetchImpl: typeof fetch = fetch): Promise<string> {
  try {
    await fetchImpl(SIGNOUT_URL, {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrfToken },
    });
  } catch {
    // Deliberately swallowed. Whatever happened to the request, the person
    // asked to leave: sending them to the signed-out page is right either way,
    // and that page is where they find out if a session somehow survived.
  }
  return SIGNED_OUT_URL;
}
