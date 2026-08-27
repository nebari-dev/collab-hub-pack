"""Where the auth choke point gets the caller's organization: claims, or membership.

``auth_context_from_claims`` historically read ``org_id``/``workspace_id`` from
the token, with ``FRAMES_AUTH_DEFAULT_ORG``/``FRAMES_AUTH_DEFAULT_WORKSPACE``
as fallbacks. On a deployment where Keycloak mints no org claim — which is
every deployment today — that fallback is what every caller actually gets, so
**every user collapses into one organization and ``internal`` means "everyone
on the server"**. Retiring it is issue #63.

``FRAMES_AUTH_ORG_SOURCE`` selects the source:

- ``claims`` (or unset) — the historical behavior, unchanged byte for byte.
  What every existing deployment keeps on upgrade.
- ``membership`` — the server owns the org model: the caller's one
  ``collab_org_members`` row decides ``(org_id, role)``, ``workspace_id`` is
  the constant ``"default"``, org claims in the token are ignored entirely, and
  the ``FRAMES_AUTH_DEFAULT_*`` fallbacks are refused rather than ignored.

**Why this is a separate switch from the identity pin.** It would have been
shorter to hang membership resolution off ``FRAMES_AUTH_IDENTITY_CLAIM=sub``
(issue #61), since membership rows are keyed by the subject and the pin is
therefore a hard precondition of this mode. It is deliberately not done that
way: the internal hub's migration has to pin identity *first*, backfill
membership rows against the resulting subjects, verify coverage, and only then
retire the fallback. Fused into one switch, that sequence has no middle state
to stop in — the flip would strand every user whose membership row had not
been written yet. Two switches, one of which is a precondition of the other,
keeps the two steps independently reversible.

The parsing contract mirrors ``frames.identity``: exact match, no trimming or
case folding, and an unrecognized value fails startup instead of guessing.
Guessing is wrong in both directions here — quietly choosing ``claims`` would
resurrect the default-org fallback on the external deployment, and quietly
choosing ``membership`` would lock out every user of a hub that has no
membership rows.
"""

from __future__ import annotations

import os

from .identity import IDENTITY_CLAIM_ENV, identity_pinned_to_sub

ORG_SOURCE_ENV = "FRAMES_AUTH_ORG_SOURCE"

ORG_SOURCE_CLAIMS = "claims"
ORG_SOURCE_MEMBERSHIP = "membership"

DEFAULT_ORG_ENV = "FRAMES_AUTH_DEFAULT_ORG"
DEFAULT_WORKSPACE_ENV = "FRAMES_AUTH_DEFAULT_WORKSPACE"
RETIRED_DEFAULT_ENVS = (DEFAULT_ORG_ENV, DEFAULT_WORKSPACE_ENV)
"""The fallbacks membership mode retires. Refused at startup, not ignored."""


def org_source_is_membership() -> bool:
    """Return whether org context is resolved from ``collab_org_members``.

    ``make_app`` calls this once at startup so a mistyped value fails the
    rollout rather than the first authenticated request.
    """

    mode = os.environ.get(ORG_SOURCE_ENV, "")
    if mode in ("", ORG_SOURCE_CLAIMS):
        return False
    if mode == ORG_SOURCE_MEMBERSHIP:
        return True
    raise RuntimeError(
        f"Unsupported {ORG_SOURCE_ENV} value {mode!r}: expected exactly "
        f"'{ORG_SOURCE_MEMBERSHIP}' (resolve the caller's organization from collab_org_members) "
        f"or '{ORG_SOURCE_CLAIMS}' (org/workspace token claims with the "
        f"{DEFAULT_ORG_ENV}/{DEFAULT_WORKSPACE_ENV} fallbacks, the default)."
    )


def enforce_membership_org_source_preconditions() -> None:
    """Refuse to start in membership mode with an unsafe surrounding config.

    Two preconditions, both checked from ``make_app``:

    1. **The identity pin must be on.** ``collab_org_members.user_id`` is the
       OIDC subject, so resolving a membership for a legacy
       ``preferred_username``/``email`` principal would look up the wrong key —
       denying everyone quietly, or worse, matching whoever happens to own a
       colliding value.
    2. **The retired fallbacks must be unset, not merely unread.** Nothing
       reads them in membership mode, so a leftover value is invisible right up
       until someone flips the source back to ``claims`` during an incident and
       every user silently collapses into one organization again. A retirement
       that is only "ignored" is not a retirement.

    The third precondition — an organization store with a real backend — needs
    the built store object and so lives in ``make_app`` itself.
    """

    if not org_source_is_membership():
        return

    if not identity_pinned_to_sub():
        raise RuntimeError(
            f"{ORG_SOURCE_ENV}={ORG_SOURCE_MEMBERSHIP} requires {IDENTITY_CLAIM_ENV}=sub: "
            "membership rows are keyed by the OIDC subject, so a legacy "
            "(preferred_username/email) principal would be resolved against the wrong key."
        )
    for retired in RETIRED_DEFAULT_ENVS:
        if os.environ.get(retired, "").strip():
            raise RuntimeError(
                f"{retired} must be unset when {ORG_SOURCE_ENV}={ORG_SOURCE_MEMBERSHIP}: the "
                "default-org fallback is retired on membership-resolving deployments. Left set, "
                "it would spring back the moment the source is flipped to "
                f"'{ORG_SOURCE_CLAIMS}', collapsing every user into one organization where "
                "'internal' means everyone on the server."
            )
