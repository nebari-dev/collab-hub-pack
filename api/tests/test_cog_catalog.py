"""The Cog catalog store (issue #84): in-memory contract coverage plus live-Postgres coverage.

Two layers, as for the other ``collab_`` tables (see ``test_collab_schema.py``):

- **Always-on** coverage of the store contract against ``InMemoryCogCatalogStore``,
  including the ``jsonb @>`` containment semantics the in-memory filter mirrors.
- **Opt-in** live coverage (``COLLAB_HUB_TEST_POSTGRES_URL``) where the same
  contract is proven against the real table: upsert/update/removal, tag change
  without a card write, containment filters that genuinely reach the GIN
  index (checked with EXPLAIN), and the session-level sweep lock under real
  concurrent connections.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo

import pytest

from collab_hub_api.cogs.catalog import (
    COG_INDEX_LOCK_KEY,
    MAX_LIST_LIMIT,
    STATUS_FAILED,
    STATUS_INDEXED,
    STATUS_NON_COG,
    CatalogFilter,
    CogArtifact,
    CogCatalogDataError,
    CogCatalogUnavailableError,
    InMemoryCogCatalogStore,
    KnownArtifact,
    PostgresCogCatalogStore,
    UnavailableCogCatalogStore,
    card_search_fields,
    contains_nul,
    json_contains,
)
from collab_hub_api.frames.collab_schema import COLLAB_SCHEMA_LOCK_KEY, run_collab_schema_migrations
from collab_hub_api.frames.db import FRAMES_SERVER_SCHEMA_LOCK_KEY

HOST = "registry.example"
SOURCE = "main"
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def digest(seed: str) -> str:
    return "sha256:" + (seed * 64)[:64]


def card(
    cog_id: str = "example/cog-a",
    *,
    version: str = "1.0.0",
    kind: str = "complete",
    publisher: str | None = "Example Organization",
    provides: list | None = None,
    requires: list[str] = (),
    accepts: list[str] = (),
    produces: list[str] = (),
) -> dict:
    """A card in the reader's shape, with the parts the filters read."""

    return {
        "card": 1,
        "id": cog_id,
        "name": cog_id.rsplit("/", 1)[-1],
        "version": version,
        "kind": kind,
        "publisher": publisher,
        "manifest_schema": "openteams/cog-manifest [0.1]",
        "provides": provides if provides is not None else [],
        "requires": [{"capability": cap, "locality": "any", "satisfiers": []} for cap in requires],
        "io": {"accepts": list(accepts), "produces": list(produces)},
        "errors": [],
    }


def artifact(
    seed: str,
    *,
    repository: str = "cogs/cog-a",
    source_id: str = SOURCE,
    status: str = STATUS_INDEXED,
    tags: tuple[str, ...] = ("v1",),
    pushed_at: datetime | None = T0,
    document: dict | None = None,
    read_errors: tuple[str, ...] = (),
) -> CogArtifact:
    document = document if document is not None else (card() if status == STATUS_INDEXED else None)
    fields = card_search_fields(document) if document else {}
    return CogArtifact(
        source_id=source_id,
        host=HOST,
        repository=repository,
        digest=digest(seed),
        status=status,
        tags=tags,
        pushed_at=pushed_at,
        manifest_media_type="application/vnd.oci.image.manifest.v1+json",
        card=document,
        read_errors=read_errors,
        **fields,
    )


# ---------------------------------------------------------------------------
# Contract, against the in-memory store
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> InMemoryCogCatalogStore:
    return InMemoryCogCatalogStore()


def test_lock_key_is_distinct_from_the_other_advisory_keys():
    assert len({COG_INDEX_LOCK_KEY, COLLAB_SCHEMA_LOCK_KEY, FRAMES_SERVER_SCHEMA_LOCK_KEY}) == 3
    assert COG_INDEX_LOCK_KEY == int.from_bytes(b"cogidx_1", "big")


