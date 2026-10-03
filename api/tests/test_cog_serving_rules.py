"""The rules a pull through the Hub is held to (issue #179), each pinned by a request that would break it.

Written against the HTTP surface with the fake backing registry of
``test_cog_serving``, so each test states an outcome a client would see
rather than how the Hub arrives at it:

- a pull token is worth nothing once its owner loses catalog access;
- an indexed pin is pullable however many newer versions exist;
- a blob is pullable only while a pullable manifest references it;
- a response that ends early closes the upstream it was relaying from.
"""

# ruff: noqa: F811 - the fixtures imported from test_cog_serving are used as parameters

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from test_cog_serving import (
    ALPHA,
    MEDIA_TYPE_OCI_MANIFEST,
    REPO,
    T0,
    Bundle,
    FakeRegistry,
    Hub,
    basic,
    bearer,
    catalog_row,
    descriptor,
    hub,  # noqa: F401 - fixture
    make_hub,  # noqa: F401 - fixture
    settings,
    sha256,
)

from collab_hub_api import config as config_module
from collab_hub_api.cogs.oci import MEDIA_TYPE_NEBI_ASSET, MEDIA_TYPE_PIXI_CONFIG, OCIClient
from collab_hub_api.cogs.registry import build_registry_sources
from collab_hub_api.cogs.serving import CogRegistryFront
from collab_hub_api.config import Config
from collab_hub_api.core import make_app
from collab_hub_api.frames.identity import IDENTITY_CLAIM_ENV
from collab_hub_api.frames.org_source import ORG_SOURCE_ENV
from collab_hub_api.frames.orgs import MEMBERSHIP_REMOVED, OrgsUnavailableError
from collab_hub_api.routers.registry import BlobStreamAborted

MEMBER = "sub-member-0001"
MEMBER_TOKEN = bearer({"sub": MEMBER, "sid": "session-member"})
ORG = "org-1111"


