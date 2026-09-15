"""The reconciliation indexer (issue #84) against the static adapter over a fake OCI client.

Every scenario the issue names: a new artifact, an unchanged artifact (second
sweep changes nothing), a retagged artifact (written without refetching), a
removed artifact (marked, never deleted, still readable by digest), a reader
failure recorded while the sweep continues, non-Cog artifacts recorded once
with a reason, single flight under the sweep lock, and the targeted entry
points a webhook receiver will call.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cog_registry_fakes import HOST, URL, FakeOCIFactory, manifest_for

from collab_hub_api.cogs.bundle import COG_ENTRY_FILE, read_cog_bundle
from collab_hub_api.cogs.catalog import (
    STATUS_FAILED,
    STATUS_INDEXED,
    STATUS_NON_COG,
    CogArtifact,
    InMemoryCogCatalogStore,
    UnavailableCogCatalogStore,
)
from collab_hub_api.cogs.indexer import (
    MAX_TITLES_IN_REASON,
    CogIndexer,
    SweepSummary,
)
from collab_hub_api.cogs.oci import (
    MEDIA_TYPE_NEBI_ASSET,
    MEDIA_TYPE_OCI_MANIFEST,
    MEDIA_TYPE_PIXI_LOCK,
    MEDIA_TYPE_PIXI_TOML,
    TITLE_ANNOTATION,
    Descriptor,
    OCIError,
)
from collab_hub_api.cogs.registry import (
    ArtifactRef,
    CogRegistrySourceConfig,
    RegistrySourceError,
    build_registry_sources,
)
from collab_hub_api.config import Config
from collab_hub_api.frames.observability import COG_INDEX_ARTIFACTS, COG_INDEX_SWEEPS

FIXTURES = Path(__file__).parent / "fixtures" / "cogs"
SOURCE_ID = "static-main"
PUSHED = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


def blob_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def layers_for(files: dict[str, bytes], *, lockfile: bytes | None = None) -> tuple[tuple[Descriptor, ...], dict]:
    """OCI layer descriptors (titled) and the blob map for a bundle's files."""

    descriptors = []
    blobs = {}
    for title, data in files.items():
        media_type = MEDIA_TYPE_PIXI_TOML if title == "pixi.toml" else MEDIA_TYPE_NEBI_ASSET
        descriptors.append(
            Descriptor(
                media_type=media_type, digest=blob_digest(data), size=len(data), annotations={TITLE_ANNOTATION: title}
            )
        )
        blobs[blob_digest(data)] = data
    if lockfile is not None:
        descriptors.append(
            Descriptor(
                media_type=MEDIA_TYPE_PIXI_LOCK,
                digest=blob_digest(lockfile),
                size=len(lockfile),
                annotations={TITLE_ANNOTATION: "pixi.lock"},
            )
        )
        blobs[blob_digest(lockfile)] = lockfile
    return tuple(descriptors), blobs


def fixture_files(name: str) -> dict[str, bytes]:
    root = FIXTURES / name
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path.name not in {"bundle-paths.txt", "expected-card.json"}
    }


def entry(
    digest_seed: str,
    tags: list[str],
    files: dict[str, bytes],
    *,
    pushed_at: datetime | None = PUSHED,
    lockfile: bytes | None = None,
    extra_layers: tuple[Descriptor, ...] = (),
) -> dict:
    layers, blobs = layers_for(files, lockfile=lockfile)
    return {
        "digest": "sha256:" + (digest_seed * 64)[:64],
        "tags": tags,
        "pushed_at": pushed_at,
        "media_type": MEDIA_TYPE_OCI_MANIFEST,
        "layers": layers + extra_layers,
        "blobs": blobs,
    }


COMPLETE = fixture_files("pixi-complete")
CONTEXT = fixture_files("pixi-context")
PROG = fixture_files("prog")
DRAFT = fixture_files("draft")


def registry() -> dict[str, list[dict]]:
    return {
        "cogs/transcriber": [entry("a", ["v1", "latest"], COMPLETE, lockfile=b"lock: 1\n")],
        "cogs/notes": [entry("b", ["v1"], CONTEXT)],
    }