def test_upsert_then_get_by_digest_returns_the_row_with_its_reference(store):
    row = artifact("a")
    store.upsert(row)

    got = store.get(row.digest)
    assert got is not None
    assert got.card == card()
    assert got.cog_id == "example/cog-a" and got.name == "cog-a" and got.version == "1.0.0"
    assert got.reference == f"{HOST}/cogs/cog-a@{row.digest}"
    assert got.present and got.indexed_at is not None
    assert store.known(SOURCE) == [
        KnownArtifact(repository="cogs/cog-a", digest=row.digest, tags=("v1",), status=STATUS_INDEXED, removed=False)
    ]


def test_upsert_replaces_the_row_whole_and_clears_removal(store):
    first = artifact("a", tags=("v1",))
    store.upsert(first)
    store.mark_removed_one(SOURCE, "cogs/cog-a", first.digest)
    assert not store.get(first.digest).present

    store.upsert(artifact("a", tags=("v1", "latest"), document=card(version="1.0.1")))

    got = store.get(first.digest)
    assert got.present and got.tags == ("latest", "v1") and got.version == "1.0.1"
    assert len(store.locations(first.digest)) == 1


def test_update_tags_changes_tags_only_and_restores_presence(store):
    row = artifact("a", tags=("v1",))
    store.upsert(row)
    store.mark_removed_one(SOURCE, "cogs/cog-a", row.digest)

    assert store.update_tags(SOURCE, "cogs/cog-a", row.digest, ("latest", "v1")) is True
    got = store.get(row.digest)
    assert got.tags == ("latest", "v1") and got.present
    assert got.card == row.card and got.pushed_at == T0

    later = T0 + timedelta(hours=1)
    store.update_tags(SOURCE, "cogs/cog-a", row.digest, ("v1",), pushed_at=later)
    assert store.get(row.digest).pushed_at == later
    assert store.update_tags(SOURCE, "cogs/cog-a", digest("z"), ("v1",)) is False


def test_mark_removed_marks_only_absent_present_rows_and_never_deletes(store):
    keep = artifact("a")
    gone = artifact("b", repository="cogs/cog-b", document=card("example/cog-b"))
    other_source = artifact("c", source_id="other", repository="cogs/cog-c", document=card("example/cog-c"))
    for row in (keep, gone, other_source):
        store.upsert(row)

    assert store.mark_removed(SOURCE, {"cogs/cog-a": [keep.digest]}) == 1
    # Idempotent: already-removed rows are not counted again.
    assert store.mark_removed(SOURCE, {"cogs/cog-a": [keep.digest]}) == 0

    assert store.get(keep.digest).present
    removed = store.get(gone.digest)
    assert removed is not None and removed.removed_at is not None
    assert removed.card == card("example/cog-b"), "a removed row stays readable, card and all"
    assert store.get(other_source.digest).present, "another source's rows are untouched"
    assert {row.digest for row in store.list_current()} == {keep.digest, other_source.digest}
    assert store.list_versions("example/cog-b") == []
    assert [row.digest for row in store.list_versions("example/cog-b", include_removed=True)] == [gone.digest]


def test_mark_removed_one_reports_whether_it_did_anything(store):
    row = artifact("a")
    store.upsert(row)
    assert store.mark_removed_one(SOURCE, "cogs/cog-a", row.digest) is True
    assert store.mark_removed_one(SOURCE, "cogs/cog-a", row.digest) is False
    assert store.mark_removed_one(SOURCE, "cogs/cog-a", digest("z")) is False


def test_same_digest_in_two_repositories_is_two_rows_and_get_prefers_present(store):
    first = artifact("a", repository="cogs/cog-a")
    second = artifact("a", repository="mirror/cog-a-0001")
    store.upsert(first)
    store.upsert(second)

    assert [row.repository for row in store.locations(first.digest)] == ["cogs/cog-a", "mirror/cog-a-0001"]
    store.mark_removed_one(SOURCE, "cogs/cog-a", first.digest)
    assert store.get(first.digest).repository == "mirror/cog-a-0001"
    assert store.get(first.digest, repository="cogs/cog-a").removed_at is not None
    assert store.get(first.digest, source_id="nope") is None
    # One cog_id, one entry in the current list even across two locations.
    assert len(store.list_current()) == 1