@pytest_asyncio.fixture
async def membership_hubs(tmp_path, monkeypatch):
    """Two replicas of one membership-resolving Hub: separate apps, one catalog, one credential store, one org store."""

    monkeypatch.setenv("FRAMES_UNSAFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("FRAMES_BEARER_ALLOW_UNSIGNED", "true")
    monkeypatch.setenv(IDENTITY_CLAIM_ENV, "sub")
    monkeypatch.setenv(ORG_SOURCE_ENV, "membership")
    upstream = FakeRegistry()
    transport = httpx.MockTransport(upstream)
    monkeypatch.setattr(
        config_module,
        "build_registry_sources",
        lambda configs: build_registry_sources(configs, http_transport=transport),
    )
    values = settings(tmp_path)
    values["frames"]["orgs"] = {"backend": "memory"}
    stack, hubs = [], []
    for _ in range(2):
        app = make_app(Config.parse(values))
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        stack.append((lifespan, client))
        hubs.append(Hub(app, client, upstream))
    first, second = hubs
    # What a shared database gives two replicas. Nothing else is shared: each
    # app has its own router, its own sources and its own process state.
    serving = second.app.state.cog_registry_serving
    second.app.state.org_store = first.app.state.org_store
    second.app.state.cog_catalog_store = first.catalog
    second.app.state.cog_registry_serving = type(serving)(
        front=CogRegistryFront(first.catalog, serving.front.sources, max_blob_bytes=1 << 30),
        credentials=first.serving.credentials,
        host=serving.host,
        token_url=serving.token_url,
        credential_ttl_seconds=serving.credential_ttl_seconds,
        token_ttl_seconds=serving.token_ttl_seconds,
        max_blob_seconds=serving.max_blob_seconds,
    )
    first.app.state.org_store.set_membership(MEMBER, ORG)
    yield first, second
    for lifespan, client in reversed(stack):
        await client.aclose()
        await lifespan.__aexit__(None, None, None)


READS = (
    ("GET", "/v2/"),
    ("HEAD", "/v2/"),
    ("GET", f"/v2/{REPO}/tags/list"),
    ("GET", f"/v2/{REPO}/manifests/latest"),
    ("HEAD", f"/v2/{REPO}/manifests/latest"),
    ("GET", f"/v2/{REPO}/manifests/{ALPHA.digest}"),
    ("HEAD", f"/v2/{REPO}/manifests/{ALPHA.digest}"),
    ("GET", f"/v2/{REPO}/blobs/{sha256(ALPHA.config)}"),
    ("HEAD", f"/v2/{REPO}/blobs/{sha256(ALPHA.config)}"),
)


async def test_a_token_stops_working_everywhere_when_its_owner_loses_catalog_access(membership_hubs):
    """Mint while a member, remove the membership, then use the token on every read route of another replica."""

    first, second = membership_hubs
    first.seed(REPO, ALPHA, "latest")
    # Both kinds of token: from a registry credential, and straight from the Hub session.
    credential = await first.exchange(MEMBER_TOKEN)
    from_credential = {"Authorization": f"Bearer {await first.token(credential, REPO)}"}
    direct = await first.get("/v2/token", params={"scope": f"repository:{REPO}:pull"}, headers=MEMBER_TOKEN)
    from_session = {"Authorization": f"Bearer {direct.json()['token']}"}

    for headers in (from_credential, from_session):
        for replica in (first, second):
            for method, path in READS:
                response = await replica.request(method, path, headers=headers)
                assert response.status_code == 200, (method, path, response.status_code)
    assert (await second.get("/v1/cogs", headers=MEMBER_TOKEN)).status_code == 200

    first.app.state.org_store.set_membership(MEMBER, ORG, status=MEMBERSHIP_REMOVED)

    # The catalog refuses this caller now, so the registry surface does too:
    # same token, same replicas, no bytes and no tags.
    catalog = await second.get("/v1/cogs", headers=MEMBER_TOKEN)
    assert catalog.status_code == 403 and catalog.json()["error"]["code"] == "no_organization"
    asked = len(first.upstream.requests)
    for headers in (from_credential, from_session):
        for replica in (second, first):
            for method, path in READS:
                response = await replica.request(method, path, headers=headers)
                assert response.status_code == 403, (method, path, response.status_code)
                assert response.content == b"" or response.json()["errors"][0]["code"] == "DENIED"
                assert "docker-content-digest" not in response.headers
    assert len(first.upstream.requests) == asked, "a refused caller costs the source nothing"
    # And the credential mints nothing more.
    refused = await second.get("/v2/token", headers=basic(credential["username"], credential["secret"]))
    assert refused.status_code == 403
    assert (await second.get("/v2/token", headers=MEMBER_TOKEN)).status_code == 403

    # Restored, the same token works again: it was the owner's standing, not the token, that changed.
    first.app.state.org_store.set_membership(MEMBER, ORG)
    assert (await second.get(f"/v2/{REPO}/manifests/latest", headers=from_credential)).status_code == 200


async def test_a_membership_lookup_that_fails_admits_nobody(membership_hubs, monkeypatch):
    first, second = membership_hubs
    first.seed(REPO, ALPHA, "latest")
    headers = await first.pull_token(REPO, who=MEMBER_TOKEN)
    assert (await second.get(f"/v2/{REPO}/manifests/latest", headers=headers)).status_code == 200

    def down(_user):
        raise OrgsUnavailableError("organization storage is down")

    monkeypatch.setattr(first.app.state.org_store, "resolve_principal", down)
    for method, path in READS:
        response = await second.request(method, path, headers=headers)
        assert response.status_code == 503, (method, path, response.status_code)
        if method == "GET":
            assert response.json()["errors"][0]["code"] == "UNAVAILABLE"


async def test_an_old_pin_stays_pullable_however_many_newer_versions_exist(hub: Hub):
    """No window stands between an indexed digest and its row: version 1 of 300 pulls like version 300."""

    oldest = Bundle("oldest")
    hub.seed(REPO, oldest, "v0", pushed_at=T0 - timedelta(days=400))
    for index in range(299):
        digest = "sha256:" + f"{index + 1:064x}"
        hub.catalog.upsert(catalog_row(REPO, digest, tags=(f"v{index + 1}",), pushed_at=T0 - timedelta(days=index)))
    # Rows of a source this Hub no longer has do not crowd it out either.
    for index in range(20):
        retired = catalog_row(REPO, oldest.digest, tags=("v0",), source_id=f"retired-{index}", pushed_at=T0)
        hub.catalog.upsert(retired)
    headers = await hub.pull_token(REPO)

    card = await hub.get(f"/v1/cogs/example/cog-alpha/versions/{oldest.digest}", headers=bearer_alice())
    assert card.status_code == 200
    for reference in (oldest.digest, "v0"):
        manifest = await hub.get(f"/v2/{REPO}/manifests/{reference}", headers=headers)
        assert manifest.status_code == 200 and manifest.content == oldest.manifest, reference
    for digest, data in oldest.blobs.items():
        blob = await hub.get(f"/v2/{REPO}/blobs/{digest}", headers=headers)
        assert blob.status_code == 200 and blob.content == data
    tags = (await hub.get(f"/v2/{REPO}/tags/list", headers=headers)).json()["tags"]
    assert len(tags) == 300 and "v0" in tags and "v299" in tags


def bearer_alice() -> dict[str, str]:
    return bearer({"preferred_username": "alice", "org_id": "org-a", "workspace_id": "ws"})


async def test_removing_a_version_takes_its_blobs_unless_another_version_shares_them(hub: Hub):
    first = Bundle("first")
    unique = b"only in the second manifest"
    second_manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": MEDIA_TYPE_OCI_MANIFEST,
            "config": descriptor(MEDIA_TYPE_PIXI_CONFIG, first.config),
            "layers": [descriptor(MEDIA_TYPE_NEBI_ASSET, unique, "COG.md")],
        }
    ).encode()
    hub.seed(REPO, first, "first")
    second_digest = hub.upstream.publish_raw(REPO, MEDIA_TYPE_OCI_MANIFEST, second_manifest, "second")
    hub.upstream.blobs[sha256(unique)] = unique
    hub.catalog.upsert(catalog_row(REPO, second_digest, tags=("second",), pushed_at=T0 - timedelta(days=1)))
    headers = await hub.pull_token(REPO)
    for tag in ("first", "second"):
        assert (await hub.get(f"/v2/{REPO}/manifests/{tag}", headers=headers)).status_code == 200
    shared, own = sha256(first.config), sha256(first.files["pixi.toml"][1])

    async def status(digest: str) -> int:
        head = await hub.request("HEAD", f"/v2/{REPO}/blobs/{digest}", headers=headers)
        get = await hub.get(f"/v2/{REPO}/blobs/{digest}", headers=headers)
        assert head.status_code == get.status_code
        return get.status_code

    assert [await status(d) for d in (shared, own, sha256(unique))] == [200, 200, 200]

    hub.catalog.mark_removed_one("backing", REPO, first.digest)
    asked = len(hub.upstream.requests)
    assert await status(own) == 404, "its own blob went with it, at once"
    assert len(hub.upstream.requests) == asked
    assert await status(shared) == 200, "the second manifest still references this one"

    hub.catalog.mark_removed_one("backing", REPO, second_digest)
    assert [await status(d) for d in (shared, sha256(unique))] == [404, 404]
    # Back in the registry and reindexed: pullable again, with nothing to re-record.
    hub.catalog.upsert(catalog_row(REPO, first.digest, tags=("first",)))
    assert await status(own) == 200


