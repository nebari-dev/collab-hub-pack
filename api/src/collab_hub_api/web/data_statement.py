"""The data statement (#146): what is stored, who can see it, how to ask for deletion.

an internal issue left one item open on purpose: trusted external invitees should be
told what this deployment stores about them, who can see it, and how to ask
for deletion — "the users are known to *us*, but our data handling is not
known to *them*." The copy below is that statement, authored by the team on
2026-08-11.

It is shown at three moments, and the text lives **here once** so the
surfaces cannot drift from each other or from the record:

* on its own page (:func:`data_statement_page`, served anonymously at
  :data:`~.surface.DATA_STATEMENT_PATH`) — the canonical, linkable copy;
* on the acceptance page's ready state (:mod:`.acceptance`), immediately
  above the button that performs the one irreversible act;
* in the invitation email, once automated delivery (#93) sends one — until
  then the operator's manually sent email carries the same paragraph.

This file's git history is the answer to "what were people shown, and when?"
— which is why edits to :data:`DATA_STATEMENT_TEXT` should change the copy
and nothing else in the same commit.
"""

from __future__ import annotations

import html

DATA_STATEMENT_CONTACT = "collab-support@openteams.com"
"""Where questions and deletion requests go. Also named inside the statement
text itself; this constant exists so the page can render it as a mailto link
without a second spelling of the address."""

DATA_STATEMENT_TEXT = (
    "We store your basic account details, organization membership, content"
    " you create, and limited security logs. Your information is visible"
    " according to the service’s sharing settings, and authorized"
    " OpenTeams staff may access it to operate and support the service. To"
    " ask a question or request deletion, contact OpenTeams at"
    " collab-support@openteams.com."
)
"""The statement, as plain text — the form the acceptance page renders and
the invitation email will carry."""


def data_statement_page(*, root_path: str = "") -> str:
    """The canonical page. Anonymous by design — see the path's entry in
    :data:`~.surface.PUBLIC_WEB_PATHS` for the argument.

    :func:`~.pages.render_page` is imported here rather than at module level so
    that importing this module costs nothing but strings. The invitation email
    (#93) needs :data:`DATA_STATEMENT_TEXT` and is built in
    :mod:`..frames.invitation_email`; a module-level import would drag the whole
    browser-surface layout — and FastAPI with it — into the mail path for one
    paragraph of text. Same deferred-import idiom :mod:`.pages` already uses
    for its own cycle. (Other modules in this package use it too; they are
    deliberately not named, because a test enumerates every file mentioning the
    link-display module to keep its removal a complete recipe, and a docstring
    cross-reference would register there as a dependency that does not exist.)
    """

    from .pages import render_page

    return render_page(
        title="Data statement",
        body=(
            "<h1>What we store, and who can see it</h1>"
            f"<p>{html.escape(DATA_STATEMENT_TEXT)}</p>"
            "<p>Questions and deletion requests: "
            f'<a href="mailto:{html.escape(DATA_STATEMENT_CONTACT)}">'
            f"{html.escape(DATA_STATEMENT_CONTACT)}</a></p>"
        ),
        root_path=root_path,
    )