def test_list_current_collapses_to_the_newest_per_cog_id(store):
    old = artifact("1", tags=("v1",), pushed_at=T0, document=card(version="1.0.0"))
    new = artifact("2", tags=("v2",), pushed_at=T0 + timedelta(days=1), document=card(version="2.0.0"))
    undated = artifact("3", tags=("dev",), pushed_at=None, document=card(version="3.0.0-dev"))
    for row in (undated, new, old):
        store.upsert(row)

    current = store.list_current()
    assert [row.version for row in current] == ["2.0.0"]
    versions = store.list_versions("example/cog-a")
    assert [row.version for row in versions] == ["2.0.0", "1.0.0", "3.0.0-dev"], "unknown pushed_at sorts last"


def test_list_current_excludes_non_cog_failed_and_idless_rows(store):
    store.upsert(artifact("a"))
    store.upsert(artifact("b", repository="images/nginx", status=STATUS_NON_COG, read_errors=("no COG.md",)))
    store.upsert(artifact("c", repository="cogs/broken", status=STATUS_FAILED, read_errors=("fetch: OCINotFound",)))
    draft = card()
    draft["id"] = None
    store.upsert(artifact("d", repository="cogs/draft", document=draft))

    assert [row.digest for row in store.list_current()] == [digest("a")]
    assert {row.status for row in (store.get(digest("b")), store.get(digest("c")))} == {STATUS_NON_COG, STATUS_FAILED}


def test_list_current_filters(store):
    store.upsert(
        artifact(
            "a",
            document=card(
                "example/transcriber",
                kind="complete",
                requires=["media/ffmpeg", "model-registry/example"],
                accepts=["media_transcription_request"],
                produces=["timestamped_transcript_bundle"],
            ),
        )
    )
    store.upsert(
        artifact(
            "b",
            repository="cogs/model",
            document=card("example/small-model", kind="model", publisher="Other", provides=["model-endpoint/openai"]),
        )
    )
    store.upsert(
        artifact("c", source_id="other", repository="cogs/notes", document=card("example/notes", kind="context"))
    )

    def ids(**filters) -> list[str]:
        return [row.cog_id for row in store.list_current(CatalogFilter(**filters))]

    assert ids() == ["example/notes", "example/small-model", "example/transcriber"]
    assert ids(kind="model") == ["example/small-model"]
    assert ids(publisher="Other") == ["example/small-model"]
    assert ids(source_id="other") == ["example/notes"]
    assert ids(requires="media/ffmpeg") == ["example/transcriber"]
    assert ids(requires="nothing/declares-this") == []
    assert ids(provides="model-endpoint/openai") == ["example/small-model"]
    assert ids(accepts="media_transcription_request") == ["example/transcriber"]
    assert ids(produces="timestamped_transcript_bundle") == ["example/transcriber"]
    assert ids(kind="complete", requires="model-registry/example") == ["example/transcriber"]
    assert ids(kind="model", requires="model-registry/example") == []


def test_list_current_limit_is_bounded_at_the_boundary(store):
    # MAX_LIST_LIMIT + 5 distinct cog_ids, so the cap is proven at its actual
    # boundary rather than inferred from a five-row set.
    for index in range(MAX_LIST_LIMIT + 5):
        store.upsert(artifact(f"{index:x}", repository=f"cogs/c{index}", document=card(f"example/c{index:04d}")))
    assert len(store.list_current(limit=2)) == 2
    listed = store.list_current(limit=MAX_LIST_LIMIT + 5)
    assert len(listed) == MAX_LIST_LIMIT
    with pytest.raises(ValueError):
        store.list_current(limit=0)


def test_upsert_refuses_an_unknown_status(store):
    with pytest.raises(ValueError, match="unknown catalog status"):
        store.upsert(artifact("a", status="vanished"))


