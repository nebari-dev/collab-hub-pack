"""Keycloak-sourced mapping from stored principal strings to subjects.

The legacy identity precedence is ``("preferred_username", "email", "sub")``,
so a stored principal is one of those three shapes and nothing records which.
The mapping therefore has to run in reverse — take the string, find the account
it could have come from — and every interesting case is a case where that
question does not have a trustworthy answer.

**A unique current match is not proof of identity.** This is the whole reason
the migration exists: usernames and email addresses are *mutable and reusable*.
If Alice owned frames as ``alice@example.com``, left, and the address was later
assigned to a new hire, then a point-in-time read of the directory finds exactly
one candidate — the new hire — and nothing in the data says the string ever
meant anyone else. Migrating on that evidence hands Alice's content to somebody
who was never given it, silently and irreversibly. So an email or username match
is recorded as :attr:`MappingConfidence.unverified` and is never, on its own,
allowed to declare anything safe. Only a stored value that *is already a
subject* is :attr:`MappingConfidence.certain`, because subjects are immutable
and never reissued.

One signal for reassignment is available and is used: Keycloak's
``createdTimestamp``. An account created *after* the record it would be mapped
into was last written cannot be the account that principal referred to when it
was written. That is not a heuristic, it is a contradiction, and
:mod:`.analysis` treats such a mapping as no mapping at all. The absence of the
signal proves nothing — a reassignment inside an account's lifetime leaves no
trace at all in a point-in-time read — which is exactly why unverified stays
unverified even when the timestamps look fine.

**Simultaneous ambiguity is a refusal, not a tie-break.** If a stored string
matches user A by email and user B by username, both readings are plausible and
one of them hands A's frames to B. The mapping declines and the report names the
string, the candidates, and where it is stored.

**Resolution is on the exact stored string.** The service authorizes by exact
comparison, so ``" alice "`` is a principal that matches no caller today.
Trimming it here would invent access that does not exist and would silently
repair data this tool exists to describe. Padded principals are reported as
their own finding instead.

**Disabled accounts map, but do not count as live.** An owner whose account is
disabled resolves to a real subject, so a migration would carry it over
correctly; but an entity whose only surviving owner is disabled is unreachable
by anyone in practice, so it gets its own severity.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


class MappingConfidence(str, Enum):
    """How much weight a mapping can carry.

    Deliberately only two positive levels. There is no "probably": either the
    stored value is itself an immutable subject, or it is a mutable claim whose
    current holder may not be the person who wrote it.
    """

    certain = "certain"
    """The stored value *is* a known subject. Immutable and never reissued."""

    unverified = "unverified"
    """Matched on a mutable, reusable claim. A proposal for a human, not a fact."""

    none = "none"


class ResolutionStatus(str, Enum):
    """How (and whether) a stored principal resolved to a Keycloak subject."""

    already_sub = "already_sub"
    """The stored string is itself a known subject — nothing to migrate."""

    matched_email = "matched_email"
    matched_username = "matched_username"

    ambiguous = "ambiguous"
    """Two or more distinct subjects match. Never migrated automatically."""

    unmapped = "unmapped"
    """No Keycloak account matches. Left exactly as stored (see package docs)."""

    empty = "empty"
    """A blank principal was stored — a data defect, not a migration input."""


MAPPED_STATUSES = frozenset(
    {
        ResolutionStatus.already_sub,
        ResolutionStatus.matched_email,
        ResolutionStatus.matched_username,
    }
)

CONFIDENCE_BY_STATUS = {
    ResolutionStatus.already_sub: MappingConfidence.certain,
    ResolutionStatus.matched_email: MappingConfidence.unverified,
    ResolutionStatus.matched_username: MappingConfidence.unverified,
}


@dataclass(frozen=True)
class DirectoryUser:
    """One Keycloak account, reduced to the fields an identity migration needs."""

    sub: str
    username: str = ""
    email: str | None = None
    enabled: bool = True
    created_at: datetime | None = None
    """Keycloak's ``createdTimestamp``. Absent on realms or exports that omit
    it, which weakens the reassignment check to "no signal" rather than "no
    problem" — the report says which."""

    @property
    def label(self) -> str:
        """Human-facing name for the report — username first, email as backup."""

        return self.username or self.email or self.sub


@dataclass(frozen=True)
class Resolution:
    """The outcome of resolving one stored principal against the directory."""

    principal: str
    status: ResolutionStatus
    user: DirectoryUser | None = None
    candidates: tuple[DirectoryUser, ...] = ()

    @property
    def mapped(self) -> bool:
        return self.status in MAPPED_STATUSES

    @property
    def confidence(self) -> MappingConfidence:
        return CONFIDENCE_BY_STATUS.get(self.status, MappingConfidence.none)

    @property
    def certain(self) -> bool:
        return self.confidence is MappingConfidence.certain

    @property
    def sub(self) -> str | None:
        return self.user.sub if self.user else None

    @property
    def live(self) -> bool:
        """Whether this principal maps to an *enabled* account.

        A precondition for the orphan check, never a sufficient one: see
        :attr:`certain`, and :mod:`.analysis` for how the two combine.
        """

        return self.mapped and self.user is not None and self.user.enabled

    @property
    def padded(self) -> bool:
        """Whether the stored string carries leading or trailing whitespace.

        Reported, never trimmed: the service compares exactly, so a padded
        principal grants nothing today and "fixing" it during a migration would
        invent access.
        """

        return self.principal != self.principal.strip() and bool(self.principal.strip())


