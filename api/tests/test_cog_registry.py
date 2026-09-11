"""The registry-source seam: config validation, startup dispatch, and the adapter contract suite."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest
from cog_registry_fakes import (
    API_HOST,
    API_URL,
    DIGEST_A,
    DIGEST_B,
    DIGEST_C,
    HOST,
    PUSHED_A,
    PUSHED_B,
    REPOSITORIES,
    URL,
    FakeOCIFactory,
    harbor_rest_handler,
    index_document,
)
from pydantic import ValidationError

from collab_hub_api.cogs.adapters.harbor import HarborRegistrySource
from collab_hub_api.cogs.adapters.static import (
    MAX_INDEX_BYTES,
    MAX_TAGS_PER_REPOSITORY,
    StaticRegistrySource,
    parse_index_document,
)
from collab_hub_api.cogs.oci import MEDIA_TYPE_OCI_MANIFEST, BasicCredentials, OCINotFound
from collab_hub_api.cogs.registry import (
    ArtifactRef,
    CogRegistryCredentials,
    CogRegistrySourceConfig,
    RegistryEvent,
    RegistrySource,
    RegistrySourceError,
    RegistrySourceProtocolError,
    WebhookRequest,
    build_registry_sources,
    http_verify,
    is_repository_path,
    parse_registry_event,
    parse_timestamp,
    reference,
    registry_host,
)

INDEX_URL = "https://index.example/catalog.v1.json"
CREDENTIALS = {"username": "robot$cogs+indexer", "password": "robot-secret-value"}


def harbor_config(**overrides) -> dict:
    return {
        "id": "harbor-main",
        "kind": "harbor",
        "url": URL,
        "api_url": API_URL,
        "token_url": f"{API_URL}/service/token",
        "projects": ["cogs"],
        "credentials": CREDENTIALS,
        "webhook_secret": "shared-webhook-secret",
        **overrides,
    }


def static_config(**overrides) -> dict:
    return {
        "id": "static-main",
        "kind": "static",
        "url": URL,
        "repositories": list(REPOSITORIES),
        "credentials": CREDENTIALS,
        **overrides,
    }


# --- config validation ----------------------------------------------------


def test_valid_configs_parse_and_normalize() -> None:
    harbor = CogRegistrySourceConfig(**harbor_config(url=f" {URL}/ ", api_url=f"{API_URL}/api/v2.0/"))
    assert harbor.url == URL
    assert harbor.api_url == f"{API_URL}/api/v2.0"
    assert harbor.credentials.configured
    static = CogRegistrySourceConfig(**static_config(repositories=[" cogs/alpha ", "cogs/beta"], index_url=INDEX_URL))
    assert static.repositories == ["cogs/alpha", "cogs/beta"]
    assert not static.credentials.configured or static.credentials.username == CREDENTIALS["username"]
    assert CogRegistrySourceConfig(**static_config(repositories=[], index_url=INDEX_URL)).index_url == INDEX_URL


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        # id
        ({"id": ""}, "must not be empty"),
        ({"id": "Harbor Main"}, "must match"),
        ({"id": "-leading"}, "must match"),
        ({"id": "x" * 65}, "must match"),
        # url
        ({"url": "registry.example"}, "url must be an http(s) URL"),
        ({"url": "ftp://registry.example"}, "url must be an http(s) URL"),
        ({"url": "https://"}, "url must be an http(s) URL"),
        ({"url": f"{URL}/?x=1"}, "must not carry a query"),
        ({"url": f"{URL}/#frag"}, "must not carry a query or fragment"),
        ({"url": "https://robot:hunter2@registry.example"}, "must not embed a username or password"),
        ({"url": "https://robot@registry.example"}, "must not embed a username or password"),
        ({"url": "https://registry.example:notaport"}, "url has an invalid port"),
        ({"url": "https://registry.example:70000"}, "url has an invalid port"),
        ({"api_url": "not a url"}, "api_url must be an http(s) URL"),
        ({"api_url": f"{API_URL}/api/v2.0?x=1"}, "api_url must not carry a query"),
        ({"api_url": f"{API_URL}:99999"}, "api_url has an invalid port"),
        ({"token_url": "//no-scheme"}, "token_url must be an http(s) URL"),
        ({"token_url": "http://svc:abc/service/token"}, "token_url has an invalid port"),
        # kind-specific shape
        ({"projects": []}, "requires at least one entry in projects"),
        ({"repositories": ["cogs/alpha"]}, "does not read repositories"),
        ({"index_url": INDEX_URL}, "does not read index_url"),
        # projects grammar
        ({"projects": ["cogs/nested"]}, "not a registry project name"),
        ({"projects": ["Cogs"]}, "not a registry project name"),
        ({"projects": [""]}, "not a registry project name"),
        ({"projects": ["cogs", "cogs"]}, "lists 'cogs' twice"),
        ({"projects": [7]}, "Input should be a valid string"),
        # credentials and timeout
        ({"credentials": {"username": "robot$x"}}, "must be set together"),
        ({"credentials": {"password": "only"}}, "must be set together"),
        ({"request_timeout_seconds": 0}, "greater than 0"),
        ({"request_timeout_seconds": 61}, "less than or equal to 60"),
        # kind: pydantic path
        ({"kind": "quay"}, "Input should be 'harbor' or 'static'"),
        ({"kind": "Harbor"}, "Input should be 'harbor' or 'static'"),
    ],
)
def test_harbor_config_rejections(overrides: dict, fragment: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        CogRegistrySourceConfig(**harbor_config(**overrides))
    assert fragment in str(excinfo.value)


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"repositories": []}, "requires repositories and/or index_url"),
        ({"projects": ["cogs"]}, "does not read projects"),
        ({"api_url": API_URL}, "does not read api_url"),
        ({"webhook_secret": "s"}, "has no webhook"),
        ({"index_url": "index.example/catalog.v1.json"}, "index_url must be an http(s) URL"),
        # repository grammar: leading slash, dot-dot, uppercase, empty component, duplicates, non-string
        ({"repositories": ["/cogs/alpha"]}, "not an OCI repository path"),
        ({"repositories": ["cogs/../alpha"]}, "not an OCI repository path"),
        ({"repositories": ["cogs/Alpha"]}, "not an OCI repository path"),
        ({"repositories": ["cogs//alpha"]}, "not an OCI repository path"),
        ({"repositories": ["cogs/alpha/"]}, "not an OCI repository path"),
        ({"repositories": ["cogs/" + "a" * 260]}, "not an OCI repository path"),
        ({"repositories": ["cogs/alpha", "cogs/alpha"]}, "lists 'cogs/alpha' twice"),
        ({"repositories": [None]}, "Input should be a valid string"),
    ],
)
def test_static_config_rejections(overrides: dict, fragment: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        CogRegistrySourceConfig(**static_config(**overrides))
    assert fragment in str(excinfo.value)


def test_credentials_never_render_the_password() -> None:
    credentials = CogRegistryCredentials(username="robot$x", password="hunter2-secret")
    config = CogRegistrySourceConfig(**harbor_config(credentials=credentials, webhook_secret="hook-secret-value"))
    dumped = json.dumps(config.model_dump(mode="json"))
    for rendered in (repr(credentials), str(credentials), repr(config), str(config), dumped):
        assert "hunter2-secret" not in rendered
        assert "hook-secret-value" not in rendered
        assert "robot$x" in rendered
    # The values themselves are intact for the adapters.
    assert credentials.password.get_secret_value() == "hunter2-secret"
    assert config.webhook_secret.get_secret_value() == "hook-secret-value"


@pytest.mark.parametrize(
    "overrides",
    [
        {"credentials": {"username": "", "password": "hunter2-secret"}},  # the credentials rule itself
        {"credentials": {"username": "robot$x", "password": "hunter2-secret"}, "url": "nope"},  # another field
        {"credentials": {"username": "robot$x", "password": "hunter2-secret"}, "projects": []},  # a model rule
        {"webhook_secret": "hook-secret-value", "url": "nope"},
    ],
)
def test_validation_errors_never_echo_secrets(overrides: dict) -> None:
    with pytest.raises(ValidationError) as excinfo:
        CogRegistrySourceConfig(**harbor_config(**overrides))
    # str() is what a failed startup renders; errors() carries pydantic's raw
    # `input` by design, which is why hide_input_in_errors alone is not enough.
    text = str(excinfo.value) + repr(excinfo.value.errors(include_input=False))
    assert "hunter2-secret" not in text
    assert "hook-secret-value" not in text


def test_credentials_strip_whitespace_and_empty_is_unconfigured() -> None:
    assert CogRegistryCredentials(username=" u ", password=" p ").username == "u"
    assert not CogRegistryCredentials().configured
    assert not CogRegistrySourceConfig(**static_config(credentials={})).credentials.configured


# --- helpers ---------------------------------------------------------------


def test_reference_formats_and_validates() -> None:
    assert reference(HOST, "cogs/alpha", DIGEST_A) == f"{HOST}/cogs/alpha@{DIGEST_A}"
    assert reference("localhost:5000", "cogs/alpha", DIGEST_A) == f"localhost:5000/cogs/alpha@{DIGEST_A}"
    with pytest.raises(ValueError, match="bare host"):
        reference(f"{HOST}/", "cogs/alpha", DIGEST_A)
    with pytest.raises(ValueError, match="bare host"):
        reference("", "cogs/alpha", DIGEST_A)
    with pytest.raises(ValueError, match="repository path"):
        reference(HOST, "/cogs/alpha", DIGEST_A)
    with pytest.raises(ValueError, match="content digest"):
        reference(HOST, "cogs/alpha", "latest")


def test_registry_host_keeps_port_and_drops_everything_else() -> None:
    assert registry_host("https://registry.example") == "registry.example"
    assert registry_host("https://user:pw@registry.example:5000/v2/") == "registry.example:5000"
    assert registry_host("http://REGISTRY.example/") == "registry.example"
    assert registry_host("https://[2001:db8::1]:5000") == "[2001:db8::1]:5000"
    assert registry_host("https://[2001:db8::1]/") == "[2001:db8::1]"
    with pytest.raises(ValueError, match="no host"):
        registry_host("https:///nohost")


def test_ipv6_host_survives_into_the_reference() -> None:
    config = CogRegistrySourceConfig(**static_config(url="https://[2001:db8::1]:5000"))
    [source] = build_registry_sources([config], oci_client_factory=FakeOCIFactory())
    assert reference(source.host, "cogs/alpha", DIGEST_A) == f"[2001:db8::1]:5000/cogs/alpha@{DIGEST_A}"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-04T17:11:31.297Z", datetime(2026, 9, 4, 17, 11, 31, 297000, tzinfo=UTC)),
        ("2026-09-04T17:11:31Z", datetime(2026, 9, 4, 17, 11, 31, tzinfo=UTC)),
        ("2026-09-04T19:11:31+02:00", datetime(2026, 9, 4, 17, 11, 31, tzinfo=UTC)),
        ("2026-09-04T17:11:31", datetime(2026, 9, 4, 17, 11, 31, tzinfo=UTC)),
        ("0001-01-01T00:00:00.000Z", datetime(1, 1, 1, tzinfo=UTC)),
        ("yesterday", None),
        ("", None),
        (None, None),
        (1788000000, None),
    ],
)
def test_parse_timestamp(value, expected) -> None:
    parsed = parse_timestamp(value)
    assert parsed == expected
    if parsed is not None:
        assert parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)
        assert parsed.tzinfo in (UTC, timezone.utc)


def test_repository_path_grammar() -> None:
    assert is_repository_path("cogs/cog-qwen3b-e4cf9c5c")
    assert is_repository_path("openteams_capabilities/data-explorer")
    assert is_repository_path("a/b/c.d__e--f")
    assert not is_repository_path("cogs/.hidden")
    assert not is_repository_path("cogs/a..b")
    assert not is_repository_path(b"cogs/alpha")


def test_http_verify_default_and_missing_bundle(tmp_path) -> None:
    assert http_verify("") is True
    with pytest.raises(OSError):
        http_verify(str(tmp_path / "missing-ca.crt"))


def test_missing_ca_bundle_fails_at_construction(tmp_path) -> None:
    config = CogRegistrySourceConfig(**harbor_config(ca_bundle_path=str(tmp_path / "missing-ca.crt")))
    with pytest.raises(OSError):
        build_registry_sources([config], oci_client_factory=FakeOCIFactory())


# --- build_registry_sources: the startup choke point ----------------------


def test_build_registry_sources_dispatches_by_kind() -> None:
    configs = [CogRegistrySourceConfig(**harbor_config()), CogRegistrySourceConfig(**static_config())]
    sources = build_registry_sources(configs, oci_client_factory=FakeOCIFactory())
    assert [type(source) for source in sources] == [HarborRegistrySource, StaticRegistrySource]
    assert [source.id for source in sources] == ["harbor-main", "static-main"]
    assert all(isinstance(source, RegistrySource) for source in sources)
    assert build_registry_sources([], oci_client_factory=FakeOCIFactory()) == []


def test_build_registry_sources_refuses_duplicate_ids_before_building_anything() -> None:
    configs = [
        CogRegistrySourceConfig(**harbor_config(id="main")),
        CogRegistrySourceConfig(**static_config()),
        CogRegistrySourceConfig(**static_config(id="main")),
    ]
    factory = FakeOCIFactory()
    with pytest.raises(RuntimeError) as excinfo:
        build_registry_sources(configs, oci_client_factory=factory)
    message = str(excinfo.value)
    assert "cogs.registry.sources[2] reuses id 'main'" in message
    assert "sources[0]" in message
    assert "stored with every indexed row" in message
    assert factory.clients == []  # preflight refused before any client was constructed


def test_build_registry_sources_refuses_unknown_kind_at_runtime() -> None:
    # Literal catches this at parse time; model_construct bypasses validation
    # the way a future caller building configs by hand could.
    bogus = CogRegistrySourceConfig.model_construct(**harbor_config(kind="quay"))
    factory = FakeOCIFactory()
    with pytest.raises(RuntimeError) as excinfo:
        build_registry_sources([CogRegistrySourceConfig(**static_config()), bogus], oci_client_factory=factory)
    message = str(excinfo.value)
    assert "cogs.registry.sources[1] ('harbor-main') has unsupported kind 'quay'" in message
    assert "'harbor'" in message and "'static'" in message
    assert factory.clients == []


# --- parse_registry_event dispatch ----------------------------------------


class _ClaimingSource:
    """A source that claims every delivery, for dispatch-order tests."""

    def __init__(self, id: str, events: list[RegistryEvent] | None) -> None:
        self.id = id
        self.host = HOST
        self._events = events
        self.seen: list[WebhookRequest] = []

    async def list_repositories(self) -> list[str]:
        return []

    async def list_artifacts(self, repo: str) -> list[ArtifactRef]:
        return []

    def oci(self):
        raise AssertionError("not used")

    def parse_event(self, request: WebhookRequest) -> list[RegistryEvent] | None:
        self.seen.append(request)
        return self._events

    async def aclose(self) -> None:
        pass


def test_parse_registry_event_first_claim_wins() -> None:
    request = WebhookRequest(headers={"content-type": "application/json"}, body=b"{}")
    [static] = build_registry_sources([CogRegistrySourceConfig(**static_config())], oci_client_factory=FakeOCIFactory())
    ignoring = _ClaimingSource("ignoring", [])
    event = RegistryEvent(kind="push", repo="cogs/alpha", digest=DIGEST_A, tags=("latest",))
    claiming = _ClaimingSource("claiming", [event])
    assert isinstance(ignoring, RegistrySource)

    assert parse_registry_event([static, claiming, ignoring], request) == (claiming, [event])
    assert ignoring.seen == []  # dispatch stopped at the first claim

    # An empty list is a claim too: "mine, nothing to do" beats a later source's events.
    assert parse_registry_event([ignoring, claiming], request) == (ignoring, [])
    assert parse_registry_event([static, _ClaimingSource("silent", None)], request) is None
    assert parse_registry_event([], request) is None


# --- contract suite: every adapter, identical semantics -------------------


@pytest.fixture(params=["static-list", "static-index", "harbor"])
async def contract(request):
    """A built source of each kind, backed by mocked REST/index and the fake OCI client."""

    factory = FakeOCIFactory()
    requests: list[httpx.Request] = []
    if request.param == "harbor":
        config = CogRegistrySourceConfig(**harbor_config())
        transport = httpx.MockTransport(harbor_rest_handler(requests=requests))
    elif request.param == "static-index":
        config = CogRegistrySourceConfig(**static_config(repositories=["cogs/alpha"], index_url=INDEX_URL))

        def handler(req: httpx.Request) -> httpx.Response:
            requests.append(req)
            assert req.url.host == "index.example"
            assert "authorization" not in req.headers  # the registry credential never goes to the index
            return httpx.Response(200, content=index_document(["cogs/beta", "cogs/alpha"]))

        transport = httpx.MockTransport(handler)
    else:
        config = CogRegistrySourceConfig(**static_config(repositories=["cogs/beta", "cogs/alpha"]))
        transport = httpx.MockTransport(lambda req: httpx.Response(500))
    [source] = build_registry_sources([config], oci_client_factory=factory, http_transport=transport)
    yield source, factory, requests, config
    await source.aclose()


async def test_contract_list_repositories_sorted_unique(contract) -> None:
    source, _, _, _ = contract
    assert await source.list_repositories() == ["cogs/alpha", "cogs/beta"]
    assert await source.list_repositories() == ["cogs/alpha", "cogs/beta"]  # stable across calls


async def test_contract_list_artifacts_semantics(contract) -> None:
    source, _, _, _ = contract
    refs = await source.list_artifacts("cogs/alpha")
    assert refs == [
        ArtifactRef(digest=DIGEST_A, tags=("latest", "v1"), pushed_at=PUSHED_A, media_type=MEDIA_TYPE_OCI_MANIFEST),
        ArtifactRef(digest=DIGEST_B, tags=("v0",), pushed_at=PUSHED_B, media_type=MEDIA_TYPE_OCI_MANIFEST),
    ]
    assert all(ref.pushed_at.tzinfo is not None for ref in refs)
    assert await source.list_artifacts("cogs/beta") == [
        ArtifactRef(digest=DIGEST_C, tags=("latest",), pushed_at=None, media_type=MEDIA_TYPE_OCI_MANIFEST)
    ]
    for ref in refs:
        assert reference(source.host, "cogs/alpha", ref.digest) == f"{HOST}/cogs/alpha@{ref.digest}"


async def test_contract_oci_is_one_preauthenticated_client(contract) -> None:
    source, factory, _, config = contract
    client = source.oci()
    assert client is source.oci()
    assert factory.clients == [client]
    assert client.credentials == BasicCredentials(CREDENTIALS["username"], CREDENTIALS["password"])
    assert client.token_url == (config.token_url or None)
    assert client.timeout_seconds == config.request_timeout_seconds
    assert client.ca_bundle_path is None


async def test_contract_host_derives_from_url_never_api_url(contract) -> None:
    source, factory, requests, config = contract
    assert source.host == HOST
    await source.list_repositories()
    for req in requests:
        assert req.url.host != HOST  # enumeration traffic went to the API/index host, identity did not follow
    if config.kind == "harbor":
        assert {req.url.host for req in requests} == {API_HOST}
        assert factory.clients[0].base_url == API_URL  # OCI traffic stays in-cluster too
    else:
        assert factory.clients[0].base_url == URL


async def test_contract_aclose_is_idempotent(contract) -> None:
    source, factory, _, _ = contract
    await source.aclose()
    await source.aclose()
    assert factory.clients[0].closed == 1


async def test_contract_aclose_still_closes_oci_when_the_http_client_fails(contract, monkeypatch) -> None:
    source, factory, _, _ = contract
    http = getattr(source, "_http", None)
    if http is None:
        pytest.skip("this variant owns no HTTP client of its own")

    async def boom() -> None:
        raise RuntimeError("close failed")

    monkeypatch.setattr(http, "aclose", boom)
    with pytest.raises(RuntimeError, match="close failed"):
        await source.aclose()
    assert factory.clients[0].closed == 1
    await source.aclose()  # and a retry is a no-op, not a second failure


async def test_contract_parse_event_rejects_foreign_payloads(contract) -> None:
    source, _, _, _ = contract
    assert source.parse_event(WebhookRequest(headers={}, body=b'{"action": "push"}')) is None
    assert source.parse_event(WebhookRequest(headers={}, body=b"not json")) is None
    assert source.parse_event(WebhookRequest(headers={}, body=b"")) is None


# --- static adapter specifics --------------------------------------------


def static_source(handler=None, **overrides):
    factory = FakeOCIFactory()
    transport = httpx.MockTransport(handler or (lambda req: httpx.Response(500)))
    [source] = build_registry_sources(
        [CogRegistrySourceConfig(**static_config(**overrides))], oci_client_factory=factory, http_transport=transport
    )
    return source, factory.clients[0]


async def test_static_list_artifacts_bounds_tags_and_skips_vanished_ones(caplog) -> None:
    source, client = static_source()
    client.tags["cogs/alpha"] = [f"tag-{i:03d}" for i in range(MAX_TAGS_PER_REPOSITORY + 50)] + ["gone", "v1"]
    for i in range(MAX_TAGS_PER_REPOSITORY + 50):
        client.manifests[("cogs/alpha", f"tag-{i:03d}")] = client.manifests[("cogs/alpha", "v1")]
    with caplog.at_level(logging.WARNING):
        refs = await source.list_artifacts("cogs/alpha")
    fetched = [call for call in client.calls if call[0] == "get_manifest"]
    assert len(fetched) == MAX_TAGS_PER_REPOSITORY
    assert ("get_manifest", "cogs/alpha", "gone") in fetched  # sorted: "gone" precedes "tag-*"; no manifest → skipped
    assert any("enumerating the first 200" in record.message for record in caplog.records)
    assert [ref.digest for ref in refs] == [DIGEST_A]
    assert "gone" not in refs[0].tags and "v1" not in refs[0].tags  # v1 sorted past the cap
    assert len(refs[0].tags) == MAX_TAGS_PER_REPOSITORY - 1


async def test_static_list_artifacts_propagates_other_oci_errors() -> None:
    source, client = static_source()
    with pytest.raises(OCINotFound):
        await source.list_artifacts("cogs/unknown")


@pytest.mark.parametrize(
    ("response", "fragment"),
    [
        (lambda: httpx.Response(404, json={}), "answered HTTP 404"),
        (lambda: httpx.Response(200, content=b"x" * (MAX_INDEX_BYTES + 1)), "declares"),
        (lambda: httpx.Response(200, stream=httpx.ByteStream(b"x" * (MAX_INDEX_BYTES + 1))), "exceeds the"),
        (lambda: httpx.Response(200, content=b"{"), "not valid JSON"),
        (lambda: httpx.Response(200, content=b"[]"), "must be a JSON object"),
        (lambda: httpx.Response(200, json={"schemaVersion": 2, "repositories": []}), "schemaVersion 2"),
        (lambda: httpx.Response(200, json={"repositories": []}), "schemaVersion None"),
        (lambda: httpx.Response(200, json={"schemaVersion": 1}), "must carry a 'repositories' list"),
        (
            lambda: httpx.Response(200, json={"schemaVersion": 1, "repositories": {}}),
            "must carry a 'repositories' list",
        ),
        (lambda: httpx.Response(200, json={"schemaVersion": 1, "repositories": ["cogs/alpha"]}), "must be an object"),
        (
            lambda: httpx.Response(200, json={"schemaVersion": 1, "repositories": [{"namespace": "cogs"}]}),
            "needs string 'namespace' and 'name'",
        ),
        (
            lambda: httpx.Response(200, json={"schemaVersion": 1, "repositories": [{"namespace": 1, "name": "a"}]}),
            "needs string 'namespace' and 'name'",
        ),
        (
            lambda: httpx.Response(
                200, json={"schemaVersion": 1, "repositories": [{"namespace": "cogs", "name": "../x"}]}
            ),
            "not an OCI repository path: 'cogs/../x'",
        ),
        (
            lambda: httpx.Response(200, json={"schemaVersion": 1, "repositories": [{"namespace": "", "name": "x"}]}),
            "not an OCI repository path",
        ),
        (lambda: httpx.Response(200, content=index_document(["cogs/a", "cogs/a"])), "lists 'cogs/a' twice"),
    ],
)
async def test_static_index_rejections(response, fragment: str) -> None:
    source, _ = static_source(lambda req: response(), repositories=[], index_url=INDEX_URL)
    with pytest.raises(RegistrySourceProtocolError) as excinfo:
        await source.list_repositories()
    assert fragment in str(excinfo.value)
    assert "source 'static-main'" in str(excinfo.value)


async def test_static_index_transport_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    source, _ = static_source(handler, repositories=[], index_url=INDEX_URL)
    with pytest.raises(RegistrySourceError, match="fetching index .* failed"):
        await source.list_repositories()


def test_parse_index_document_accepts_the_live_shape() -> None:
    body = json.dumps(
        {
            "schemaVersion": 1,
            "repositories": [
                {"namespace": "openteams_capabilities", "name": "data-explorer"},
                {"namespace": "acme", "name": "cog-alpha-1a2b3c4d", "extra": "ignored"},
            ],
        }
    ).encode()
    assert parse_index_document(body) == ["acme/cog-alpha-1a2b3c4d", "openteams_capabilities/data-explorer"]