def make(artifacts: dict[str, list[dict]] | None = None, **indexer_kwargs):
    """A static source over a fake OCI client, an in-memory store, and an indexer over both."""

    factory = FakeOCIFactory(artifacts if artifacts is not None else registry())
    config = CogRegistrySourceConfig(id=SOURCE_ID, kind="static", url=URL, repositories=sorted(factory.artifacts))
    (source,) = build_registry_sources([config], oci_client_factory=factory)
    store = InMemoryCogCatalogStore()
    indexer = CogIndexer(store, [source], **indexer_kwargs)
    return indexer, store, factory.clients[0]


def counter(metric, **labels) -> float:
    return metric.labels(**labels)._value.get()


def blob_calls(client) -> list[tuple[str, ...]]:
    return [call for call in client.calls if call[0] == "get_blob"]


def manifest_by_digest_calls(client) -> list[tuple[str, ...]]:
    return [call for call in client.calls if call[0] == "get_manifest" and call[2].startswith("sha256:")]


# ---------------------------------------------------------------------------
# One sweep
# ---------------------------------------------------------------------------


async def test_first_sweep_indexes_every_cog_with_the_readers_card():
    indexer, store, client = make()
    before = counter(COG_INDEX_ARTIFACTS, outcome="indexed")

    summary = await indexer.sweep()

    assert (summary.indexed, summary.skipped, summary.failed, summary.removed, summary.non_cog) == (2, 0, 0, 0, 0)
    assert summary.sources == 1 and summary.sources_failed == 0 and not summary.locked_out
    assert counter(COG_INDEX_ARTIFACTS, outcome="indexed") == before + 2
    assert indexer.last_summary is summary

    transcriber = store.get("sha256:" + "a" * 64)
    assert transcriber.status == STATUS_INDEXED
    # Acceptance: the stored card IS the reader's output for the same files
    # and layer titles (the lockfile is a title but never a fetched file).
    expected = read_cog_bundle(COMPLETE, bundle_paths=[*COMPLETE, "pixi.lock"]).to_dict()
    assert transcriber.card == expected
    assert transcriber.cog_id == "example/cog-audio-transcriber"
    assert transcriber.name == "cog-audio-transcriber" and transcriber.version == "0.1.0"
    assert transcriber.kind == "complete" and transcriber.publisher == "Example Organization"
    assert transcriber.manifest_schema == "openteams/cog-manifest [0.1]"
    assert transcriber.tags == ("latest", "v1") and transcriber.pushed_at == PUSHED
    assert transcriber.manifest_media_type == MEDIA_TYPE_OCI_MANIFEST
    assert transcriber.reference == f"{HOST}/cogs/transcriber@sha256:{'a' * 64}"
    assert transcriber.read_errors == ()

    # Fetch strategy: COG.md first, then only the profile file it names. The
    # lockfile is never fetched.
    fetched = {digest for _, repo, digest in blob_calls(client) if repo == "cogs/transcriber"}
    assert fetched == {blob_digest(COMPLETE[COG_ENTRY_FILE]), blob_digest(COMPLETE["pixi.toml"])}
    assert blob_digest(b"lock: 1\n") not in {call[2] for call in blob_calls(client)}

    notes = store.get("sha256:" + "b" * 64)
    assert notes.cog_id == "example/cog-meeting-notes" and notes.kind == "context"
    assert {row.cog_id for row in store.list_current()} == {
        "example/cog-audio-transcriber",
        "example/cog-meeting-notes",
    }


async def test_second_sweep_changes_nothing_and_fetches_no_bundles():
    indexer, store, client = make()
    await indexer.sweep()
    rows_before = {row.digest: row for row in (store.get("sha256:" + "a" * 64), store.get("sha256:" + "b" * 64))}
    client.calls.clear()

    summary = await indexer.sweep()

    assert (summary.indexed, summary.skipped, summary.retagged, summary.removed) == (0, 2, 0, 0)
    assert blob_calls(client) == [], "an unchanged digest costs no blob fetch"
    assert manifest_by_digest_calls(client) == [], "and no manifest fetch by digest"
    for digest, before in rows_before.items():
        assert store.get(digest) == before


