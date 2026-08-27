"""Mapping, orphan detection, and the dry-run verdict.

The orphan check is the reason the report exists. Everything else is context
for it.

**The check.** After a migration, a principal that did not map is still stored
verbatim and therefore matches no caller's subject. So an entity is reachable
afterwards exactly when at least one of its owners maps to a live subject. A
frame whose owners *all* fail to map still exists, still occupies storage, and
still answers ``GET`` for nobody: no one can publish it, rename it, add an
owner, or delete it. That is unrecoverable through the API — recovering it needs
another data edit — which is why it has to be found and decided on *before*
Gate D rather than discovered by the person who lost the frame.

**Everything with an owner list is checked**, not only frames and groups.
``nexus_task_state`` is keyed by the same principal and every task endpoint
scopes access by it, so a row whose owner does not map is a user whose entire
task list becomes unreachable. Scanning a carrier, calling it ACL-bearing, and
then leaving it out of the verdict would be its own kind of dishonesty.

**Confidence changes what a mapping is allowed to conclude.** An email or
username match is a *proposal*: those claims are mutable and reusable, so a
unique match against today's directory does not establish that the string meant
this account when it was written (see :mod:`.directory`). An entity resting only
on such matches is neither "clear" nor "orphaned" — it is
:data:`ORPHAN_UNVERIFIED_ONLY`, *needs human confirmation*, a distinct outcome
with a distinct fix: somebody has to say yes. Only a stored value that is
already a subject clears an entity by itself.

Where the directory offers evidence *against* a mapping — the account was
created after the record was last written, so it cannot be the account that
principal referred to — the mapping is discarded rather than downgraded, and the
entity is reported as :data:`ORPHAN_REASSIGNED`. That is the fingerprint of a
reassigned address, and it is precisely the case that would otherwise hand one
person's content to another.

**The verdict is gated on coverage.** A scan that skipped a source, hit an
unreadable sidecar, or found a table missing cannot support "no blocking
findings"; it can only support "nothing found where it looked". Any gap or error
makes the verdict :data:`VERDICT_INCOMPLETE`, whatever the findings say.

**Non-owner carriers never orphan anything.** An unmapped ``readers`` entry, a
history ``actor``, a usage row: these lose their link to a person, which is a
real (and reported) loss of provenance, but they cannot make an entity
unmanageable. The report separates the two so that a long unmapped-readers list
does not look like an emergency and a single orphaned frame does not get lost
inside it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .directory import DirectoryIndex, DirectoryUser, MappingConfidence, Resolution, ResolutionStatus
from .scan import CARRIERS_BY_ID, Occurrence, OwnedRecord, ScanResult


@dataclass
class PrincipalSummary:
    """One distinct stored principal, everywhere it appears and how it resolved."""

    principal: str
    resolution: Resolution
    occurrences: list[Occurrence] = field(default_factory=list)
    sampled_total: int | None = None
    """Set when jsonb locations were sampled rather than listed exhaustively."""

    reassignment_suspected: bool = False
    """The mapped account was created after a record this principal appears in,
    so it cannot be the account the principal referred to when it was written."""

    earliest_record_written_at: datetime | None = None

    @property
    def carriers(self) -> list[str]:
        return sorted({item.carrier for item in self.occurrences})

    @property
    def count(self) -> int:
        return self.sampled_total if self.sampled_total is not None else len(self.occurrences)

    @property
    def confidence(self) -> MappingConfidence:
        if self.reassignment_suspected:
            return MappingConfidence.none
        return self.resolution.confidence

    @property
    def usable_mapping(self) -> bool:
        """Whether a migration may carry this mapping forward at all."""

        return self.resolution.mapped and not self.reassignment_suspected

    @property
    def in_acl_carrier(self) -> bool:
        """Whether this principal grants access anywhere (vs. only provenance)."""

        return any(CARRIERS_BY_ID[item.carrier].acl for item in self.occurrences if item.carrier in CARRIERS_BY_ID)


ORPHAN_NO_OWNERS = "no_owners_recorded"
ORPHAN_UNMAPPED = "no_owner_maps"
ORPHAN_REASSIGNED = "owner_account_newer_than_record"
ORPHAN_DISABLED = "mapping_owners_all_disabled"
ORPHAN_UNVERIFIED_ONLY = "only_unverified_owner_mappings"

BLOCKING_KINDS = frozenset({ORPHAN_NO_OWNERS, ORPHAN_UNMAPPED, ORPHAN_REASSIGNED})

VERDICT_CLEAR = "clear"
VERDICT_NEEDS_CONFIRMATION = "needs_human_confirmation"
VERDICT_BLOCKED = "blocked"
VERDICT_INCOMPLETE = "incomplete_scan"


@dataclass
class OwnerStatus:
    """One owner of one entity, and everything the verdict needed to know."""

    principal: str
    status: str
    confidence: str
    enabled: bool | None
    reassignment_suspected: bool

    @property
    def summary(self) -> str:
        parts = [self.status]
        if self.confidence != MappingConfidence.none.value:
            parts.append(self.confidence)
        if self.enabled is False:
            parts.append("account disabled")
        if self.reassignment_suspected:
            parts.append("ACCOUNT NEWER THAN RECORD")
        return ", ".join(parts)


@dataclass
class OrphanFinding:
    """One entity that is, or may be, unmanageable after the migration."""

    kind: str
    entity_type: str
    entity_id: str
    name: str
    org_id: str
    workspace_id: str
    owners: list[str]
    owner_status: list[OwnerStatus]
    origin: str = ""

    @property
    def blocking(self) -> bool:
        """Whether this finding makes the entity unreachable outright."""

        return self.kind in BLOCKING_KINDS


@dataclass
class InventoryAnalysis:
    """The full dry-run result: what maps, what does not, and what breaks."""

    scan: ScanResult
    directory_size: int
    principals: list[PrincipalSummary] = field(default_factory=list)
    orphans: list[OrphanFinding] = field(default_factory=list)
    unseen_directory_users: list[DirectoryUser] = field(default_factory=list)
    duplicate_directory_emails: list[str] = field(default_factory=list)
    directory_source: str = ""
    directory_notes: list[str] = field(default_factory=list)

    def by_status(self, status: ResolutionStatus) -> list[PrincipalSummary]:
        return [item for item in self.principals if item.resolution.status == status]

    def by_kind(self, kind: str) -> list[OrphanFinding]:
        return [item for item in self.orphans if item.kind == kind]

    @property
    def needs_rewrite(self) -> list[PrincipalSummary]:
        """Principals a migration would change (already-subjects excluded)."""

        return [
            item
            for item in self.principals
            if item.resolution.status in (ResolutionStatus.matched_email, ResolutionStatus.matched_username)
        ]

    @property
    def unmapped(self) -> list[PrincipalSummary]:
        return self.by_status(ResolutionStatus.unmapped)

    @property
    def ambiguous(self) -> list[PrincipalSummary]:
        return self.by_status(ResolutionStatus.ambiguous)

    @property
    def empty(self) -> list[PrincipalSummary]:
        return self.by_status(ResolutionStatus.empty)

    @property
    def padded(self) -> list[PrincipalSummary]:
        """Principals stored with leading/trailing whitespace — reported, never trimmed."""

        return [item for item in self.principals if item.resolution.padded]

    @property
    def reassignment_suspects(self) -> list[PrincipalSummary]:
        return [item for item in self.principals if item.reassignment_suspected]

    @property
    def unverified_mappings(self) -> list[PrincipalSummary]:
        return [item for item in self.principals if item.confidence is MappingConfidence.unverified]

    @property
    def blocking_orphans(self) -> list[OrphanFinding]:
        return [item for item in self.orphans if item.blocking]

    @property
    def coverage_gaps(self) -> list[str]:
        return self.scan.gaps

    @property
    def verdict(self) -> str:
        """The single line the whole report exists to justify.

        Ordered by what must be true first: a scan that did not finish cannot
        clear anything, however clean its findings look.
        """

        if self.coverage_gaps:
            return VERDICT_INCOMPLETE
        if self.blocking_orphans or self.ambiguous:
            return VERDICT_BLOCKED
        if self.orphans or self.reassignment_suspects or self.padded or self.scan.data_notes:
            return VERDICT_NEEDS_CONFIRMATION
        if self.unverified_mappings:
            return VERDICT_NEEDS_CONFIRMATION
        return VERDICT_CLEAR

    @property
    def clear_to_proceed(self) -> bool:
        """True only for :data:`VERDICT_CLEAR`.

        Deliberately strict. Every other verdict names something a person has to
        look at, and a boolean that blurred them would put this tool's whole
        purpose behind a rounding error.
        """

        return self.verdict == VERDICT_CLEAR


def analyze(
    scan: ScanResult,
    index: DirectoryIndex,
    *,
    directory_source: str = "",
    directory_notes=None,
) -> InventoryAnalysis:
    """Map every scanned principal, test the mappings, and run the orphan check."""

    scan.resolve_deferred()

    grouped: dict[str, PrincipalSummary] = {}
    for occurrence in scan.occurrences:
        summary = grouped.get(occurrence.principal)
        if summary is None:
            summary = PrincipalSummary(
                principal=occurrence.principal,
                resolution=index.resolve(occurrence.principal),
            )
            grouped[occurrence.principal] = summary
        summary.occurrences.append(occurrence)

    for value, omitted in scan.sampled_values.items():
        summary = grouped.get(value)
        if summary is not None:
            summary.sampled_total = len(summary.occurrences) + omitted

    _flag_reassignments(grouped, scan)

    principals = sorted(grouped.values(), key=lambda item: item.principal.casefold())

    analysis = InventoryAnalysis(
        scan=scan,
        directory_size=len(index),
        principals=principals,
        directory_source=directory_source,
        directory_notes=list(directory_notes or []),
        duplicate_directory_emails=index.duplicate_emails,
    )

    summaries = {item.principal: item for item in principals}
    for record in scan.owned_records:
        finding = _check_owners(record, summaries, index)
        if finding is not None:
            analysis.orphans.append(finding)

    seen_subs = {item.resolution.sub for item in principals if item.usable_mapping}
    analysis.unseen_directory_users = [user for user in index.users if user.sub not in seen_subs]

    return analysis


def _flag_reassignments(summaries: dict[str, PrincipalSummary], scan: ScanResult) -> None:
    """Mark mappings contradicted by the mapped account's own creation time.

    The comparison is against the *earliest* timestamped record a principal
    appears in. An account created after that record was written did not exist
    when the principal was stored, so it cannot be the account the principal
    named — the fingerprint of an address that was released and later reissued.

    Only records carrying a timestamp participate. Where the data offers none,
    or Keycloak offers no ``createdTimestamp``, the check yields nothing: that
    is absence of evidence, not evidence of absence, and is exactly why an email
    or username match stays *unverified* even when this check passes.
    """

    earliest: dict[str, datetime] = {}
    for record in scan.owned_records:
        if record.written_at is None:
            continue
        for principal in [*record.owners, *([record.created_by] if record.created_by else [])]:
            current = earliest.get(principal)
            if current is None or record.written_at < current:
                earliest[principal] = record.written_at

    for principal, summary in summaries.items():
        written_at = earliest.get(principal)
        summary.earliest_record_written_at = written_at
        user = summary.resolution.user
        if written_at is None or user is None or user.created_at is None:
            continue
        if user.created_at > written_at:
            summary.reassignment_suspected = True


def _check_owners(
    record: OwnedRecord,
    summaries: dict[str, PrincipalSummary],
    index: DirectoryIndex,
) -> OrphanFinding | None:
    statuses: list[OwnerStatus] = []
    certain_live = False
    unverified_live = False
    mapped_disabled = False
    reassigned = False

    for owner in record.owners:
        summary = summaries.get(owner)
        resolution = summary.resolution if summary else index.resolve(owner)
        suspected = bool(summary and summary.reassignment_suspected)
        confidence = summary.confidence if summary else resolution.confidence
        statuses.append(
            OwnerStatus(
                principal=owner,
                status=resolution.status.value,
                confidence=confidence.value,
                enabled=(resolution.user.enabled if resolution.user else None),
                reassignment_suspected=suspected,
            )
        )
        if suspected:
            # Evidence against the mapping, not weak evidence for it.
            reassigned = True
            continue
        if not resolution.mapped:
            continue
        if not resolution.live:
            mapped_disabled = True
            continue
        if resolution.certain:
            certain_live = True
        else:
            unverified_live = True

    if not record.owners:
        kind = ORPHAN_NO_OWNERS
    elif certain_live:
        return None
    elif unverified_live:
        kind = ORPHAN_UNVERIFIED_ONLY
    elif mapped_disabled:
        kind = ORPHAN_DISABLED
    elif reassigned:
        kind = ORPHAN_REASSIGNED
    else:
        kind = ORPHAN_UNMAPPED

    return OrphanFinding(
        kind=kind,
        entity_type=record.entity_type,
        entity_id=record.entity_id,
        name=record.name,
        org_id=record.org_id,
        workspace_id=record.workspace_id,
        owners=list(record.owners),
        owner_status=statuses,
        origin=record.origin,
    )