def test_sweep_lock_is_non_blocking_and_single_holder(store):
    with store.sweep_lock() as held:
        assert held is True
        with store.sweep_lock() as second:
            assert second is False
    with store.sweep_lock() as again:
        assert again is True


def test_unavailable_store_refuses_every_call():
    store = UnavailableCogCatalogStore()
    for call in (
        lambda: store.known(SOURCE),
        lambda: store.upsert(artifact("a")),
        lambda: store.update_tags(SOURCE, "r", digest("a"), ()),
        lambda: store.mark_removed(SOURCE, {}),
        lambda: store.mark_removed_one(SOURCE, "r", digest("a")),
        lambda: store.sweep_lock(),
        lambda: store.get(digest("a")),
        lambda: store.locations(digest("a")),
        lambda: store.list_current(),
        lambda: store.list_versions("x"),
    ):
        with pytest.raises(CogCatalogUnavailableError):
            call()


CONTAINMENT_CASES = [
    # (document, needle, expected `document @> needle`). One shared table: the
    # in-memory helper is asserted against these expectations here, and the
    # live layer runs every case through a real server's `@>` so the two can
    # never quietly disagree (the codex gate caught exactly that: the helper
    # once let a scalar match a singleton array, which Postgres refuses).
    ({"a": 1, "b": 2}, {"a": 1}, True),
    ({"a": 1}, {"a": 2}, False),
    ({"a": 1}, {"b": 1}, False),
    (
        {"requires": [{"capability": "x", "locality": "any"}, {"capability": "y"}]},
        {"requires": [{"capability": "y"}]},
        True,
    ),
    ({"requires": [{"capability": "x"}]}, {"requires": [{"capability": "y"}]}, False),
    ({"provides": ["a", "b"]}, {"provides": ["b"]}, True),
    ({"provides": ["a", "b"]}, {"provides": ["b", "c"]}, False),
    ({"provides": []}, {"provides": []}, True),
    # Scalar/array asymmetry: below the top level, neither direction matches.
    ({"k": "v"}, {"k": ["v"]}, False),
    ({"k": ["v"]}, {"k": "v"}, False),
    # ... but a top-level array does contain a bare scalar (and not vice versa).
    (["v", "w"], "v", True),
    ("v", ["v"], False),
    # Booleans are not numbers, while numbers compare numerically.
    ({"k": 1}, {"k": True}, False),
    ({"k": True}, {"k": 1}, False),
    ({"k": 1}, {"k": 1.0}, True),
    # Nested arrays: an array element only matches an array element.
    ({"a": [[1, 2]]}, {"a": [[1]]}, True),
    ({"a": [[1]]}, {"a": [1]}, False),
    ({"a": [1, 2]}, {"a": [[1]]}, False),
    # Empty object/array and null.
    ({"a": [{"x": 1}]}, {"a": [{}]}, True),
    ({"a": None}, {"a": None}, True),
    ({"a": "x"}, {}, True),
    ({"io": {"accepts": ["x"]}}, {"io": {"accepts": ["x"]}}, True),
    ({"io": None}, {"io": {"accepts": ["x"]}}, False),
]


@pytest.mark.parametrize(("document", "needle", "expected"), CONTAINMENT_CASES)
def test_json_contains_mirrors_jsonb_containment(document, needle, expected):
    assert json_contains(document, needle) is expected


def test_get_prefers_the_most_recently_indexed_present_location(store):
    # Conflicting timestamps on purpose: the OLDER-pushed location was indexed
    # more recently, and get()'s documented order is by indexing recency --
    # every location of one digest shares the artifact's push time in
    # practice, so pushed_at cannot be the tiebreak.
    early, late = T0 - timedelta(days=1), T0 + timedelta(days=1)
    store.upsert(replace(artifact("a", repository="cogs/first", pushed_at=late), indexed_at=early))
    store.upsert(replace(artifact("a", repository="mirror/second", pushed_at=early), indexed_at=late))

    assert store.get(digest("a")).repository == "mirror/second"

    # A removed row loses to any present one, whatever its indexing recency.
    store.mark_removed_one(SOURCE, "mirror/second", digest("a"))
    assert store.get(digest("a")).repository == "cogs/first"


