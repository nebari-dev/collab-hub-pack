"""Authorization wrappers for the two authority axes (issue #87, Gate E).

One wrapper per axis, composed *on top of* the audited-execution primitive in
:mod:`.audit`, which is deliberately unaware of either:

- :func:`requires_platform_role` — cross-deployment authority, from the
  caller's active ``collab_platform_roles`` row
  (``AuthContext.platform_role``).
- :func:`requires_org_role` — authority inside **one** organization, from the
  caller's membership row (``AuthContext.org_role``), optionally pinned to
  the organization the call actually targets via ``org_arg``.

An endpoint declares which axis governs it. Owner-initiated and
operator-initiated performances of the same action therefore produce
identical audit rows differing only in ``actor`` — which is exactly right —
and neither axis ever implies the other: an operator does not pass the owner
check, an owner does not pass the operator check, and both facts are tested
independently.

Both wrappers read the :class:`~.auth.AuthContext` **already resolved by the
auth choke point** out of the wrapped callable's own arguments. They never
resolve authentication themselves, and they fail closed: a call that carries
no context (or an ambiguous one) is a programming error and raises rather
than defaulting to "allowed". Denials are plain 403s — the caller is
authenticated and known, they simply lack the authority — and the two axes'
denials are indistinguishable on the wire on purpose.

These are decorators rather than FastAPI dependencies so the policy sits on
the function that performs the action, survives being called from non-HTTP
contexts (MCP tools, CLI), and cannot be forgotten by a router include. On a
FastAPI endpoint they run after dependency injection, so the ``AuthContext``
from ``Depends(get_auth_context)`` is among the bound arguments as required.

**Decorator order is load-bearing — the guard goes UNDER the route.**
Decorators apply bottom-up, so this is guarded::

    @router.post("/orgs/{org_id}/invitations")   # registers the guarded wrapper
    @requires_org_role("owner", org_arg="org_id")
    def send_invitation(auth: AuthContext, org_id: str): ...

and this **registers the original, unguarded function as the endpoint** while
the guarded wrapper is thrown away::

    @requires_platform_role("operator")   # wraps what router.post returned —
    @router.post("/operator/invitations") # the route already holds the raw fn
    def send_invitation(auth: AuthContext): ...

That misuse cannot be prevented at decoration time (the guard cannot know a
route was registered first), so it is made **detectable**: wrapping marks the
callable it wrapped, and :func:`verify_protected_routes` fails loudly if any
registered route's endpoint carries that mark — i.e. some guard wrapped the
registered object itself and therefore sits outside the registered call
chain. The rule covers the **partial** misordering too, where one guard sits
correctly below the route decorator and another sits above it: the route
then holds a genuine guard wrapper, but the outer guard is orphaned all the
same. Consumers that register guarded endpoints (issue #89) must call
:func:`verify_protected_routes` at app startup or in a test — it is the
difference between a misordered decorator being a failed deploy and being an
open, unauthorized route.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable

from fastapi import HTTPException, status

from .auth import AuthContext
from .orgs import PLATFORM_ROLE_OPERATOR, ROLE_MEMBER, ROLE_OWNER

_KNOWN_PLATFORM_ROLES = frozenset({PLATFORM_ROLE_OPERATOR})
_KNOWN_ORG_ROLES = frozenset({ROLE_OWNER, ROLE_MEMBER})


def _forbidden() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have the authority to perform this action.",
    )


def _auth_context(fn: Callable, arguments: dict) -> AuthContext:
    """Find the one resolved AuthContext among the call's own arguments.

    Located by type, not by parameter name, so a renamed parameter cannot
    silently detach the check from the context it governs. Zero contexts and
    two contexts are both programming errors and both fail closed — defaults
    are deliberately not applied, because a default ``AuthContext`` sitting in
    a signature is a fixture nobody authenticated.
    """

    contexts = [value for value in arguments.values() if isinstance(value, AuthContext)]
    if len(contexts) != 1:
        raise RuntimeError(
            f"{fn.__qualname__} is wrapped by an authorization check but was called with "
            f"{len(contexts)} AuthContext arguments; exactly one is required. Authorization "
            "fails closed rather than guessing which caller it is deciding for."
        )
    return contexts[0]


_GUARD_OF_ATTR = "_authorization_guard_of"
"""Set on a guard wrapper: the callable it protects. Introspection only —
the verifier's rule does not consult it (see verify_protected_routes)."""