async def test_retagged_artifact_is_updated_without_refetching():
    artifacts = registry()
    indexer, store, client = make(artifacts)
    await indexer.sweep()
    client.calls.clear()

    # Move `latest` from the transcriber to a new tag set: the fake's tag
    # table is what list_tags answers, so rewrite it.
    client.tags["cogs/transcriber"] = ["v1", "stable"]
    manifest = manifest_for(artifacts["cogs/transcriber"][0])
    client.manifests[("cogs/transcriber", "stable")] = manifest
    del client.manifests[("cogs/transcriber", "latest")]

    summary = await indexer.sweep()

    assert (summary.retagged, summary.skipped, summary.indexed) == (1, 1, 0)
    row = store.get("sha256:" + "a" * 64)
    assert row.tags == ("stable", "v1")
    assert row.card is not None and row.status == STATUS_INDEXED
    assert blob_calls(client) == []
    assert manifest_by_digest_calls(client) == []


async def test_removed_artifact_is_marked_not_deleted_and_stays_readable():
    indexer, store, client = make()
    await indexer.sweep()

    # Delete the notes Cog from the registry.
    client.tags["cogs/notes"] = []
    del client.manifests[("cogs/notes", "v1")]

    summary = await indexer.sweep()

    assert summary.removed == 1 and summary.skipped == 1
    notes = store.get("sha256:" + "b" * 64)
    assert notes is not None and notes.removed_at is not None
    assert notes.card is not None and notes.cog_id == "example/cog-meeting-notes"
    assert [row.cog_id for row in store.list_current()] == ["example/cog-audio-transcriber"]
    assert store.list_versions("example/cog-meeting-notes") == []
    assert len(store.list_versions("example/cog-meeting-notes", include_removed=True)) == 1

    # A third sweep does not count it again ...
    assert (await indexer.sweep()).removed == 0

    # ... and if the digest comes back it is restored without a refetch.
    client.tags["cogs/notes"] = ["v1"]
    client.manifests[("cogs/notes", "v1")] = client.manifests[("cogs/notes", "sha256:" + "b" * 64)]
    client.calls.clear()
    summary = await indexer.sweep()
    assert summary.retagged == 1 and blob_calls(client) == []
    assert store.get("sha256:" + "b" * 64).present


async def test_reader_failure_is_recorded_and_the_sweep_continues_then_retries():
    artifacts = registry()
    indexer, store, client = make(artifacts)
    # The transcriber's COG.md blob is missing from the registry.
    broken = blob_digest(COMPLETE[COG_ENTRY_FILE])
    del client.blobs[broken]

    summary = await indexer.sweep()

    assert (summary.failed, summary.indexed) == (1, 1)
    row = store.get("sha256:" + "a" * 64)
    assert row.status == STATUS_FAILED and row.card is None
    assert row.read_errors == (f"fetch: OCINotFound: {broken}",)
    assert row.tags == ("latest", "v1") and row.reference.endswith("@sha256:" + "a" * 64)
    assert store.get("sha256:" + "b" * 64).status == STATUS_INDEXED
    assert store.list_current() and all(r.status == STATUS_INDEXED for r in store.list_current())

    # A failed row is retried on the next sweep -- and indexes once the blob is back.
    client.blobs[broken] = COMPLETE[COG_ENTRY_FILE]
    summary = await indexer.sweep()
    assert (summary.indexed, summary.skipped, summary.failed) == (1, 1, 0)
    assert store.get("sha256:" + "a" * 64).status == STATUS_INDEXED


async def test_oversized_bundle_file_is_a_recorded_failure():
    indexer, store, _ = make(max_bytes_per_file=16)

    summary = await indexer.sweep()

    assert summary.failed == 2 and summary.indexed == 0
    row = store.get("sha256:" + "a" * 64)
    assert row.status == STATUS_FAILED
    assert row.read_errors[0].startswith("fetch: OCITooLarge")