def test_naive_datetimes_are_refused(store):
    naive = T0.replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone-aware"):
        store.upsert(artifact("a", pushed_at=naive))
    store.upsert(artifact("a"))
    with pytest.raises(ValueError, match="timezone-aware"):
        store.update_tags(SOURCE, "cogs/cog-a", digest("a"), ("v2",), pushed_at=naive)


def test_contains_nul_walks_values_and_keys_but_not_escape_text():
    assert contains_nul({"a": ["fine", {"b": "bad\x00"}]}) is True
    assert contains_nul({"bad\x00key": "v"}) is True
    assert contains_nul({"a": ["fine", {"b": r"literal \u0000 text"}]}) is False
    assert contains_nul({"n": 1, "b": True, "x": None}) is False


def test_cards_that_merely_mention_nul_are_stored(store):
    document = card()
    document["body"] = r"The escape sequence \u0000 denotes NUL."
    store.upsert(artifact("a", document=document))
    assert store.get(digest("a")).card["body"] == document["body"]


class _OffsetlessTz(tzinfo):
    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None


def test_datetimes_with_a_tzinfo_but_no_offset_are_refused(store):
    # tzinfo present, utcoffset() None: naive in every way that matters
    # (round-2 codex finding).
    sneaky = T0.replace(tzinfo=_OffsetlessTz())
    with pytest.raises(ValueError, match="timezone-aware"):
        store.upsert(artifact("a", pushed_at=sneaky))
    store.upsert(artifact("a"))
    with pytest.raises(ValueError, match="timezone-aware"):
        store.update_tags(SOURCE, "cogs/cog-a", digest("a"), ("v2",), pushed_at=sneaky)


def test_upsert_refuses_a_card_jsonb_cannot_store(store):
    # Parity with Postgres, where a NUL in any jsonb string is refused: the
    # in-memory store must fail the same way or tests would pass on content
    # production rejects (the codex gate's poison-card finding).
    poison = card()
    poison["summary"] = "before\x00after"
    with pytest.raises(CogCatalogDataError):
        store.upsert(artifact("a", document=poison))
    assert store.get(digest("a")) is None


def test_card_search_fields_stringify_scalars_and_drop_structures():
    fields = card_search_fields({"id": "a/b", "name": "b", "version": 1.5, "kind": "model", "publisher": {"x": 1}})
    assert fields == {
        "cog_id": "a/b",
        "name": "b",
        "version": "1.5",
        "kind": "model",
        "publisher": None,
        "manifest_schema": None,
    }


# ---------------------------------------------------------------------------
# Live-Postgres coverage (opt in with COLLAB_HUB_TEST_POSTGRES_URL)
# ---------------------------------------------------------------------------

POSTGRES_URL = os.environ.get("COLLAB_HUB_TEST_POSTGRES_URL", "")

live_postgres = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set COLLAB_HUB_TEST_POSTGRES_URL to a disposable database to run the live-Postgres catalog tests",
)


def _database(max_size: int = 10):
    from collab_hub_api.frames.db import PostgresDatabase

    return PostgresDatabase(POSTGRES_URL, min_size=0, max_size=max_size, timeout_seconds=10.0)


@pytest.fixture
def live_store():
    """A migrated live database with an empty catalog, dropped again afterwards."""

    from test_collab_schema import COLLAB_TABLES

    database = _database()

    def drop_all() -> None:
        with database.connection() as conn:
            for table in COLLAB_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    try:
        drop_all()
        run_collab_schema_migrations(database)
        yield PostgresCogCatalogStore(database), database
        drop_all()
    finally:
        database.close()


