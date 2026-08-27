"""Read-only identity inventory and dry-run migration mapping report (issue #65).

Every ACL principal the Frames server persists today is whatever
``FRAMES_AUTH_IDENTITY_CLAIM=legacy`` resolved at write time — in practice a
``preferred_username`` or an ``email``, both mutable, neither with a rename
path. Issue #61 pins new writes to the immutable ``sub``; this package answers
the question that has to be settled **before** anyone flips that switch on a
deployment that already holds data: *if every stored principal were rewritten
to its subject, what would break?*

It answers it without touching anything. The production rewrite is Gate D
(issue #68) and is deliberately not implemented here — not in a flag, not in a
commented-out branch. What ships is a scanner, a mapper, and a report.

**Why read-only is enforced structurally, not by care.** A tool that reads
production ACLs runs with credentials that would let it destroy them, usually
at the hands of an operator who is tired and mid-incident. So the guarantees
are arranged to survive a bug in this package, not merely the absence of one:

* **Postgres** — every connection is opened with
  ``options=-c default_transaction_read_only=on``, and the session is *verified*
  (``SHOW transaction_read_only``) before a single query runs. A stray
  ``INSERT`` is refused by the server, not by us. **That is the guarantee.**
  Statements also pass a ``SELECT``-only guard on the way out, which is defence
  in depth — an earlier and clearer failure — and is not claimed as an
  independent layer. Operators should further supply a role holding only
  ``CONNECT``/``USAGE``/``SELECT`` — see ``docs/frames-operations.md``.
* **S3** — the client is wrapped so only ``get_object``/``list_objects_v2``
  reach boto3, *and* a botocore ``before-call.s3`` hook raises on any operation
  outside the read allowlist, which is what covers paginators and any future
  call path. The documented IAM policy grants only ``s3:GetObject`` and
  ``s3:ListBucket``.
* **Local filesystem** — sidecars are opened read-only and directories are only
  iterated. Note that this is also why the scanner does **not** reuse
  :class:`~collab_hub_api.frames.store.LocalFsFrameStore`: its constructor
  calls ``mkdir(parents=True, exist_ok=True)``, so merely instantiating the
  app's own store is already a write. The one filesystem write Python performs
  unbidden is bytecode caching, so the API image sets
  ``PYTHONDONTWRITEBYTECODE=1`` and the documented invocation is ``python -B -m
  …``; without one of the two, importing this package could create
  ``__pycache__`` directories and falsify the claim.
* **Import graph** — this package imports no module that can write: not
  ``frames.store``/``groups``/``history``/``usage``/``active_state``, not
  ``tasks.store``, not ``config`` (which imports all of them). The only
  first-party import is the GET-only
  :class:`~collab_hub_api.user_directory.KeycloakUserDirectoryClient`. A test
  imports the package in a clean interpreter and asserts the banned modules are
  absent from ``sys.modules``, so the property is checked rather than asserted.

**Why raw JSON instead of the app's models.** The scanner parses frame sidecars
with :mod:`json`, not :class:`~collab_hub_api.frames.models.Frame`. A record
that fails validation — a legacy ``owner`` scalar, a tag that no longer passes
``TAG_PATTERN``, a reader list on an ``internal`` frame — is exactly the record
an audit must not skip, and pydantic would either reject it or silently repair
it on the way in. The audit reports what is *stored*.

**Unmapped principals are never altered and never dropped.** ``readers`` are
arbitrary, unvalidated, self-asserted strings: an email typed into a share box
that never matched a Keycloak account still expresses an intent to grant, and
deleting it during a migration silently revokes access nobody agreed to revoke.
Unmapped principals are surfaced, counted, and located, and that is all. Nor is
anything *repaired*: a principal stored with surrounding whitespace matches no
caller today, so trimming it to reach an account would invent access rather
than describe it.

**A mapping is only as good as its evidence.** Email and username matches are
labelled ``unverified`` and can never, alone, declare an entity safe — those
claims are mutable and reusable, so a unique match against today's directory
does not establish who the string meant when it was written. Only a stored
value that is already a subject is ``certain``. See :mod:`.directory` for the
reassignment case this exists to catch, and :mod:`.analysis` for how confidence
and scan coverage together produce the verdict.

The carriers scanned are enumerated in :data:`~.scan.CARRIERS`: **eighteen
places the service persists an identity**, plus one catch-all sweep row for
anything a named-field read would miss. Between them, issues #65 and #61 named
ten. Each row carries that provenance, because a carrier no issue listed is a
carrier a migration plan would have missed — and one of them,
``nexus_task_devices.payload``, was missed by the first draft of this very
tool, which is why every JSON document is now swept whole rather than read
field by field.
"""

from __future__ import annotations

from .analysis import (
    VERDICT_BLOCKED,
    VERDICT_CLEAR,
    VERDICT_INCOMPLETE,
    VERDICT_NEEDS_CONFIRMATION,
    InventoryAnalysis,
    OrphanFinding,
    PrincipalSummary,
    analyze,
)
from .directory import (
    DirectoryIndex,
    DirectoryUser,
    MappingConfidence,
    Resolution,
    ResolutionStatus,
)
from .readonly import ReadOnlyViolationError, redact_database_url
from .report import render_json, render_markdown
from .scan import CARRIERS, Carrier, FrameRecord, GroupRecord, Occurrence, ScanResult

__all__ = [
    "CARRIERS",
    "VERDICT_BLOCKED",
    "VERDICT_CLEAR",
    "VERDICT_INCOMPLETE",
    "VERDICT_NEEDS_CONFIRMATION",
    "Carrier",
    "DirectoryIndex",
    "DirectoryUser",
    "FrameRecord",
    "GroupRecord",
    "InventoryAnalysis",
    "MappingConfidence",
    "Occurrence",
    "OrphanFinding",
    "PrincipalSummary",
    "ReadOnlyViolationError",
    "Resolution",
    "ResolutionStatus",
    "ScanResult",
    "analyze",
    "redact_database_url",
    "render_json",
    "render_markdown",
]