async def test_a_reader_that_stops_reading_does_not_keep_the_upstream_open(hub: Hub, monkeypatch):
    """The deadline passes while the response is blocked sending to the client: the upstream is closed.

    The stuck client connection itself is the server's to reap (the response
    sits behind middleware that is blocked in ``send``); what the Hub owns,
    and must not leak, is the connection to the source.
    """

    # Large enough that the relay cannot have read it all before the client stalls.
    large = Bundle("stalled", big=4_000_000)
    hub.seed(REPO, large, "latest")
    headers = await hub.pull_token(REPO)
    assert (await hub.get(f"/v2/{REPO}/manifests/latest", headers=headers)).status_code == 200
    big = sha256(large.files["model.bin"][1])
    # The source's body as a real stream, a chunk at a time, so the relay cannot have drained it early.
    async def body(data: bytes):
        for offset in range(0, len(data), 65536):
            await asyncio.sleep(0)
            yield data[offset : offset + 65536]

    hub.upstream._slowly = body
    hub.upstream.stream_delay = 1
    opened = []
    real_open = OCIClient.open_blob

    async def recording_open(self, repo, digest):
        stream = await real_open(self, repo, digest)
        opened.append(stream)
        return stream

    monkeypatch.setattr(OCIClient, "open_blob", recording_open)
    serving = hub.serving
    hub.app.state.cog_registry_serving = type(serving)(
        **{**{f: getattr(serving, f) for f in serving.__dataclass_fields__}, "max_blob_seconds": 0.3}
    )

    sent: list[dict] = []
    stalled = asyncio.Event()

    async def receive():
        await asyncio.sleep(3600)

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            stalled.set()
            await asyncio.sleep(3600)  # a client that has stopped reading

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"/v2/{REPO}/blobs/{big}",
        "raw_path": f"/v2/{REPO}/blobs/{big}".encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test"), (b"authorization", headers["Authorization"].encode())],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
        "state": {},
    }
    exchange = asyncio.create_task(hub.app(scope, receive, send))
    try:
        await asyncio.wait_for(stalled.wait(), timeout=5)
        (stream,) = opened
        assert not stream._closed, "still relaying when the client stopped reading"
        # The deadline (0.3 s) passes with the response blocked on the client.
        for _ in range(100):
            if stream._closed:
                break
            await asyncio.sleep(0.05)
        assert stream._closed and stream._response.is_closed, "the upstream response must not outlive the deadline"
    finally:
        # The server (not the app) owns the stuck client connection; here the test does.
        exchange.cancel()
        with pytest.raises((asyncio.CancelledError, BlobStreamAborted)):
            await exchange
    received = sum(len(m.get("body", b"")) for m in sent if m["type"] == "http.response.body")
    assert received < len(large.files["model.bin"][1])