_HAS_GUARD_ATTR = "_authorization_orphanable_guards"
"""Set on the callable a guard wrapped: the labels of every guard wrapping
it. Finding this mark on a **registered route endpoint** means those guards
sit outside the registered call chain — i.e. they were applied after (above)
the route decorator and enforce nothing."""


def _wrap(fn: Callable, check: Callable[[dict], None], label: str) -> Callable:
    """Apply *check* to every call of *fn*, preserving sync/async-ness."""

    sig = inspect.signature(fn)

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            check(sig.bind(*args, **kwargs).arguments)
            return await fn(*args, **kwargs)

    else:

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            check(sig.bind(*args, **kwargs).arguments)
            return fn(*args, **kwargs)

    # The wrapper must not carry the wrapped-by-a-guard mark itself:
    # functools.wraps copied fn.__dict__ (which may hold marks from another
    # decoration of the same fn), and nothing has wrapped THIS wrapper yet.
    wrapper.__dict__.pop(_HAS_GUARD_ATTR, None)
    setattr(wrapper, _GUARD_OF_ATTR, fn)
    # Mark the callable this guard wrapped — appending, so a stack of guards
    # records every one of them. If the marked object ends up registered as a
    # route endpoint, these guards are outside the registered chain (the
    # @requires_... line sat above the route decorator) and enforce nothing;
    # verify_protected_routes turns that into a loud failure naming them.
    fn.__dict__.setdefault(_HAS_GUARD_ATTR, []).append(label)
    return wrapper


def _iter_routes(routes) -> list:
    collected = []
    for route in routes:
        collected.append(route)
        collected.extend(_iter_routes(getattr(route, "routes", ())))
    return collected


def verify_protected_routes(app) -> None:
    """Fail loudly if any registered route orphans an authorization guard.

    Walks every route of *app* (routers and mounts included) and raises if a
    route's endpoint carries the wrapped-by-a-guard mark — meaning some
    ``@requires_...`` guard wrapped **the registered object itself**, so that
    guard lives *outside* the registered call chain and enforces nothing.
    That single rule catches both misordering shapes:

    - **full**: every guard above the route decorator — the route holds the
      raw function, which the guards marked;
    - **partial**: a mixed stack (one guard below the route decorator, one
      above) — the route holds the inner guard wrapper, which the outer,
      orphaned guard marked. The endpoint being a guard itself is exactly
      why the old "is the endpoint a guard?" rule was insufficient.

    Correct ordering (all guards below the route decorator, nearest the
    function) registers the outermost wrapper, which nothing further wrapped,
    so it carries no mark.

    Call it at startup or from a test in any module that registers guarded
    endpoints. Detection is by attributes stamped at wrap time, so it also
    survives further ``functools.wraps``-based decoration of the registered
    chain; it cannot see wrappers that hide the endpoint without copying
    ``__dict__``, which is why this is a required check, not optional
    belt-and-braces.
    """

    offenders = []
    for route in _iter_routes(getattr(app, "routes", ())):
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        orphaned = getattr(endpoint, _HAS_GUARD_ATTR, None)
        if orphaned:
            offenders.append(
                f"{getattr(route, 'path', route)} -> {getattr(endpoint, '__qualname__', endpoint)}"
                f" (orphaned guards: {', '.join(orphaned)})"
            )
    if offenders:
        raise RuntimeError(
            "Routes whose endpoint has ORPHANED authorization guards — the @requires_... "
            "decorator sat above the route decorator (decorators apply bottom-up), so the "
            "registered endpoint does not enforce it; move every guard below the route "
            f"decorator, nearest the function: {'; '.join(sorted(offenders))}"
        )


