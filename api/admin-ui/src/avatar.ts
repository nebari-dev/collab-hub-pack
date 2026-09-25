/**
 * A person's initials, for the header circle.
 *
 * Drawn locally rather than fetched. The panel's Content-Security-Policy allows
 * images only from this origin, so a picture from the identity provider or a
 * gravatar service would be blocked -- and widening that policy to a third
 * party for a decoration would be a poor trade. Two letters from what we
 * already know is enough to tell one signed-in person from another.
 */

export function initials(who: { name?: string; email?: string; user?: string }): string {
  const name = (who.name ?? "").trim();
  if (name) {
    const parts = name.split(/\s+/);
    // First and last, never the middle: three letters crowd a 2rem circle, and
    // "Alice Beatrice Example" is recognisable as AE.
    const first = parts[0]?.[0] ?? "";
    const last = parts.length > 1 ? (parts[parts.length - 1][0] ?? "") : "";
    const pair = (first + last).toUpperCase();
    if (pair) return pair;
  }

  const email = (who.email ?? "").trim();
  if (email) return email.slice(0, 2).toUpperCase();

  const user = (who.user ?? "").trim();
  if (user) return user.slice(0, 2).toUpperCase();

  // Never an empty circle: an unlabelled blank reads as a rendering fault.
  return "?";
}