async def test_tags_page_all_the_way_to_the_end(hub: Hub):
    """10,001 tags: a default page is bounded, and following ``Link`` reaches the last one."""

    hub.seed(REPO, ALPHA, "latest")
    tags = tuple(f"v{index:05d}" for index in range(10_001))
    hub.catalog.upsert(catalog_row(REPO, "sha256:" + "9" * 64, tags=tags, pushed_at=T0 - timedelta(days=1)))
    headers = await hub.pull_token(REPO)

    first = await hub.get(f"/v2/{REPO}/tags/list", headers=headers)
    assert len(first.json()["tags"]) == 1000 and first.json()["tags"][0] == "latest"
    assert first.headers["link"] == f'</v2/{REPO}/tags/list?n=1000&last=v00998>; rel="next"'

    after = await hub.get(f"/v2/{REPO}/tags/list", params={"last": "v09999"}, headers=headers)
    assert after.json()["tags"] == ["v10000"] and "link" not in after.headers
    assert (await hub.get(f"/v2/{REPO}/tags/list", params={"last": "v10000"}, headers=headers)).json()["tags"] == []

    collected, url, pages = [], f"/v2/{REPO}/tags/list?n=1000", 0
    while url:
        page = await hub.get(url, headers=headers)
        assert page.status_code == 200
        collected.extend(page.json()["tags"])
        link = page.headers.get("link")
        url = link[1 : link.index(">")] if link else None
        pages += 1
    assert collected == sorted(("latest", *tags)) and pages == 11
    # n is capped at the page size, and still links onward.
    capped = await hub.get(f"/v2/{REPO}/tags/list", params={"n": 50_000}, headers=headers)
    assert len(capped.json()["tags"]) == 1000 and "link" in capped.headers