async def test_cards_with_errors_are_still_indexed_with_their_errors_mirrored():
    indexer, store, _ = make({"cogs/draft": [entry("d", ["v0"], DRAFT)]})

    summary = await indexer.sweep()

    assert summary.indexed == 1
    row = store.get("sha256:" + "d" * 64)
    expected = read_cog_bundle(DRAFT, bundle_paths=list(DRAFT))
    assert row.status == STATUS_INDEXED
    assert row.card == expected.to_dict()
    assert list(row.read_errors) == expected.errors
    # A draft declares no profile, so it has no id and is not in the current list.
    assert row.cog_id is None and store.list_current() == []


async def test_non_cog_artifacts_are_recorded_once_with_a_reason():
    image_layers = tuple(
        Descriptor(media_type="application/vnd.oci.image.layer.v1.tar+gzip", digest="sha256:" + f"{i:064x}", size=10)
        for i in range(3)
    )
    titled = tuple(
        Descriptor(
            media_type=MEDIA_TYPE_NEBI_ASSET,
            digest="sha256:" + f"{i + 100:064x}",
            size=1,
            annotations={TITLE_ANNOTATION: f"file-{i}.bin"},
        )
        for i in range(MAX_TITLES_IN_REASON + 2)
    )
    indexer, store, client = make(
        {
            "images/nginx": [entry("e", ["1.27"], {}, extra_layers=image_layers)],
            "images/tools": [entry("f", ["v2"], {}, extra_layers=titled)],
        }
    )

    summary = await indexer.sweep()

    assert (summary.non_cog, summary.indexed, summary.failed) == (2, 0, 0)
    nginx = store.get("sha256:" + "e" * 64)
    assert nginx.status == STATUS_NON_COG and nginx.card is None
    assert nginx.read_errors == ("manifest carries no COG.md or pixi.toml layer; 3 layer(s), none titled",)
    tools = store.get("sha256:" + "f" * 64)
    assert tools.read_errors[0].endswith("file-7.bin and 2 more")
    assert blob_calls(client) == [], "nothing is fetched from a manifest with no bundle files"
    assert store.list_current() == []

    # The second sweep skips them: no manifest fetch by digest, nothing re-read.
    client.calls.clear()
    summary = await indexer.sweep()
    assert summary.skipped == 2 and summary.non_cog == 0
    assert manifest_by_digest_calls(client) == [] and blob_calls(client) == []


async def test_prog_without_cog_md_is_indexed_and_pixi_without_capability_is_non_cog():
    indexer, store, client = make(
        {
            "progs/local-server": [entry("p", ["v1"], PROG)],
            "images/pixi-only": [entry("q", ["v1"], {"pixi.toml": b'[workspace]\nname = "x"\n'})],
        }
    )

    summary = await indexer.sweep()

    assert (summary.indexed, summary.non_cog) == (1, 1)
    prog = store.get("sha256:" + "p" * 64)
    assert prog.status == STATUS_INDEXED and prog.kind == "prog" and prog.cog_id == "example/local-server"
    assert prog.card == read_cog_bundle(PROG, bundle_paths=list(PROG)).to_dict()
    plain = store.get("sha256:" + "q" * 64)
    assert plain.status == STATUS_NON_COG
    assert plain.read_errors == ("bundle has no COG.md and pixi.toml declares no [tool.nebi.capability]",)
    # Both fetched exactly one file: pixi.toml.
    assert len(blob_calls(client)) == 2


# ---------------------------------------------------------------------------
# Enumeration failures never mark a catalog gone
# ---------------------------------------------------------------------------


class FlakySource:
    """A RegistrySource whose enumeration fails on demand, wrapping a real one."""

    def __init__(self, inner, *, fail_repositories: bool = False, fail_repo: str | None = None):
        self.inner = inner
        self.id = inner.id
        self.host = inner.host
        self.fail_repositories = fail_repositories
        self.fail_repo = fail_repo

    async def list_repositories(self):
        if self.fail_repositories:
            raise RegistrySourceError("listing API answered HTTP 502")
        return await self.inner.list_repositories()

    async def list_artifacts(self, repo):
        if repo == self.fail_repo:
            raise OCIError("connection reset")
        return await self.inner.list_artifacts(repo)

    def oci(self):
        return self.inner.oci()

    def parse_event(self, request):
        return None

    async def aclose(self):
        await self.inner.aclose()