@live_postgres
def test_live_upsert_get_update_tags_and_removal(live_store):
    store, _ = live_store
    row = artifact("a", tags=("v1",))
    store.upsert(row)

    got = store.get(row.digest)
    assert got.card == card(), "the card round-trips through jsonb verbatim"
    assert got.tags == ("v1",) and got.pushed_at == T0 and got.indexed_at is not None
    assert got.reference == f"{HOST}/cogs/cog-a@{row.digest}"
    assert got.read_errors == () and got.present
    first_indexed_at = got.indexed_at

    # Tag change: no card write (indexed_at unchanged), presence restored.
    store.mark_removed_one(SOURCE, "cogs/cog-a", row.digest)
    assert store.update_tags(SOURCE, "cogs/cog-a", row.digest, ("latest", "v1")) is True
    got = store.get(row.digest)
    assert got.tags == ("latest", "v1") and got.present and got.indexed_at == first_indexed_at

    # Upsert replaces the row whole and refreshes indexed_at.
    store.upsert(artifact("a", tags=("v2",), document=card(version="2.0.0"), read_errors=("warning: x",)))
    got = store.get(row.digest)
    assert got.version == "2.0.0" and got.tags == ("v2",) and got.read_errors == ("warning: x",)
    assert got.indexed_at >= first_indexed_at
    assert len(store.locations(row.digest)) == 1

    # Removal keeps the row readable by digest.
    other = artifact("b", repository="cogs/cog-b", document=card("example/cog-b"))
    store.upsert(other)
    assert store.mark_removed(SOURCE, {"cogs/cog-a": [row.digest]}) == 1
    assert store.mark_removed(SOURCE, {"cogs/cog-a": [row.digest]}) == 0
    removed = store.get(other.digest)
    assert removed.removed_at is not None and removed.card == card("example/cog-b")
    assert [r.digest for r in store.list_current()] == [row.digest]
    assert [r.digest for r in store.list_versions("example/cog-b", include_removed=True)] == [other.digest]
    assert sorted(store.known(SOURCE), key=lambda k: k.repository) == [
        KnownArtifact("cogs/cog-a", row.digest, ("v2",), STATUS_INDEXED, False),
        KnownArtifact("cogs/cog-b", other.digest, ("v1",), STATUS_INDEXED, True),
    ]


@live_postgres
def test_live_status_check_and_non_cog_rows(live_store):
    import psycopg

    store, database = live_store
    store.upsert(artifact("n", repository="images/nginx", status=STATUS_NON_COG, read_errors=("no COG.md layer",)))
    got = store.get(digest("n"))
    assert got.status == STATUS_NON_COG and got.card is None and got.read_errors == ("no COG.md layer",)
    assert store.list_current() == []

    with pytest.raises(psycopg.errors.CheckViolation):
        with database.connection() as conn:
            conn.execute("UPDATE collab_cog_artifacts SET status = 'vanished'")


