"""Command line entry point: ``python -m collab_hub_api.identity_inventory``.

Configuration is read from the **same environment variables the API pod already
has**, so the ordinary way to run this is inside (or beside) the deployment
being audited, with no new configuration to get wrong:

``COLLAB_HUB_API__FRAMES__STORAGE_BACKEND`` / ``...__STORAGE__FRAMES_PATH`` /
``...__FRAMES__S3__{BUCKET,PREFIX,ENDPOINT_URL,REGION}`` /
``...__FRAMES__POSTGRES__URL`` /
``...__USER_DIRECTORY__KEYCLOAK__{TOKEN_URL,ISSUER_URL,ADMIN_API_BASE_URL,CLIENT_ID,CLIENT_SECRET}``.

Those names are read directly rather than through :mod:`.config`, because
importing the app config imports every store class — including their write
paths — into a process that is supposed to be incapable of writing.

Secrets are accepted **only** from the environment. There is no
``--client-secret`` and no ``--postgres-url`` carrying a password, because
anything on argv is visible in ``ps`` and in shell history on a shared bastion.
For the same reason the database URL is redacted everywhere it is printed.

Exit codes are meaningful, so this can gate a runbook step:

- ``0`` — verdict ``clear``: every source scanned, and every owned record keeps
  an owner whose mapping is *certain*. Nothing else returns 0.
- ``1`` — findings that need a person: blocking orphans, ambiguity, suspected
  reassignment, or entities resting on unverified matches.
- ``2`` — the scan is incomplete or failed: a source was skipped, a table was
  missing, a sidecar could not be read, or the run itself errored. A partial
  scan can never report "clear", because it only ever establishes that nothing
  was found *where it looked*.

Bytecode caching is the one filesystem write Python performs unbidden, so the
documented invocation is ``python -B -m …`` and the API image sets
``PYTHONDONTWRITEBYTECODE=1``. Without one of the two, importing this package
can create ``__pycache__`` directories and falsify the no-writes claim.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .analysis import (
    VERDICT_BLOCKED,
    VERDICT_CLEAR,
    VERDICT_INCOMPLETE,
    VERDICT_NEEDS_CONFIRMATION,
    analyze,
)
from .directory import DirectoryLoad, load_directory_from_json, load_directory_from_keycloak
from .readonly import (
    ReadOnlyLocalFrames,
    ReadOnlyS3,
    ReadOnlyViolationError,
    UnsafePathError,
    open_no_follow,
    read_only_postgres,
    redact_database_url,
)
from .report import render_json, render_markdown
from .scan import ScanResult, SourceCoverage, scan_local_frames, scan_postgres, scan_s3_frames

ENV_PREFIX = "COLLAB_HUB_API__"

EXIT_OK = 0
EXIT_BLOCKING_FINDINGS = 1
EXIT_SCAN_FAILED = 2

VERDICT_EXIT_CODES = {
    VERDICT_CLEAR: EXIT_OK,
    VERDICT_BLOCKED: EXIT_BLOCKING_FINDINGS,
    VERDICT_NEEDS_CONFIRMATION: EXIT_BLOCKING_FINDINGS,
    VERDICT_INCOMPLETE: EXIT_SCAN_FAILED,
}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(f"{ENV_PREFIX}{name}", default)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m collab_hub_api.identity_inventory",
        description=(
            "Read-only identity inventory and dry-run migration mapping report (issue #65). "
            "Writes nothing to Postgres, S3, the filesystem under audit, or Keycloak."
        ),
    )
    parser.add_argument(
        "--frames-backend",
        choices=("auto", "local", "s3", "none"),
        default="auto",
        help="Frame sidecar backend to scan. 'auto' follows FRAMES__STORAGE_BACKEND.",
    )
    parser.add_argument("--frames-path", default="", help="Local frames root (default: STORAGE__FRAMES_PATH).")
    parser.add_argument("--s3-bucket", default="", help="Frames bucket (default: FRAMES__S3__BUCKET).")
    parser.add_argument("--s3-prefix", default="", help="Frames key prefix (default: FRAMES__S3__PREFIX).")
    parser.add_argument("--skip-postgres", action="store_true", help="Do not scan any Postgres carrier.")
    parser.add_argument(
        "--directory-json",
        default="",
        help="Read the Keycloak user list from an exported JSON file instead of the admin API.",
    )
    parser.add_argument("--output", default="", help="Write the Markdown report here (mode 0600) instead of stdout.")
    parser.add_argument("--json-output", default="", help="Also write the machine-readable report here (mode 0600).")
    parser.add_argument(
        "--redact",
        action="store_true",
        help="Replace principals with stable pseudonyms so the report can be circulated.",
    )
    return parser


def load_directory(args) -> DirectoryLoad:
    """Load the Keycloak account list, from the admin API or an export."""

    if args.directory_json:
        payload = json.loads(Path(args.directory_json).read_text(encoding="utf-8"))
        load = load_directory_from_json(payload)
        load.notes.append(f"Loaded from export {args.directory_json}; it may be older than the stores scanned.")
        return load

    from ..user_directory import KeycloakUserDirectoryClient

    issuer = _env("USER_DIRECTORY__KEYCLOAK__ISSUER_URL")
    token_url = _env("USER_DIRECTORY__KEYCLOAK__TOKEN_URL") or (
        f"{issuer.rstrip('/')}/protocol/openid-connect/token" if issuer else ""
    )
    admin_base = _env("USER_DIRECTORY__KEYCLOAK__ADMIN_API_BASE_URL")
    client_id = _env("USER_DIRECTORY__KEYCLOAK__CLIENT_ID")
    client_secret = _env("USER_DIRECTORY__KEYCLOAK__CLIENT_SECRET")
    missing = [
        name
        for name, value in (
            ("TOKEN_URL (or ISSUER_URL)", token_url),
            ("ADMIN_API_BASE_URL", admin_base),
            ("CLIENT_ID", client_id),
            ("CLIENT_SECRET", client_secret),
        )
        if not value
    ]
    if missing:
        raise SystemExit(
            "Keycloak is not configured: missing "
            + ", ".join(f"{ENV_PREFIX}USER_DIRECTORY__KEYCLOAK__{name}" for name in missing)
            + ". Use --directory-json to map against an exported user list instead."
        )

    client = KeycloakUserDirectoryClient(
        token_url=token_url,
        admin_api_base_url=admin_base,
        client_id=client_id,
        client_secret=client_secret,
    )
    try:
        return load_directory_from_keycloak(client)
    finally:
        client.close()


def run_scan(args) -> ScanResult:
    """Scan every configured carrier through read-only accessors."""

    result = ScanResult()

    backend = args.frames_backend
    if backend == "auto":
        backend = _env("FRAMES__STORAGE_BACKEND", "local")
    if backend == "local":
        root = args.frames_path or _env("STORAGE__FRAMES_PATH", "/var/frames")
        scan_local_frames(result, ReadOnlyLocalFrames(root))
    elif backend == "s3":
        bucket = args.s3_bucket or _env("FRAMES__S3__BUCKET")
        if not bucket:
            result.coverage.append(SourceCoverage("frames (S3)", False, "no bucket configured"))
        else:
            scan_s3_frames(
                result,
                ReadOnlyS3(
                    bucket=bucket,
                    prefix=args.s3_prefix or _env("FRAMES__S3__PREFIX", "frames"),
                    endpoint_url=_env("FRAMES__S3__ENDPOINT_URL") or None,
                    region_name=_env("FRAMES__S3__REGION") or None,
                ),
            )
    else:
        result.coverage.append(SourceCoverage("frames (sidecars)", False, f"skipped (backend={backend})"))

    database_url = _env("FRAMES__POSTGRES__URL")
    if args.skip_postgres or not database_url:
        reason = "skipped by --skip-postgres" if args.skip_postgres else "no FRAMES__POSTGRES__URL configured"
        result.coverage.append(SourceCoverage("postgres carriers", False, reason))
    else:
        with read_only_postgres(database_url) as db:
            result.coverage.append(
                SourceCoverage(
                    "postgres session",
                    True,
                    f"{redact_database_url(database_url)} (transaction_read_only=on, verified)",
                )
            )
            scan_postgres(result, db)
    return result


def _write_private(path: str, content: str) -> None:
    """Write a report so only its owner can read it.

    The file lists who owns what on a production hub. It is *created* 0600, so
    it is never briefly world-readable — but the create mode is ignored when the
    file already exists, and re-running over yesterday's 0644 report would
    otherwise leave a world-readable file full of staff emails. So the mode is
    also set explicitly on the open descriptor, which fixes an inherited mode
    without a path race.
    """

    target = Path(path)
    descriptor = open_no_follow(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
    except OSError:  # pragma: no cover - platforms without fchmod semantics
        pass
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        directory = load_directory(args)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"Could not load the Keycloak directory: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_SCAN_FAILED

    try:
        scan = run_scan(args)
    except ReadOnlyViolationError as exc:
        print(f"READ-ONLY VIOLATION, aborting: {exc}", file=sys.stderr)
        return EXIT_SCAN_FAILED
    except Exception as exc:
        print(f"Scan failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_SCAN_FAILED

    analysis = analyze(
        scan,
        directory.index,
        directory_source=directory.source,
        directory_notes=directory.notes,
    )

    markdown = render_markdown(analysis, redact=args.redact)
    try:
        if args.output:
            _write_private(args.output, markdown)
            print(f"Wrote {args.output} (mode 0600)", file=sys.stderr)
        else:
            sys.stdout.write(markdown)
        if args.json_output:
            _write_private(args.json_output, render_json(analysis, redact=args.redact))
            print(f"Wrote {args.json_output} (mode 0600)", file=sys.stderr)
    except (OSError, UnsafePathError) as exc:
        print(f"Could not write the report safely: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_SCAN_FAILED

    print(f"Verdict: {analysis.verdict}", file=sys.stderr)
    for gap in analysis.coverage_gaps:
        print(f"  coverage gap: {gap}", file=sys.stderr)
    return VERDICT_EXIT_CODES[analysis.verdict]


if __name__ == "__main__":  # pragma: no cover - exercised via __main__.py
    raise SystemExit(main())
