"""Read-only access primitives: they refuse to write rather than declining to.

Each backend gets the strongest refusal that backend can express:

* Postgres refuses server-side (``default_transaction_read_only``), verified
  before the first query. **This is the guarantee.** The ``SELECT``-only guard
  in :func:`_require_select` sits in front of it as defence in depth — an
  earlier, clearer error — and is not claimed as an independent layer.
* S3 refuses in botocore's own dispatch (``before-call.s3``), which is the one
  place every call path — direct calls, paginators, future code — must pass
  through.
* The local filesystem has no such gate, so the reader simply never opens a
  file for writing and never creates a directory. The one filesystem write
  Python performs on its own is bytecode caching, which the image and the
  documented invocation disable (``PYTHONDONTWRITEBYTECODE`` / ``python -B``).

None of this replaces least-privilege credentials; it bounds the damage a bug in
this package could do with credentials more privileged than they should be.
"""

from __future__ import annotations

import errno
import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

# Word-boundary matched so column names survive: `created_at` is not CREATE and
# `updated_at` is not UPDATE.
_DATA_MODIFYING = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|DROP|ALTER|CREATE|GRANT|REVOKE|COPY|CALL|DO|VACUUM|REFRESH)\b",
    re.IGNORECASE,
)

# S3 operations the inventory is allowed to issue. Deliberately a *positive*
# allowlist: a denylist of write operations would silently admit whatever AWS
# adds next.
READ_ONLY_S3_OPERATIONS = frozenset(
    {
        "GetObject",
        "HeadObject",
        "HeadBucket",
        "ListObjects",
        "ListObjectsV2",
        "ListBuckets",
    }
)


class ReadOnlyViolationError(RuntimeError):
    """Raised when the inventory attempts an operation that could mutate data.

    Reaching this is a bug in this package, not an operator error. It is raised
    rather than logged so a dry run that tried to write fails loudly and
    incompletely instead of producing a report that looks fine.
    """


class UnsafePathError(RuntimeError):
    """Raised when a filesystem path could escape or redirect the scan."""


def open_no_follow(path: str | Path, flags: int, mode: int = 0o600, *, dir_fd: int | None = None) -> int:
    """Open one path without following a final symlink.

    The inventory reads production sidecars and writes a PII-bearing report.
    Following a replaced sidecar can read outside the configured store; following
    a report symlink can truncate an unrelated file. ``O_NOFOLLOW`` closes both
    final-component races before the read or truncate side effect occurs.
    """

    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if not no_follow:  # pragma: no cover - the deployed Linux image supports it
        raise UnsafePathError("this platform cannot safely refuse filesystem symlinks")
    try:
        return os.open(
            os.fspath(path),
            flags | no_follow | getattr(os, "O_CLOEXEC", 0),
            mode,
            dir_fd=dir_fd,
        )
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            raise UnsafePathError(f"refusing symlink path: {path}") from exc
        raise


def redact_database_url(database_url: str) -> str:
    """Return a database URL safe to print: host, port, and database only.

    Connection URLs carry a password. The report and every error message pass
    through here, so no code path prints a credential even when psycopg raises
    with the URL attached.
    """

    if not database_url:
        return "(not configured)"
    try:
        parts = urlsplit(database_url)
    except ValueError:
        return "(unparseable database URL)"
    if not parts.scheme:
        return "(redacted database URL)"
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


class ReadOnlyPostgres:
    """A verified read-only Postgres session that only runs ``SELECT``.

    The read-only property is established three ways so that no single mistake
    removes it: the connection option makes the *server* refuse writes, the
    startup check proves the option took effect on this server, and
    :meth:`rows` rejects any statement that is not a ``SELECT`` before it is
    sent.
    """

    def __init__(self, connection):
        self._connection = connection

    @property
    def connection(self):
        return self._connection

    def verify_read_only(self) -> str:
        """Confirm the session is read-only, or raise before any data is read."""

        row = self._connection.execute("SHOW transaction_read_only").fetchone()
        value = _first_value(row)
        if value != "on":
            raise ReadOnlyViolationError(
                "Refusing to scan: the Postgres session is not read-only "
                f"(transaction_read_only={value!r}). The inventory only ever runs "
                "with default_transaction_read_only=on."
            )
        return value

    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        """Run one ``SELECT`` and return every row as a dict."""

        _require_select(sql)
        return self._connection.execute(sql, params).fetchall()

    def iter_rows(self, sql: str, params: tuple = (), *, batch_size: int = 1000) -> Iterator[dict]:
        """Stream one ``SELECT`` through a server-side cursor.

        History and usage-event tables are unbounded in a way the frame and
        group tables are not, and an inventory that only works on a small hub is
        not an inventory. Rows arrive in batches instead of one list.
        """

        _require_select(sql)
        with self._connection.cursor(name="identity_inventory") as cursor:
            cursor.itersize = batch_size
            cursor.execute(sql, params)
            yield from cursor

    def table_exists(self, table_name: str) -> bool:
        """Return whether a table is present, so a missing feature is reported not fatal.

        A hub that never enabled tasks has no ``nexus_task_state``. That is a
        coverage note in the report, not a crash — but it must be *stated*,
        because "no rows found" and "table absent" mean very different things to
        someone deciding whether to migrate.
        """

        row = self._connection.execute("SELECT to_regclass(%s) AS reg", (table_name,)).fetchone()
        return _first_value(row) is not None