@live_postgres
def test_live_containment_filters_match_and_use_the_gin_index(live_store):
    from psycopg.types.json import Jsonb

    store, database = live_store
    store.upsert(
        artifact(
            "a",
            document=card(
                "example/transcriber",
                requires=["media/ffmpeg", "model-registry/example"],
                accepts=["media_transcription_request"],
                produces=["timestamped_transcript_bundle"],
            ),
        )
    )
    store.upsert(
        artifact(
            "b",
            repository="cogs/model",
            document=card("example/small-model", kind="model", publisher="Other", provides=["model-endpoint/openai"]),
        )
    )
    store.upsert(
        artifact(
            "c",
            repository="cogs/old",
            pushed_at=T0 - timedelta(days=1),
            document=card("example/transcriber", version="0.9", requires=["media/ffmpeg"]),
        )
    )

    def ids(**filters) -> list[str]:
        return [row.cog_id for row in store.list_current(CatalogFilter(**filters))]

    assert ids() == ["example/small-model", "example/transcriber"]
    assert ids(kind="model") == ["example/small-model"]
    assert ids(publisher="Other") == ["example/small-model"]
    assert ids(requires="media/ffmpeg") == ["example/transcriber"]
    assert ids(requires="nothing/declares-this") == []
    assert ids(provides="model-endpoint/openai") == ["example/small-model"]
    assert ids(accepts="media_transcription_request") == ["example/transcriber"]
    assert ids(produces="timestamped_transcript_bundle") == ["example/transcriber"]
    assert ids(kind="complete", requires="model-registry/example") == ["example/transcriber"]
    # Newest-per-cog_id collapse happens in SQL: the 0.9 row is the same cog_id, older.
    assert [row.version for row in store.list_versions("example/transcriber")] == ["1.0.0", "0.9"]

    # The filter is written as `card @> %s`, which is what the jsonb_path_ops
    # GIN index serves. On a three-row table the planner will not necessarily
    # pick that index over the removed_at btree (both are eligible and both
    # are cheap), so two things are proven separately: the store's statement
    # carries the containment predicate on `card`, and that predicate alone is
    # answered from the GIN index once a sequential scan is off the table.
    filters = CatalogFilter(requires="media/ffmpeg")
    sql, params = PostgresCogCatalogStore.current_query(filters, filters.containment(), 10)
    bound = [Jsonb(p) if isinstance(p, dict) else p for p in params]
    with database.connection() as conn:
        conn.execute("SET LOCAL enable_seqscan = off")
        plan = "\n".join(row["QUERY PLAN"] for row in conn.execute("EXPLAIN " + sql, bound).fetchall())
        bare = "\n".join(
            row["QUERY PLAN"]
            for row in conn.execute(
                "EXPLAIN SELECT digest FROM collab_cog_artifacts WHERE card @> %s", (Jsonb(filters.containment()),)
            ).fetchall()
        )
    assert '(card @> \'{"requires": [{"capability": "media/ffmpeg"}]}\'::jsonb)' in plan, plan
    assert "Index Scan" in plan and "Seq Scan" not in plan, plan
    assert "collab_cog_artifacts_card_idx" in bare, bare


@live_postgres
def test_live_sweep_lock_is_single_flight_across_connections_and_restores_autocommit(live_store):
    store, database = live_store
    other = PostgresCogCatalogStore(_database(max_size=2))
    try:
        with store.sweep_lock() as held:
            assert held is True
            with other.sweep_lock() as second:
                assert second is False, "a second connection must not get the session lock"
            # The lock is session-level: a fresh transaction on another
            # connection of the *same* pool must also be refused, and the
            # holder's connection must not be inside a transaction.
            with database.connection() as conn:
                row = conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (COG_INDEX_LOCK_KEY,)).fetchone()
                assert row["locked"] is False
                idle = conn.execute(
                    "SELECT count(*) AS n FROM pg_stat_activity"
                    " WHERE state = 'idle in transaction' AND pid <> pg_backend_pid()"
                    " AND query LIKE '%pg_try_advisory_lock%'"
                ).fetchone()
                assert idle["n"] == 0, "the lock holder must not sit idle in a transaction"
        with other.sweep_lock() as after:
            assert after is True
    finally:
        other._db.close()
    # Autocommit restoration is asserted with connection identity in
    # test_live_lock_connection_returns_with_autocommit_restored below; a
    # checkout from a many-connection pool here would not have proven it was
    # the lock's connection being inspected.


@live_postgres
def test_live_lock_connection_returns_with_autocommit_restored(live_store):
    """The very connection that held the lock goes back with autocommit off.

    A one-connection pool makes the identity provable: the next checkout MUST
    be the connection that held the lock (the pid, read out of pg_locks while
    it was held, pins that), so the transaction semantics asserted afterwards
    are asserted on the right connection rather than on whichever one a larger
    pool happened to hand out.
    """

    _, database = live_store
    single = _database(max_size=1)
    store = PostgresCogCatalogStore(single)
    try:
        with store.sweep_lock() as held:
            assert held is True
            with database.connection() as conn:
                row = conn.execute(
                    "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND classid = %s AND objid = %s",
                    (COG_INDEX_LOCK_KEY >> 32, COG_INDEX_LOCK_KEY & 0xFFFFFFFF),
                ).fetchone()
            assert row is not None
            holder_pid = row["pid"]

        with pytest.raises(RuntimeError):
            with single.connection() as conn:
                assert conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"] == holder_pid
                assert conn.autocommit is False
                conn.execute("INSERT INTO collab_orgs (id, created_by) VALUES ('rolled-back', 'sub')")
                raise RuntimeError("abort")
        with single.connection() as conn:
            assert conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"] == holder_pid
            assert conn.execute("SELECT count(*) AS n FROM collab_orgs").fetchone()["n"] == 0
    finally:
        single.close()