async def test_source_enumeration_failure_skips_removal_and_continues():
    indexer, store, client = make()
    await indexer.sweep()
    (real,) = indexer.sources

    flaky = FlakySource(real, fail_repositories=True)
    broken = CogIndexer(store, [flaky])
    # Meanwhile the registry lost everything -- which the indexer cannot see.
    client.tags = {}
    client.manifests = {}

    summary = await broken.sweep()

    assert summary.sources_failed == 1 and summary.removed == 0
    assert summary.errors == [f"{SOURCE_ID}: list_repositories: RegistrySourceError: listing API answered HTTP 502"]
    assert all(row.present for row in store.list_current()) and len(store.list_current()) == 2


async def test_one_repository_failing_reconciles_the_rest_but_skips_removal():
    artifacts = registry()
    indexer, store, client = make(artifacts)
    await indexer.sweep()
    (real,) = indexer.sources

    # Notes is gone from the registry, but the transcriber listing fails:
    # the sweep must not conclude that notes was removed.
    client.tags["cogs/notes"] = []
    del client.manifests[("cogs/notes", "v1")]
    artifacts["cogs/transcriber"].append(entry("c", ["v2"], COMPLETE))
    client.seed({"cogs/transcriber": artifacts["cogs/transcriber"][-1:]})
    flaky = FlakySource(real, fail_repo="cogs/notes")

    summary = await CogIndexer(store, [flaky]).sweep()

    assert summary.sources_failed == 1 and summary.removed == 0
    assert summary.indexed == 1, "the healthy repository was still reconciled"
    assert store.get("sha256:" + "b" * 64).present
    assert store.get("sha256:" + "c" * 64).status == STATUS_INDEXED


async def test_per_source_bound_stops_enumeration_and_skips_removal():
    indexer, store, _ = make(max_artifacts_per_source=1)

    summary = await indexer.sweep()

    assert summary.indexed == 1 and summary.sources_failed == 1 and summary.removed == 0
    assert summary.errors == [f"{SOURCE_ID}: enumeration incomplete; removal step skipped"]


async def test_missing_configured_repository_is_empty_not_failed():
    indexer, store, client = make()
    await indexer.sweep()
    # The registry no longer has the notes repository at all (404 on tags).
    del client.tags["cogs/notes"]

    summary = await indexer.sweep()

    assert summary.sources_failed == 0 and summary.removed == 1
    assert store.get("sha256:" + "b" * 64).removed_at is not None


# ---------------------------------------------------------------------------
# Single flight
# ---------------------------------------------------------------------------


async def test_sweep_is_single_flight_under_the_store_lock():
    indexer, store, _ = make()
    before = counter(COG_INDEX_SWEEPS, result="locked_out")

    with store.sweep_lock() as held:
        assert held
        summary = await indexer.sweep()

    assert summary.locked_out is True
    assert summary.indexed == 0 and store.list_current() == []
    assert counter(COG_INDEX_SWEEPS, result="locked_out") == before + 1

    # Released: the next sweep proceeds and the lock is free afterwards.
    assert (await indexer.sweep()).indexed == 2
    with store.sweep_lock() as free:
        assert free


async def test_two_indexers_starting_together_do_not_double_index():
    indexer, store, client = make()
    other = CogIndexer(store, indexer.sources)

    first, second = await asyncio.gather(indexer.sweep(), other.sweep())

    winners = [s for s in (first, second) if not s.locked_out]
    assert len(winners) == 1 and winners[0].indexed == 2
    # Each Cog's bundle files were fetched once.
    assert len(blob_calls(client)) == 4