async def test_a_blocked_database_call_answers_503_in_time_and_releases_its_worker_and_connection(hub: Hub):
    """The request budget reaches the database: the store call is bounded by the server, not by the coroutine.

    The connection here is a fake that behaves as Postgres does under a
    ``statement_timeout``: the statement blocks, and is cancelled when the
    timeout the Hub set for it elapses. What this proves is the Hub's half --
    that it sets the timeout from the request budget, bounds the pool wait,
    answers 503 in time, and that once the statement is cancelled the worker
    thread unwinds and the connection is returned. That Postgres honours
    ``statement_timeout`` is its own, and is exercised by the live test below.
    """

    import threading
    import time
    from contextlib import contextmanager

    import psycopg

    from collab_hub_api.cogs.catalog import PostgresCogCatalogStore

    events: dict[str, float] = {}
    state = {"timeout_ms": None, "acquire_timeout": None, "checked_out": 0}
    released = threading.Event()

    class BlockingConnection:
        def execute(self, sql, params=None):
            if "set_config('statement_timeout'" in sql:
                state["timeout_ms"] = int(params[0])
                return self
            events["blocked_at"] = time.monotonic()
            time.sleep(state["timeout_ms"] / 1000)  # the server gives up exactly when it was told to
            events["cancelled_at"] = time.monotonic()
            raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

        def fetchall(self):
            return []

        def fetchone(self):
            return None

    class Database:
        @contextmanager
        def connection(self, timeout=None):
            state["acquire_timeout"] = timeout
            state["checked_out"] += 1
            try:
                yield BlockingConnection()
            finally:
                state["checked_out"] -= 1
                released.set()

    hub.seed(REPO, ALPHA, "latest")
    headers = await hub.pull_token(REPO)
    serving = hub.serving
    blocked = CogRegistryFront(PostgresCogCatalogStore(Database()), serving.front.sources, max_blob_bytes=1 << 30)
    fields = {name: getattr(serving, name) for name in serving.__dataclass_fields__}
    hub.app.state.cog_registry_serving = type(serving)(**{**fields, "front": blocked, "max_metadata_seconds": 0.4})

    for method, path in (
        ("GET", f"/v2/{REPO}/manifests/latest"),
        ("HEAD", f"/v2/{REPO}/manifests/{ALPHA.digest}"),
        ("GET", f"/v2/{REPO}/tags/list"),
        ("HEAD", f"/v2/{REPO}/blobs/{sha256(ALPHA.config)}"),
        ("GET", f"/v2/{REPO}/blobs/{sha256(ALPHA.config)}"),
    ):
        released.clear()
        started = time.monotonic()
        response = await hub.request(method, path, headers=headers)
        elapsed = time.monotonic() - started
        assert response.status_code == 503, (method, path, response.status_code)
        assert elapsed < 2.0, f"{method} {path} took {elapsed:.2f}s against a 0.4s budget"
        # The budget, not a default: the statement timeout and the pool wait are what is left of 0.4 s.
        assert 1 <= state["timeout_ms"] <= 400 and 0 < state["acquire_timeout"] <= 0.4
        # The worker was not abandoned mid-statement: it ran to the cancellation and gave the connection back.
        assert await asyncio.to_thread(released.wait, 2.0), "the connection was never released"
        assert state["checked_out"] == 0
        assert events["cancelled_at"] - events["blocked_at"] <= 0.45


def test_live_postgres_ends_a_statement_at_the_request_budget():
    """The other half of the test above, against a real server: ``statement_timeout`` cancels the statement."""

    import os
    import time

    url = os.environ.get("COLLAB_HUB_TEST_POSTGRES_URL", "")
    if not url:
        pytest.skip("set COLLAB_HUB_TEST_POSTGRES_URL to run the live statement-timeout test")
    import psycopg

    from collab_hub_api.cogs import deadline
    from collab_hub_api.frames.db import PostgresDatabase

    database = PostgresDatabase(url, min_size=0, max_size=1, timeout_seconds=10.0)
    token = deadline.request_deadline.set(time.monotonic() + 0.3)
    try:
        started = time.monotonic()
        with pytest.raises(psycopg.errors.QueryCanceled):
            with deadline.bounded_connection(database) as conn:
                conn.execute("SELECT pg_sleep(10)")
        assert time.monotonic() - started < 2.0
        # The one pooled connection came back usable, with no timeout left on it.
        deadline.request_deadline.set(None)
        with database.connection() as conn:
            assert conn.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "0"
            assert conn.execute("SELECT 1 AS one").fetchone()["one"] == 1
        # And a pool with no free connection is not waited on past the budget.
        deadline.request_deadline.set(time.monotonic() + 0.3)
        with database.connection():
            started = time.monotonic()
            with pytest.raises(psycopg.OperationalError):
                with deadline.bounded_connection(database):
                    pass
            assert time.monotonic() - started < 2.0
    finally:
        deadline.request_deadline.reset(token)
        database.close()
