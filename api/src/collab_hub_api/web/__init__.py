"""The server-side browser surface: OIDC session, CSRF, and page scaffolding.

Issue #88 (Gate E, ratified 2026-08-03): operators and org owners drive
privileged actions from a server-rendered web interface. This package is the
shared surface those pages build on — the pages themselves (``/invite/accept``,
``/admin/invitations``, ``/org/invitations``) are separate issues (#90–#92).

What lives here, and why it is a *second* auth axis rather than a reuse of the
bearer path in ``frames.auth``:

* ``session`` — the signed, HttpOnly, Secure session cookie a browser holds
  after signing in, and the CSRF secret bound to it. Bearer tokens belong to
  the desktop app; a browser must never be asked to hold one.
* ``oidc`` — the OIDC authorization-code flow against the deployment's
  **confidential** web client (``collab-web``), including the ID-token
  verification whose audience is always this client's own id (see issue #83
  for the defect this deliberately does not repeat).
* ``authz`` — page-facing dependencies: require a session, require the
  ``operator`` platform role, require org ``owner``. Authorization is resolved
  per request from the server's own stores, never frozen into the cookie, so
  revoking a role takes effect on the next request rather than at session
  expiry.
* ``pages`` — the shared layout, stylesheet, and security headers
  (``Referrer-Policy: no-referrer``, ``Cache-Control: no-store``, a
  no-script CSP) every page of the surface serves.
* ``surface`` — configuration resolution and the fail-fast startup
  preconditions, including the check that the per-path protection map
  actually lets a browser reach the sign-in routes.

The routes are in ``routers.web``; ``core.make_app`` mounts them only when a
web client id is configured.
"""