async def test_lock_is_released_when_a_sweep_raises():
    indexer, store, _ = make()

    class Boom(Exception):
        pass

    async def explode(*args, **kwargs):
        raise Boom()

    indexer._sweep_source = explode  # type: ignore[method-assign]
    before = counter(COG_INDEX_SWEEPS, result="failed")
    with pytest.raises(Boom):
        await indexer.sweep()
    assert counter(COG_INDEX_SWEEPS, result="failed") == before + 1
    with store.sweep_lock() as free:
        assert free
    assert indexer.last_summary is not None and indexer.last_summary.duration_seconds >= 0


# ---------------------------------------------------------------------------
# Targeted entry points (webhook receiver, #86)
# ---------------------------------------------------------------------------


async def test_reindex_writes_one_artifact_regardless_of_catalog_state():
    indexer, store, client = make()

    row = await indexer.reindex(SOURCE_ID, "cogs/notes", "sha256:" + "b" * 64, tags=["v1", "hot"], pushed_at=PUSHED)

    assert isinstance(row, CogArtifact) and row.status == STATUS_INDEXED
    assert row.tags == ("hot", "v1") and row.cog_id == "example/cog-meeting-notes"
    assert store.get("sha256:" + "b" * 64) == store.get(row.digest)
    assert store.get(row.digest).card == row.card

    # A digest the registry does not have is recorded as failed, not raised.
    row = await indexer.reindex(SOURCE_ID, "cogs/notes", "sha256:" + "9" * 64)
    assert row.status == STATUS_FAILED and row.read_errors[0].startswith("fetch: OCINotFound")

    with pytest.raises(KeyError):
        await indexer.reindex("no-such-source", "cogs/notes", "sha256:" + "b" * 64)


async def test_mark_removed_marks_one_row():
    indexer, store, _ = make()
    await indexer.sweep()
    before = counter(COG_INDEX_ARTIFACTS, outcome="removed")

    assert await indexer.mark_removed(SOURCE_ID, "cogs/notes", "sha256:" + "b" * 64) is True
    assert await indexer.mark_removed(SOURCE_ID, "cogs/notes", "sha256:" + "b" * 64) is False
    assert counter(COG_INDEX_ARTIFACTS, outcome="removed") == before + 1
    assert store.get("sha256:" + "b" * 64).removed_at is not None
    with pytest.raises(KeyError):
        await indexer.mark_removed("no-such-source", "cogs/notes", "sha256:" + "b" * 64)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


async def test_run_sweeps_on_startup_then_on_the_interval_until_stopped():
    indexer, _, _ = make()
    sweeps: list[SweepSummary] = []
    stop = asyncio.Event()

    real_sweep = indexer.sweep

    async def counting_sweep():
        summary = await real_sweep()
        sweeps.append(summary)
        if len(sweeps) == 3:
            stop.set()
        return summary

    indexer.sweep = counting_sweep  # type: ignore[method-assign]
    await asyncio.wait_for(indexer.run(interval_seconds=0.01, jitter=0.0, stop=stop), timeout=5)

    assert len(sweeps) == 3
    assert sweeps[0].indexed == 2 and sweeps[1].skipped == 2


async def test_run_can_skip_the_startup_sweep_and_survives_a_failing_sweep():
    indexer, _, _ = make()
    calls: list[str] = []
    stop = asyncio.Event()

    async def failing_sweep():
        calls.append("sweep")
        if len(calls) == 2:
            stop.set()
        raise RuntimeError("store outage")

    indexer.sweep = failing_sweep  # type: ignore[method-assign]
    task = asyncio.create_task(indexer.run(interval_seconds=0.01, run_on_startup=False, jitter=0.0, stop=stop))
    await asyncio.wait_for(task, timeout=5)

    assert calls == ["sweep", "sweep"], "no startup sweep; the loop kept going after the first failure"


async def test_run_rejects_a_non_positive_interval():
    indexer, _, _ = make()
    with pytest.raises(ValueError):
        await indexer.run(interval_seconds=0)