def _first_value(row) -> object:
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()), None)
    return row[0]


def _require_select(sql: str) -> None:
    """Reject anything that is not a single, non-data-modifying ``SELECT``.

    Honest framing: this is **defence in depth behind the server-side read-only
    transaction**, not an independent guarantee. The transaction is what
    actually stops a write; this catches a bad statement earlier and with a
    clearer error. It is written to hold against the obvious ways a prefix check
    can be walked past — a second statement after a semicolon, and a
    data-modifying CTE wrapped in an outer ``SELECT`` — because a guard that can
    be stepped over invites being trusted as though it could not.
    """

    statement = sql.strip().lstrip("(").lstrip()
    if not statement.upper().startswith("SELECT"):
        raise ReadOnlyViolationError(f"Refusing to run a non-SELECT statement from the inventory: {statement[:60]!r}")
    if ";" in statement.rstrip().rstrip(";"):
        raise ReadOnlyViolationError("Refusing a multi-statement SQL string from the inventory")
    modifying = _DATA_MODIFYING.search(statement)
    if modifying:
        raise ReadOnlyViolationError(
            f"Refusing a SELECT containing the data-modifying keyword {modifying.group(0).upper()!r}"
        )


@contextmanager
def read_only_postgres(database_url: str):
    """Open a verified read-only Postgres session for the duration of the block.

    ``options`` is passed as a connect parameter rather than issued as a
    statement afterwards, so the very first query of the session — including
    anything psycopg itself runs — is already inside a read-only transaction.
    Autocommit is left off; nothing here needs to commit, and an open read
    transaction is the cheapest way to keep the scan internally consistent.
    """

    import psycopg
    from psycopg.rows import dict_row

    connection = psycopg.connect(
        database_url,
        row_factory=dict_row,
        options="-c default_transaction_read_only=on",
    )
    try:
        session = ReadOnlyPostgres(connection)
        session.verify_read_only()
        yield session
    finally:
        connection.close()


def reject_write_operations(model=None, **_kwargs) -> None:
    """botocore ``before-call.s3`` hook: refuse anything outside the read allowlist.

    Registered on the client's own event system, so it applies to paginators
    and to any call made through this client by code that has not been written
    yet — which is the point. A wrapper class alone would only constrain the
    call sites that exist today.
    """

    name = getattr(model, "name", None)
    if name is not None and name not in READ_ONLY_S3_OPERATIONS:
        raise ReadOnlyViolationError(f"Refusing S3 operation {name!r}: the identity inventory is read-only")


class ReadOnlyS3:
    """Minimal read-only view of an S3 bucket holding frame sidecars.

    Exposes exactly the two calls the scan needs. Anything else is not merely
    unused — it is unreachable, because it was never bound.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "frames",
        endpoint_url: str | None = None,
        region_name: str | None = None,
    ):
        try:
            import boto3
            from botocore.config import Config
            from botocore.exceptions import ClientError
        except ImportError as exc:  # pragma: no cover - packaging guard
            raise RuntimeError("The identity inventory needs boto3 to read S3 frame sidecars") from exc

        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.client_error = ClientError
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region_name,
            config=Config(s3={"addressing_style": "path"}) if endpoint_url else None,
        )
        self._client.meta.events.register("before-call.s3", reject_write_operations)

    def iter_metadata_keys(self) -> Iterator[str]:
        """Yield every ``.../metadata.json`` key under the configured prefix."""

        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=f"{self.prefix}/"):
            for item in page.get("Contents", []):
                key = item["Key"]
                if key.endswith("/metadata.json"):
                    yield key

    def get_json(self, key: str) -> dict:
        """Read and parse one JSON object."""

        obj = self._client.get_object(Bucket=self.bucket, Key=key)
        return json.loads(obj["Body"].read().decode("utf-8"))


@dataclass(frozen=True)
class LocalSidecar:
    """One frame metadata sidecar found on a local filesystem."""

    frame_id: str
    path: Path


class ReadOnlyLocalFrames:
    """Read-only view of the local frames directory.

    Never creates the root, never creates a frame directory, never opens a file
    for writing — which is precisely the difference between this and
    ``LocalFsFrameStore``, whose constructor creates the root.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def exists(self) -> bool:
        return self.root.is_dir()

    def iter_sidecars(self) -> Iterator[LocalSidecar]:
        for path in sorted(self.root.iterdir()):
            if path.is_symlink():
                raise UnsafePathError(f"refusing symlink under frames root: {path}")
            if not path.is_dir():
                continue
            sidecar = path / "metadata.json"
            if sidecar.is_symlink():
                raise UnsafePathError(f"refusing symlinked frame sidecar: {sidecar}")
            if sidecar.is_file():
                yield LocalSidecar(frame_id=path.name, path=sidecar)

    def read_json(self, path: Path) -> dict:
        directory = open_no_follow(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            descriptor = open_no_follow(path.name, os.O_RDONLY, dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            return json.load(handle)