class DirectoryIndex:
    """Reverse index over Keycloak accounts: sub / email / username to subject.

    Email and username are matched case-insensitively. Keycloak lower-cases both
    by default, but a realm configured otherwise (or data imported from a
    federated store) can carry mixed case, and a case-sensitive miss would
    present as an unmapped owner — the single most misleading outcome this
    report can produce. Case folding is *matching*, not repair: nothing is
    rewritten and the stored string is always reported verbatim. Surrounding
    whitespace is **not** folded, because unlike case it changes whether the
    service's own exact comparison succeeds.
    """

    def __init__(self, users: list[DirectoryUser]):
        self.users = list(users)
        self._by_sub: dict[str, DirectoryUser] = {}
        self._by_email: dict[str, list[DirectoryUser]] = defaultdict(list)
        self._by_username: dict[str, list[DirectoryUser]] = defaultdict(list)
        for user in self.users:
            if user.sub:
                self._by_sub[user.sub] = user
            if user.email:
                self._by_email[user.email.casefold()].append(user)
            if user.username:
                self._by_username[user.username.casefold()].append(user)

    def __len__(self) -> int:
        return len(self.users)

    @property
    def duplicate_emails(self) -> list[str]:
        """Emails held by more than one account — every principal using one is ambiguous."""

        return sorted(email for email, users in self._by_email.items() if len(users) > 1)

    @property
    def accounts_without_created_at(self) -> int:
        """Accounts with no ``createdTimestamp``, i.e. no reassignment signal."""

        return len([user for user in self.users if user.created_at is None])

    def resolve(self, principal: str) -> Resolution:
        """Resolve one stored principal string — exactly as stored — to a subject."""

        if not principal.strip():
            return Resolution(principal=principal, status=ResolutionStatus.empty)

        folded = principal.casefold()
        by_sub = self._by_sub.get(principal)
        by_email = self._by_email.get(folded, [])
        by_username = self._by_username.get(folded, [])

        candidates: list[DirectoryUser] = []
        for user in ([by_sub] if by_sub else []) + by_email + by_username:
            if user not in candidates:
                candidates.append(user)

        if not candidates:
            return Resolution(principal=principal, status=ResolutionStatus.unmapped)
        if len({user.sub for user in candidates}) > 1:
            # Two different people are equally good readings of one string.
            return Resolution(
                principal=principal,
                status=ResolutionStatus.ambiguous,
                candidates=tuple(candidates),
            )

        user = candidates[0]
        if by_sub is not None:
            status = ResolutionStatus.already_sub
        elif by_email:
            status = ResolutionStatus.matched_email
        else:
            status = ResolutionStatus.matched_username
        return Resolution(principal=principal, status=status, user=user, candidates=(user,))


@dataclass
class DirectoryLoad:
    """A loaded directory plus how it was obtained, for the report's provenance."""

    index: DirectoryIndex
    source: str
    notes: list[str] = field(default_factory=list)


def user_from_keycloak_record(record: dict) -> DirectoryUser | None:
    """Build a :class:`DirectoryUser` from one raw Keycloak user representation."""

    sub = str(record.get("id") or record.get("sub") or "")
    if not sub:
        return None
    email = record.get("email")
    return DirectoryUser(
        sub=sub,
        username=str(record.get("username") or ""),
        email=str(email) if email else None,
        enabled=bool(record.get("enabled", True)),
        created_at=parse_created_at(record.get("createdTimestamp")),
    )


def parse_created_at(value: object) -> datetime | None:
    """Parse Keycloak's ``createdTimestamp`` (epoch milliseconds) defensively.

    Anything unparseable becomes "no signal" rather than a guess, because a
    wrong timestamp here would either invent a reassignment finding or suppress
    a real one.
    """

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(value / 1000, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _with_created_at_note(index: DirectoryIndex, notes: list[str]) -> list[str]:
    missing = index.accounts_without_created_at
    if missing:
        notes.append(
            f"{missing} accounts carry no createdTimestamp, so the reassignment check could not "
            "run for them. That is missing evidence, not evidence of safety."
        )
    return notes


def load_directory_from_keycloak(client, *, page_size: int = 200, max_users: int = 200_000) -> DirectoryLoad:
    """Page the whole Keycloak user list through the existing read-only client.

    The client already holds ``query-users``/``view-users`` for the member
    picker; this adds no permission. Paging is explicit because Keycloak's
    ``/users`` defaults to a small page and a truncated directory would turn
    real users into "unmapped principals" — a report that under-reports coverage
    is worse than no report.
    """

    users: list[DirectoryUser] = []
    notes: list[str] = []
    first = 0
    while first < max_users:
        page = client.list_user_records_page(first=first, limit=page_size)
        if not page:
            break
        for record in page:
            user = user_from_keycloak_record(record)
            if user is not None:
                users.append(user)
        if len(page) < page_size:
            break
        first += page_size
    else:  # pragma: no cover - only reachable on an implausibly large realm
        notes.append(f"Stopped after {max_users} users; the directory may be incomplete.")
    index = DirectoryIndex(users)
    return DirectoryLoad(index=index, source="keycloak", notes=_with_created_at_note(index, notes))


def load_directory_from_json(payload: object) -> DirectoryLoad:
    """Build a directory from an exported user list.

    Accepts Keycloak's own ``/admin/realms/<realm>/users`` JSON (``id``,
    ``username``, ``email``, ``enabled``, ``createdTimestamp``) so an operator
    can run the mapping against an export when the tool cannot reach Keycloak
    directly — a common constraint when the hub's admin API is not routable from
    wherever the report is being reviewed.
    """

    if not isinstance(payload, list):
        raise ValueError("The directory export must be a JSON list of Keycloak user objects")
    users = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        user = user_from_keycloak_record(item)
        if user is not None:
            users.append(user)
    index = DirectoryIndex(users)
    return DirectoryLoad(index=index, source="json-export", notes=_with_created_at_note(index, []))