@live_postgres
def test_live_jsonb_containment_agrees_with_the_helper(live_store):
    """Every shared containment case, asked of a real server and of the helper.

    Agreement is the property (the in-memory store must answer filters exactly
    as production does); the expected values in the table are additionally
    pinned so a wrong expectation cannot hide a double failure.
    """

    from psycopg.types.json import Jsonb

    _, database = live_store
    with database.connection() as conn:
        for document, needle, expected in CONTAINMENT_CASES:
            row = conn.execute("SELECT %s::jsonb @> %s::jsonb AS contains", (Jsonb(document), Jsonb(needle))).fetchone()
            assert row["contains"] is expected, f"server disagrees with the table for {document!r} @> {needle!r}"
            assert json_contains(document, needle) is expected, f"helper disagrees for {document!r} @> {needle!r}"


@live_postgres
def test_live_poison_card_is_a_data_error_not_an_outage(live_store):
    """psycopg's DataError for NUL-in-jsonb surfaces as CogCatalogDataError.

    This is the second line behind the indexer's pre-validation: bypass the
    validation (write straight to the store) and the translation still lets a
    caller distinguish "this row's content" from "the database is down".
    """

    store, _ = live_store
    poison = card()
    poison["summary"] = "nul\x00nul"
    with pytest.raises(CogCatalogDataError):
        store.upsert(artifact("f", repository="cogs/poison", document=poison))
    assert store.get(digest("f")) is None

    with pytest.raises(ValueError, match="timezone-aware"):
        store.upsert(artifact("f", pushed_at=T0.replace(tzinfo=None)))


@live_postgres
def test_live_get_orders_by_indexing_recency_not_push_time(live_store):
    store, _ = live_store
    # The later-pushed location is indexed FIRST; the earlier-pushed one is
    # indexed second and must win, because get() is about this catalog's own
    # recency, not the artifact's shared push time.
    store.upsert(artifact("a", repository="cogs/first", pushed_at=T0 + timedelta(days=1)))
    time.sleep(0.01)  # separate the two server-side indexed_at values
    store.upsert(artifact("a", repository="mirror/second", pushed_at=T0 - timedelta(days=1)))

    assert store.get(digest("a")).repository == "mirror/second"
    store.mark_removed_one(SOURCE, "mirror/second", digest("a"))
    assert store.get(digest("a")).repository == "cogs/first", "a present row beats a removed one"


@live_postgres
def test_live_concurrent_sweepers_exactly_one_wins(live_store):
    _, _ = live_store
    replicas = 6
    stores = [PostgresCogCatalogStore(_database(max_size=2)) for _ in range(replicas)]
    start = threading.Barrier(replicas)
    release = threading.Event()

    def attempt(store: PostgresCogCatalogStore) -> bool:
        start.wait()
        with store.sweep_lock() as held:
            if held:
                release.wait(timeout=10)
            return held

    try:
        with ThreadPoolExecutor(max_workers=replicas) as pool:
            futures = [pool.submit(attempt, store) for store in stores]
            # Let the losers report before the winner lets go.
            while sum(f.done() for f in futures) < replicas - 1:
                threading.Event().wait(0.01)
            release.set()
            results = [f.result() for f in futures]
    finally:
        for store in stores:
            store._db.close()
    assert results.count(True) == 1 and results.count(False) == replicas - 1