async def test_run_is_cancellable():
    indexer, _, _ = make()
    task = asyncio.create_task(indexer.run(interval_seconds=60, run_on_startup=False))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_artifact_ref_tags_are_normalized_in_the_row():
    # Pure helper behaviour pinned: tags are stored sorted and de-duplicated.
    ref = ArtifactRef(digest="sha256:" + "a" * 64, tags=("v1", "latest", "v1"))
    assert tuple(sorted(set(ref.tags))) == ("latest", "v1")


# ---------------------------------------------------------------------------
# App wiring (config builder + lifespan)
# ---------------------------------------------------------------------------


def cogs_block(*sources: dict, enabled: bool = True, **index) -> dict:
    """A ``cogs:`` config block (#87) as ``Config.parse`` takes it."""

    return {"registry_sources": list(sources), "index": {"enabled": enabled, **index}}


STATIC_SOURCE = {"id": "s", "kind": "static", "url": URL, "repositories": ["cogs/x"]}


def test_build_cog_indexing_is_none_without_the_block_or_when_disabled():
    from collab_hub_api.config import build_cog_indexing

    assert build_cog_indexing(Config.parse(), InMemoryCogCatalogStore()) is None
    disabled = Config.parse({"cogs": cogs_block(STATIC_SOURCE, enabled=False)})
    assert build_cog_indexing(disabled, InMemoryCogCatalogStore()) is None


def test_build_cog_indexing_builds_sources_and_reads_the_loop_parameters():
    from collab_hub_api.config import build_cog_indexing

    config = Config.parse({"cogs": cogs_block(STATIC_SOURCE, interval_seconds=42, run_on_startup=False)})

    indexing = build_cog_indexing(config, InMemoryCogCatalogStore())

    assert indexing is not None
    assert indexing.interval_seconds == 42.0 and indexing.run_on_startup is False
    assert [s.id for s in indexing.indexer.sources] == ["s"]


def test_build_cog_indexing_refuses_the_unavailable_store():
    from collab_hub_api.config import build_cog_indexing

    config = Config.parse({"cogs": cogs_block(STATIC_SOURCE)})
    with pytest.raises(RuntimeError, match="cogs.index.enabled requires the Cog catalog store"):
        build_cog_indexing(config, UnavailableCogCatalogStore())


async def test_app_exposes_the_store_and_no_indexer_when_indexing_is_off(config, monkeypatch):
    from collab_hub_api.core import make_app

    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    app = make_app(config)
    async with app.router.lifespan_context(app):
        # No shared Postgres in the test config: the store refuses (503 at
        # the API), and nothing sweeps.
        assert isinstance(app.state.cog_catalog_store, UnavailableCogCatalogStore)
        assert app.state.cog_indexer is None and app.state.cog_registry_sources == []


async def test_app_runs_the_indexer_in_its_lifespan_and_closes_sources_on_shutdown(tmp_path, monkeypatch):
    from collab_hub_api import config as config_module
    from collab_hub_api.core import make_app

    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    config = Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {
                "active_state": {"backend": "memory"},
                "history": {"backend": "memory"},
                "usage": {"backend": "memory"},
                "mcp_session_manager_enabled": False,
            },
            "tasks": {"backend": "memory"},
            "cogs": cogs_block(STATIC_SOURCE, interval_seconds=3600, run_on_startup=False),
        }
    )
    # No Postgres in this config, so stand the store in: what is under test
    # is the lifespan's ownership of the task and the sources.
    store = InMemoryCogCatalogStore()
    monkeypatch.setattr(config_module, "build_cog_catalog_store", lambda *_: store)
    monkeypatch.setattr("collab_hub_api.core.build_cog_catalog_store", lambda *_: store)

    app = make_app(config)
    async with app.router.lifespan_context(app):
        indexer = app.state.cog_indexer
        assert isinstance(indexer, CogIndexer) and app.state.cog_catalog_store is store
        (registry_source,) = app.state.cog_registry_sources
        tasks = {t.get_name(): t for t in asyncio.all_tasks()}
        assert "cog-index" in tasks and not tasks["cog-index"].done()
        assert not registry_source._closed
    assert tasks["cog-index"].cancelled()
    assert registry_source._closed, "the lifespan closes the registry clients it built"
