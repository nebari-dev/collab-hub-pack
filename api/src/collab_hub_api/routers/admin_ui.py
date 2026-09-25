"""Serving the admin panel's built assets.

The panel is a single-page app built by ``admin-ui`` and copied into the image
by the Docker build. This module serves the document it boots from and the
hashed asset files beside it, and does nothing else -- everything the panel
reads or writes goes through :mod:`.admin_api`.

Routes, not a mount
-------------------
Two explicit routes rather than ``StaticFiles``. The browser surface refuses a
``Mount`` under its prefixes at startup on purpose: the guard authenticates by
path before routing, and a mounted sub-app's routing is opaque to the checks
that verify the surface is covered. Two routes cost four lines and keep the
surface's own verification meaningful.

Which also means the asset path is validated here rather than by somebody
else's implementation. ``filename`` is matched against a strict pattern and the
resolved path is required to be inside the asset directory, so a traversal
attempt is a 404 rather than a file read.

The trailing slash is load-bearing
----------------------------------
The document is served at ``/admin/`` and ``/admin`` redirects to it. That is
not tidiness: Vite emits document-relative asset URLs (``./assets/…``), and a
browser resolves those against the document's *directory*. Served at ``/admin``
the directory is ``/``, so the browser asks for ``/assets/…`` -- outside this
surface entirely, refused by the protection map -- and the panel renders as a
blank page while its document returns a perfectly good 200.

The same resolution decides where the panel's own API calls go: ``api/session``
from ``/admin/`` is ``/admin/api/session``, and from ``/admin`` it is
``/api/session``.

Document-relative rather than absolute, and a redirect rather than a rewritten
base, because the app can be mounted under a ``rootPath`` prefix. Relative URLs
resolved from ``/prefix/admin/`` land on ``/prefix/admin/assets/…`` with nothing
in the bundle knowing the prefix exists.

Absent assets mount nothing
---------------------------
``make_app`` only mounts this router when the built directory actually exists.
A deployment without a built panel therefore 404s on ``/admin``, which is what
it did before this existed, rather than answering 500 from a route that cannot
find its own index.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from ..web.authz import require_operator

__all__ = ["ASSET_NAME", "make_router"]

ASSET_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
"""What an asset file may be called.

Vite emits hashed names from this alphabet. No slashes and no dots-only names,
so ``..`` and every path with a separator in it fail the match before anything
touches the filesystem.
"""


def make_router(dist: Path) -> APIRouter:
    """Serve the panel from *dist*, behind the operator gate.

    The router carries ``require_operator``, so a signed-in non-operator gets
    the surface's 403 page rather than the shell. The shell holds no data and
    serving it would leak nothing, but an admin panel that renders its chrome
    for someone who may not use it is a worse answer than a plain refusal.
    """

    router = APIRouter(include_in_schema=False, dependencies=[Depends(require_operator)])
    index = dist / "index.html"
    assets = dist / "assets"

    @router.get("/admin/")
    def panel_shell() -> HTMLResponse:
        return HTMLResponse(index.read_text(encoding="utf-8"))

    @router.get("/admin")
    def panel_shell_redirect(request: Request) -> RedirectResponse:
        """Send ``/admin`` to ``/admin/``; see the module note on the slash.

        308 rather than 302 so the method is preserved and the browser caches
        the canonical form, and built from ``root_path`` so a deployment under
        a prefix redirects within its own mount instead of to the origin root.

        The fragment survives on its own: browsers do not send it and reapply
        it to the redirect target, so ``/admin#audit`` still opens the audit
        section.
        """

        root_path = (request.scope.get("root_path") or "").rstrip("/")
        return RedirectResponse(f"{root_path}/admin/", status_code=status.HTTP_308_PERMANENT_REDIRECT)

    @router.get("/admin/assets/{filename}")
    def panel_asset(filename: str) -> FileResponse:
        if not ASSET_NAME.match(filename) or filename in (".", ".."):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        target = (assets / filename).resolve()
        # The pattern already excludes separators, so this cannot currently
        # fail. It is kept because the pattern is the thing most likely to be
        # loosened by someone adding a file type, and a containment check that
        # only exists while the pattern is strict is a check that disappears
        # exactly when it starts to matter.
        if not target.is_file() or assets.resolve() not in target.parents:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return FileResponse(target)

    return router