def requires_platform_role(role: str) -> Callable[[Callable], Callable]:
    """Admit only callers whose **platform** role is exactly *role*.

    Cross-deployment authority: ``@requires_platform_role("operator")`` on an
    action means "operators may do this to any organization, or to none".
    Everything about the caller's *organization* — including being its owner —
    is irrelevant to this check by design.

    Under claims-sourced auth ``platform_role`` is structurally ``None``
    (the ``collab_`` tables are never read), so every caller is denied: a
    deployment that has not adopted server-owned authorization has no
    operators.
    """

    if role not in _KNOWN_PLATFORM_ROLES:
        # A typo'd role would deny everyone forever, silently — the check
        # would look strict while actually being dead. Fail at import time.
        raise ValueError(f"Unknown platform role {role!r}; known roles: {sorted(_KNOWN_PLATFORM_ROLES)}")

    def decorator(fn: Callable) -> Callable:
        def check(arguments: dict) -> None:
            ctx = _auth_context(fn, arguments)
            if ctx.platform_role != role:
                raise _forbidden()

        return _wrap(fn, check, f"requires_platform_role({role!r})")

    return decorator


def requires_org_role(role: str, *, org_arg: str | None = None) -> Callable[[Callable], Callable]:
    """Admit only callers holding *role* in the organization the call targets.

    Authority inside **one** organization. The check is two-part:

    - the caller's membership role must be exactly *role*; and
    - when ``org_arg`` names one of the wrapped callable's parameters, the
      organization id passed there must equal the caller's own ``org_id``.
      An owner of org A is nobody in org B, so a mismatch is the same plain
      403 as not being an owner at all.

    ``org_arg`` should be given whenever the action names its target
    organization explicitly; omit it only for actions that are implicitly
    scoped to the caller's home organization. When given, the argument must be
    present and non-empty on every call — a missing target is a programming
    error and fails closed, because "no org supplied" must never widen into
    "any org allowed".

    Under claims-sourced auth ``org_role`` is ``None`` and every caller is
    denied, same rationale as :func:`requires_platform_role`.
    """

    if role not in _KNOWN_ORG_ROLES:
        raise ValueError(f"Unknown org role {role!r}; known roles: {sorted(_KNOWN_ORG_ROLES)}")

    def decorator(fn: Callable) -> Callable:
        if org_arg is not None and org_arg not in inspect.signature(fn).parameters:
            # Decoration-time, not call-time: a misspelled org_arg would
            # otherwise fail closed on every call in production instead of in
            # the first test that imports the module.
            raise ValueError(f"{fn.__qualname__} has no parameter {org_arg!r} to check the target organization from")

        def check(arguments: dict) -> None:
            ctx = _auth_context(fn, arguments)
            if ctx.org_role != role:
                # Covers the hub-scoped operator shape too: a membership-less
                # operator has org_role None and is denied here — platform
                # authority never manufactures org authority.
                raise _forbidden()
            if org_arg is not None:
                target_org = arguments.get(org_arg)
                if not target_org or not isinstance(target_org, str):
                    raise RuntimeError(
                        f"{fn.__qualname__} was called without a usable {org_arg!r}; the org-role "
                        "check cannot tell which organization it is deciding for and fails closed."
                    )
                # home_org_id, not the org_id property: this wrapper is one of
                # the few explicitly hub-aware readers, and on a hand-built
                # context with a role but no org the comparison must be a
                # plain 403, not a NoOrganizationError from mid-check.
                if target_org != ctx.home_org_id:
                    raise _forbidden()

        return _wrap(fn, check, f"requires_org_role({role!r}, org_arg={org_arg!r})")

    return decorator
