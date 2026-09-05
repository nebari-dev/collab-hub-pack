"""Tests for the generic OCI Distribution client (``cogs/oci.py``).

Everything runs against an in-process fake registry behind ``httpx.MockTransport``
so the protocol edges (challenge shapes, size caps, digest tampering, redirects)
are exercised deterministically. One opt-in live test at the bottom talks to a
real registry when ``COLLAB_HUB_TEST_OCI_URL`` is set.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass, field
from urllib.parse import parse_qs

import httpx
import pytest

from collab_hub_api.cogs import oci
from collab_hub_api.cogs.oci import (
    MEDIA_TYPE_DOCKER_MANIFEST,
    MEDIA_TYPE_NEBI_ASSET,
    MEDIA_TYPE_OCI_INDEX,
    MEDIA_TYPE_OCI_MANIFEST,
    MEDIA_TYPE_PIXI_CONFIG,
    MEDIA_TYPE_PIXI_LOCK,
    MEDIA_TYPE_PIXI_TOML,
    TITLE_ANNOTATION,
    BasicCredentials,
    Descriptor,
    Manifest,
    OCIAuthError,
    OCIClient,
    OCIDigestMismatch,
    OCIInvalidReference,
    OCINotFound,
    OCIProtocolError,
    OCITooLarge,
    fetch_bundle_files,
    select_bundle_layers,
)

REGISTRY = "https://registry.example"
REALM = "https://auth.example/token"
OVERRIDE_TOKEN_URL = "https://registry.example/service/token"
BLOB_STORE = "https://blobs.example"
REPO = "cogs/cog-transcriber-105e86a3"
CREDS = BasicCredentials("robot$indexer", "robot-secret")


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def layer(media_type: str, data: bytes, title: str | None) -> dict:
    entry = {"mediaType": media_type, "digest": sha256(data), "size": len(data)}
    if title is not None:
        entry["annotations"] = {TITLE_ANNOTATION: title}
    return entry


# Synthesized from the shape Nebi publishes: an empty pixi config, pixi.toml,
# a large pixi.lock, then one asset layer per bundled file titled by path.
COG_MD = b"---\nname: transcriber\nmanifest: pixi.toml\n---\n# Transcriber\n"
PIXI_TOML = b'[workspace]\nname = "transcriber"\n\n[tool.cog]\nid = "example/transcriber"\n'
PIXI_LOCK = b"version: 6\nenvironments:\n" + b"  - x\n" * 4000
COG_YAML = b"id: example/transcriber\n"
SYSTEM_MD = b"You transcribe audio.\n"
CONFIG = b"{}"

BLOBS: dict[str, bytes] = {sha256(b): b for b in (COG_MD, PIXI_TOML, PIXI_LOCK, COG_YAML, SYSTEM_MD, CONFIG)}


def build_manifest(*, include_pixi: bool = True, media_type: str = MEDIA_TYPE_OCI_MANIFEST) -> bytes:
    layers = []
    if include_pixi:
        layers.append(layer(MEDIA_TYPE_PIXI_TOML, PIXI_TOML, "pixi.toml"))
        layers.append(layer(MEDIA_TYPE_PIXI_LOCK, PIXI_LOCK, "pixi.lock"))
    layers.append(layer(MEDIA_TYPE_NEBI_ASSET, COG_MD, "COG.md"))
    layers.append(layer(MEDIA_TYPE_NEBI_ASSET, COG_YAML, "cog.yaml"))
    layers.append(layer(MEDIA_TYPE_NEBI_ASSET, SYSTEM_MD, "context/system.md"))
    layers.append({"mediaType": MEDIA_TYPE_NEBI_ASSET, "digest": sha256(b"untitled"), "size": 8})
    document = {
        "schemaVersion": 2,
        "mediaType": media_type,
        "config": {"mediaType": MEDIA_TYPE_PIXI_CONFIG, "digest": sha256(CONFIG), "size": len(CONFIG)},
        "layers": layers,
        "annotations": {"org.opencontainers.image.created": "2026-09-04T17:11:14Z"},
    }
    return json.dumps(document, separators=(",", ":")).encode()


MANIFEST = build_manifest()
MANIFEST_DIGEST = sha256(MANIFEST)


def basic_header(creds: BasicCredentials) -> str:
    return "Basic " + base64.b64encode(f"{creds.username}:{creds.password}".encode()).decode()


@dataclass
class FakeRegistry:
    """A registry speaking just enough of the distribution API for the client.

    ``auth`` selects the challenge the registry issues: ``"bearer"`` (anonymous
    tokens), ``"bearer-basic"`` (the token endpoint demands the credential),
    ``"basic"`` (no token endpoint at all) or ``"open"`` (no challenge).
    """

    auth: str = "bearer"
    manifests: dict[str, bytes] = field(default_factory=lambda: {"latest": MANIFEST, MANIFEST_DIGEST: MANIFEST})
    blobs: dict[str, bytes] = field(default_factory=lambda: dict(BLOBS))
    tags: list[str] = field(default_factory=lambda: ["latest", "v1", "v2"])
    tag_page_size: int = 0
    send_content_digest: bool = True
    content_digest_override: str | None = None
    blob_redirect: bool = False
    blob_content_length: str | None = "auto"
    blob_body_override: bytes | None = None
    manifest_content_length: str | None = "auto"
    token_ttl: int = 300
    token_field: str = "token"
    reject_all_tokens: bool = False
    challenge_scope: str | None = None
    requests: list[httpx.Request] = field(default_factory=list)
    token_requests: list[httpx.Request] = field(default_factory=list)
    issued: int = 0
    valid_tokens: set[str] = field(default_factory=set)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # -- routing ------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        if host == "blobs.example":
            return self.blob_store(request)
        if request.url.path == "/service/token" or host == "auth.example":
            return self.token(request)
        assert host == "registry.example", request.url
        denied = self.check_auth(request)
        if denied is not None:
            return denied
        path = request.url.path
        if path.startswith(f"/v2/{REPO}/manifests/"):
            return self.manifest(path.rsplit("/", 1)[1], request)
        if path.startswith(f"/v2/{REPO}/blobs/"):
            return self.blob(path.rsplit("/", 1)[1], request)
        if path == f"/v2/{REPO}/tags/list":
            return self.tag_list(request)
        return httpx.Response(404, json={"errors": [{"code": "NAME_UNKNOWN"}]})

    def challenge(self) -> str:
        scope = self.challenge_scope or f"repository:{REPO}:pull"
        if self.auth == "basic":
            return 'Basic realm="registry"'
        return f'Bearer realm="{REALM}",service="registry.example",scope="{scope}"'

    def check_auth(self, request: httpx.Request) -> httpx.Response | None:
        if self.auth == "open":
            return None
        authorization = request.headers.get("Authorization", "")
        if self.auth == "basic":
            ok = authorization == basic_header(CREDS)
        else:
            token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
            ok = bool(token) and token in self.valid_tokens and not self.reject_all_tokens
        if ok:
            return None
        return httpx.Response(401, headers={"WWW-Authenticate": self.challenge()}, json={"errors": []})

    # -- endpoints ----------------------------------------------------------

    def token(self, request: httpx.Request) -> httpx.Response:
        self.token_requests.append(request)
        if self.auth == "bearer-basic" and request.headers.get("Authorization") != basic_header(CREDS):
            return httpx.Response(401, json={"errors": [{"code": "UNAUTHORIZED"}]})
        self.issued += 1
        token = f"tok-{self.issued}"
        self.valid_tokens.add(token)
        return httpx.Response(200, json={self.token_field: token, "expires_in": self.token_ttl})

    def manifest(self, ref: str, request: httpx.Request) -> httpx.Response:
        body = self.manifests.get(ref)
        if body is None:
            return httpx.Response(404, json={"errors": [{"code": "MANIFEST_UNKNOWN"}]})
        assert MEDIA_TYPE_OCI_INDEX in request.headers["Accept"]
        try:
            media_type = json.loads(body).get("mediaType", MEDIA_TYPE_OCI_MANIFEST)
        except (ValueError, AttributeError):
            media_type = MEDIA_TYPE_OCI_MANIFEST
        headers = {"Content-Type": media_type}
        if self.send_content_digest:
            headers["Docker-Content-Digest"] = self.content_digest_override or sha256(body)
        return self._body_response(body, headers, self.manifest_content_length)

    def blob(self, digest: str, request: httpx.Request) -> httpx.Response:
        if self.blob_redirect:
            return httpx.Response(307, headers={"Location": f"{BLOB_STORE}/store/{digest}?sig=abc"})
        return self._blob_body(digest)

    def blob_store(self, request: httpx.Request) -> httpx.Response:
        # Object storage never sees the registry credential.
        assert "Authorization" not in request.headers, "credential leaked to the redirect target"
        return self._blob_body(request.url.path.rsplit("/", 1)[1])

    def _blob_body(self, digest: str) -> httpx.Response:
        body = self.blobs.get(digest)
        if body is None:
            return httpx.Response(404, json={"errors": [{"code": "BLOB_UNKNOWN"}]})
        if self.blob_body_override is not None:
            body = self.blob_body_override
        return self._body_response(body, {"Content-Type": "application/octet-stream"}, self.blob_content_length)

    def tag_list(self, request: httpx.Request) -> httpx.Response:
        tags = self.tags
        headers: dict[str, str] = {}
        if self.tag_page_size:
            params = parse_qs(request.url.query.decode())
            start = 0
            if "last" in params:
                start = tags.index(params["last"][0]) + 1
            page = tags[start : start + self.tag_page_size]
            tags = page
            if start + self.tag_page_size < len(self.tags):
                headers["Link"] = f'</v2/{REPO}/tags/list?n={self.tag_page_size}&last={page[-1]}>; rel="next"'
        return httpx.Response(200, headers=headers, json={"name": REPO, "tags": tags})

    @staticmethod
    def _body_response(body: bytes, headers: dict[str, str], content_length: str | None) -> httpx.Response:
        if content_length == "auto":
            headers["Content-Length"] = str(len(body))
        elif content_length is not None:
            headers["Content-Length"] = content_length
        if content_length is None:
            # Chunked: hand httpx an async iterator so no Content-Length is synthesized.
            async def chunks():
                for start in range(0, len(body), 1024):
                    yield body[start : start + 1024]

            return httpx.Response(200, headers=headers, stream=_AsyncChunks(chunks()))
        return httpx.Response(200, headers=headers, content=body)


class _AsyncChunks(httpx.AsyncByteStream):
    def __init__(self, iterator):
        self._iterator = iterator

    async def __aiter__(self):
        async for chunk in self._iterator:
            yield chunk


def client_for(registry: FakeRegistry, **kwargs) -> OCIClient:
    return OCIClient(REGISTRY, transport=registry.transport(), **kwargs)


# --- authentication -------------------------------------------------------


async def test_anonymous_bearer_challenge_then_manifest():
    registry = FakeRegistry(auth="bearer")
    async with client_for(registry) as client:
        manifest = await client.get_manifest(REPO, "latest")

    assert manifest.digest == MANIFEST_DIGEST
    assert manifest.media_type == MEDIA_TYPE_OCI_MANIFEST
    assert manifest.config is not None and manifest.config.media_type == MEDIA_TYPE_PIXI_CONFIG
    assert [lyr.title for lyr in manifest.layers][:3] == ["pixi.toml", "pixi.lock", "COG.md"]
    assert manifest.layer_by_title("context/system.md") is not None
    assert manifest.layers[-1].title is None
    assert manifest.annotations["org.opencontainers.image.created"].startswith("2026")
    assert manifest.raw == MANIFEST

    # Anonymous: the token request carries no credential and passes the challenge's params.
    (token_request,) = registry.token_requests
    assert token_request.url.host == "auth.example"
    assert "Authorization" not in token_request.headers
    params = parse_qs(token_request.url.query.decode())
    assert params == {"service": ["registry.example"], "scope": [f"repository:{REPO}:pull"]}
    # First attempt anonymous, then token, then retry with the bearer token.
    manifest_requests = [r for r in registry.requests if "/manifests/" in r.url.path]
    assert len(manifest_requests) == 2
    assert "Authorization" not in manifest_requests[0].headers
    assert manifest_requests[1].headers["Authorization"] == "Bearer tok-1"


async def test_bearer_token_endpoint_receives_basic_credentials():
    registry = FakeRegistry(auth="bearer-basic")
    async with client_for(registry, credentials=CREDS) as client:
        manifest = await client.get_manifest(REPO, "latest")
    assert manifest.digest == MANIFEST_DIGEST
    (token_request,) = registry.token_requests
    assert token_request.headers["Authorization"] == basic_header(CREDS)
    # The password never reaches the registry itself, only the token endpoint.
    for request in registry.requests:
        if request.url.host == "registry.example":
            assert not request.headers.get("Authorization", "").startswith("Basic ")


async def test_token_endpoint_rejecting_credentials_is_auth_error():
    registry = FakeRegistry(auth="bearer-basic")
    async with client_for(registry, credentials=BasicCredentials("robot$indexer", "wrong")) as client:
        with pytest.raises(OCIAuthError) as excinfo:
            await client.get_manifest(REPO, "latest")
    assert "wrong" not in str(excinfo.value)


async def test_second_401_after_token_is_auth_error():
    registry = FakeRegistry(auth="bearer", reject_all_tokens=True)
    async with client_for(registry) as client:
        with pytest.raises(OCIAuthError, match="rejected the credential"):
            await client.get_manifest(REPO, "latest")
    # Exactly one retry: anonymous, token, retry, stop.
    assert len([r for r in registry.requests if "/manifests/" in r.url.path]) == 2
    assert len(registry.token_requests) == 1


async def test_basic_challenge_uses_credentials_directly_and_then_proactively():
    registry = FakeRegistry(auth="basic")
    async with client_for(registry, credentials=CREDS) as client:
        await client.get_manifest(REPO, "latest")
        tags = await client.list_tags(REPO)
    assert tags == ["latest", "v1", "v2"]
    assert registry.token_requests == []
    statuses = [r.headers.get("Authorization", "") for r in registry.requests]
    # First: anonymous probe; second: basic retry; third (tags): basic sent up front.
    assert statuses[0] == ""
    assert statuses[1] == basic_header(CREDS)
    assert statuses[2] == basic_header(CREDS)
    assert len(registry.requests) == 3


async def test_basic_challenge_without_credentials_is_auth_error():
    registry = FakeRegistry(auth="basic")
    async with client_for(registry) as client:
        with pytest.raises(OCIAuthError, match="none are configured"):
            await client.list_tags(REPO)


async def test_401_without_challenge_is_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    async with OCIClient(REGISTRY, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OCIAuthError, match="without a Bearer or Basic challenge"):
            await client.list_tags(REPO)


async def test_bearer_challenge_without_realm_is_protocol_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, headers={"WWW-Authenticate": 'Bearer service="x"'})

    async with OCIClient(REGISTRY, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OCIProtocolError, match="no usable realm"):
            await client.list_tags(REPO)


async def test_token_url_override_replaces_realm_but_keeps_service_and_scope():
    registry = FakeRegistry(auth="bearer-basic")
    async with client_for(registry, credentials=CREDS, token_url=OVERRIDE_TOKEN_URL) as client:
        await client.get_manifest(REPO, "latest")
    (token_request,) = registry.token_requests
    assert token_request.url.host == "registry.example"
    assert token_request.url.path == "/service/token"
    params = parse_qs(token_request.url.query.decode())
    assert params == {"service": ["registry.example"], "scope": [f"repository:{REPO}:pull"]}
    assert not any(r.url.host == "auth.example" for r in registry.requests)


async def test_token_cached_per_scope_and_refreshed_after_expiry(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(oci, "_monotonic", lambda: now[0])
    registry = FakeRegistry(auth="bearer", token_ttl=120)
    async with client_for(registry) as client:
        await client.get_manifest(REPO, "latest")
        assert len(registry.token_requests) == 1
        # Second and third calls reuse the cached token: no challenge, no token request.
        await client.list_tags(REPO)
        await client.get_blob(REPO, sha256(COG_MD), max_bytes=1024)
        assert len(registry.token_requests) == 1
        assert all(
            r.headers.get("Authorization") == "Bearer tok-1"
            for r in registry.requests[-2:]
        )
        # Past expiry (minus the safety margin) the token is dropped and re-fetched.
        now[0] += 120 - oci.TOKEN_EXPIRY_MARGIN_SECONDS
        await client.list_tags(REPO)
        assert len(registry.token_requests) == 2
        assert registry.requests[-1].headers["Authorization"] == "Bearer tok-2"


async def test_token_cache_keyed_by_challenge_scope_not_hint():
    # A registry may answer with a broader scope than the client guessed; the
    # cached token is still found on the next request to the same repository.
    registry = FakeRegistry(auth="bearer", challenge_scope=f"repository:{REPO}:pull,push")
    async with client_for(registry) as client:
        await client.get_manifest(REPO, "latest")
        await client.list_tags(REPO)
    assert len(registry.token_requests) == 1


async def test_short_lived_token_is_not_cached():
    registry = FakeRegistry(auth="bearer", token_ttl=5)
    async with client_for(registry) as client:
        await client.list_tags(REPO)
        await client.list_tags(REPO)
    assert len(registry.token_requests) == 2


async def test_access_token_field_and_default_ttl_accepted():
    registry = FakeRegistry(auth="bearer", token_field="access_token")
    async with client_for(registry) as client:
        await client.list_tags(REPO)
        await client.list_tags(REPO)
    assert len(registry.token_requests) == 1


async def test_token_response_without_token_is_protocol_error():
    registry = FakeRegistry(auth="bearer", token_field="nonsense")
    async with client_for(registry) as client:
        with pytest.raises(OCIProtocolError, match="carries no token"):
            await client.list_tags(REPO)


async def test_token_endpoint_server_error_is_protocol_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.example":
            return httpx.Response(503)
        return httpx.Response(401, headers={"WWW-Authenticate": f'Bearer realm="{REALM}"'})

    async with OCIClient(REGISTRY, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OCIProtocolError, match="HTTP 503"):
            await client.list_tags(REPO)


async def test_stale_cached_token_triggers_one_refresh(monkeypatch):
    # The registry revokes tok-1 while it is still cached; the 401 is answered
    # by a fresh challenge round, not surfaced as an auth failure.
    registry = FakeRegistry(auth="bearer")
    async with client_for(registry) as client:
        await client.list_tags(REPO)
        registry.valid_tokens.clear()
        tags = await client.list_tags(REPO)
    assert tags == ["latest", "v1", "v2"]
    assert len(registry.token_requests) == 2


def test_parse_challenges_handles_quoting_multiple_schemes_and_lines():
    parsed = oci._parse_challenges(
        [
            'Bearer realm="https://auth.example/token",service=registry.example,scope="repository:a/b:pull,push"',
            'Basic realm="Registry \\"Realm\\"", Negotiate',
        ]
    )
    assert [c.scheme for c in parsed] == ["bearer", "basic", "negotiate"]
    assert parsed[0].params == {
        "realm": "https://auth.example/token",
        "service": "registry.example",
        "scope": "repository:a/b:pull,push",
    }
    assert parsed[1].params == {"realm": 'Registry "Realm"'}
    assert parsed[2].params == {}
    assert oci._parse_challenges(["realm=orphan"]) == []


def test_credentials_repr_hides_password():
    assert "robot-secret" not in repr(CREDS)
    assert "robot$indexer" in repr(CREDS)


# --- manifests --------------------------------------------------------------


async def test_get_manifest_by_digest_verifies_body():
    registry = FakeRegistry(auth="open")
    registry.manifests[MANIFEST_DIGEST] = build_manifest(include_pixi=False)  # wrong body for that digest
    async with client_for(registry) as client:
        with pytest.raises(OCIDigestMismatch, match="requested digest"):
            await client.get_manifest(REPO, MANIFEST_DIGEST)


async def test_get_manifest_docker_content_digest_mismatch():
    registry = FakeRegistry(auth="open", content_digest_override=sha256(b"something else"))
    async with client_for(registry) as client:
        with pytest.raises(OCIDigestMismatch, match="Docker-Content-Digest"):
            await client.get_manifest(REPO, "latest")


async def test_get_manifest_malformed_content_digest_header():
    registry = FakeRegistry(auth="open", content_digest_override="md5:abc")
    async with client_for(registry) as client:
        with pytest.raises(OCIProtocolError, match="malformed Docker-Content-Digest"):
            await client.get_manifest(REPO, "latest")


async def test_get_manifest_without_content_digest_computes_sha256():
    registry = FakeRegistry(auth="open", send_content_digest=False)
    async with client_for(registry) as client:
        manifest = await client.get_manifest(REPO, "latest")
    assert manifest.digest == MANIFEST_DIGEST


async def test_get_manifest_oversized_by_content_length_and_by_body():
    registry = FakeRegistry(auth="open")
    async with client_for(registry, max_manifest_bytes=100) as client:
        with pytest.raises(OCITooLarge):
            await client.get_manifest(REPO, "latest")
    # A body that is larger than the (absent) Content-Length promised is still capped.
    registry = FakeRegistry(auth="open", manifest_content_length=None)
    async with client_for(registry, max_manifest_bytes=100) as client:
        with pytest.raises(OCITooLarge, match="exceeds"):
            await client.get_manifest(REPO, "latest")
    assert all(r.url.host == "registry.example" for r in registry.requests)


async def test_get_manifest_lying_content_length_is_still_capped():
    registry = FakeRegistry(auth="open", manifest_content_length="10")
    async with client_for(registry, max_manifest_bytes=100) as client:
        with pytest.raises(OCITooLarge, match="exceeds"):
            await client.get_manifest(REPO, "latest")


async def test_get_manifest_not_found_and_unexpected_status():
    registry = FakeRegistry(auth="open")
    async with client_for(registry) as client:
        with pytest.raises(OCINotFound):
            await client.get_manifest(REPO, "missing")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>gateway</html>")

    async with OCIClient(REGISTRY, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OCIProtocolError, match="HTTP 502") as excinfo:
            await client.get_manifest(REPO, "latest")
    assert "html" not in str(excinfo.value)


async def test_get_manifest_malformed_documents():
    registry = FakeRegistry(auth="open", send_content_digest=False)
    cases = {
        b"not json": "not valid JSON",
        b"[]": "not a JSON object",
        json.dumps({"mediaType": MEDIA_TYPE_OCI_MANIFEST, "layers": {}}).encode(): "'layers' is not a list",
        json.dumps({"mediaType": MEDIA_TYPE_OCI_MANIFEST, "layers": ["x"]}).encode(): "not an object",
        json.dumps({"mediaType": MEDIA_TYPE_OCI_MANIFEST, "layers": [{"digest": sha256(b""), "size": 0}]}).encode():
            "no mediaType",
        json.dumps(
            {"mediaType": MEDIA_TYPE_OCI_MANIFEST, "layers": [{"mediaType": "x", "digest": "sha256:zz", "size": 0}]}
        ).encode(): "malformed digest",
        json.dumps(
            {"mediaType": MEDIA_TYPE_OCI_MANIFEST, "layers": [{"mediaType": "x", "digest": sha256(b""), "size": -1}]}
        ).encode(): "malformed size",
        json.dumps({"mediaType": MEDIA_TYPE_OCI_MANIFEST, "config": 3}).encode(): "config descriptor is not an object",
    }
    async with client_for(registry) as client:
        for body, message in cases.items():
            registry.manifests["latest"] = body
            with pytest.raises(OCIProtocolError, match=message):
                await client.get_manifest(REPO, "latest")


async def test_get_manifest_falls_back_to_content_type_media_type():
    body = json.dumps({"schemaVersion": 2, "layers": []}).encode()
    registry = FakeRegistry(auth="open", send_content_digest=False)
    registry.manifests["latest"] = body
    async with client_for(registry) as client:
        manifest = await client.get_manifest(REPO, "latest")
    # The fake sends Content-Type from the document's mediaType, defaulting to the OCI manifest type.
    assert manifest.media_type == MEDIA_TYPE_OCI_MANIFEST
    assert manifest.config is None and manifest.layers == ()


async def test_get_manifest_docker_v2_media_type_parses():
    registry = FakeRegistry(auth="open", send_content_digest=False)
    registry.manifests["latest"] = build_manifest(media_type=MEDIA_TYPE_DOCKER_MANIFEST)
    async with client_for(registry) as client:
        manifest = await client.get_manifest(REPO, "latest")
    assert manifest.media_type == MEDIA_TYPE_DOCKER_MANIFEST
    assert manifest.layer_by_title("COG.md") is not None


def index_of(*children: bytes, media_type: str = MEDIA_TYPE_OCI_INDEX) -> bytes:
    document = {
        "schemaVersion": 2,
        "mediaType": media_type,
        "manifests": [
            {"mediaType": MEDIA_TYPE_OCI_MANIFEST, "digest": sha256(child), "size": len(child)} for child in children
        ],
    }
    return json.dumps(document).encode()


async def test_index_resolves_to_child_with_pixi_layer():
    no_pixi = build_manifest(include_pixi=False)
    registry = FakeRegistry(auth="bearer")
    registry.manifests["latest"] = index_of(no_pixi, MANIFEST)
    registry.manifests[sha256(no_pixi)] = no_pixi
    async with client_for(registry) as client:
        manifest = await client.get_manifest(REPO, "latest")
    assert manifest.digest == MANIFEST_DIGEST
    assert manifest.layer_by_title("pixi.toml") is not None
    fetched = [r.url.path.rsplit("/", 1)[1] for r in registry.requests if "/manifests/" in r.url.path]
    assert fetched[-2:] == [sha256(no_pixi), MANIFEST_DIGEST]
    # One token for the whole walk.
    assert len(registry.token_requests) == 1


async def test_index_without_pixi_child_is_protocol_error():
    no_pixi = build_manifest(include_pixi=False)
    nested = index_of(MANIFEST)
    registry = FakeRegistry(auth="open")
    registry.manifests["latest"] = index_of(no_pixi, nested)
    registry.manifests[sha256(no_pixi)] = no_pixi
    registry.manifests[sha256(nested)] = nested  # nested indexes are skipped, not recursed
    async with client_for(registry) as client:
        with pytest.raises(OCIProtocolError, match="no child manifest carrying a pixi.toml layer"):
            await client.get_manifest(REPO, "latest")


async def test_index_children_are_bounded_and_validated():
    registry = FakeRegistry(auth="open")
    filler = [build_manifest(include_pixi=False).replace(b"2026", str(2000 + i).encode()) for i in range(10)]
    for body in filler:
        registry.manifests[sha256(body)] = body
    # The pixi child sits past the bound, so it is never reached.
    registry.manifests["latest"] = index_of(*filler, MANIFEST)
    async with client_for(registry) as client:
        with pytest.raises(OCIProtocolError):
            await client.get_manifest(REPO, "latest")
    assert len([r for r in registry.requests if "/manifests/sha256:" in r.url.path]) == oci.MAX_INDEX_CHILDREN

    bad_child = {"mediaType": MEDIA_TYPE_OCI_INDEX, "manifests": [{"digest": "x"}]}
    registry.manifests["latest"] = json.dumps(bad_child).encode()
    async with client_for(registry) as client:
        with pytest.raises(OCIProtocolError, match="malformed digest"):
            await client.get_manifest(REPO, "latest")
    registry.manifests["latest"] = json.dumps({"mediaType": MEDIA_TYPE_OCI_INDEX, "manifests": 1}).encode()
    async with client_for(registry) as client:
        with pytest.raises(OCIProtocolError, match="no 'manifests' list"):
            await client.get_manifest(REPO, "latest")


def test_is_index_falls_back_to_document_shape():
    assert oci._is_index("", b'{"manifests": []}') is True
    assert oci._is_index("", b'{"layers": []}') is False
    assert oci._is_index("", b"garbage") is False
    assert oci._is_index(MEDIA_TYPE_OCI_MANIFEST, b'{"manifests": []}') is False


# --- blobs --------------------------------------------------------------------


async def test_get_blob_returns_verified_bytes():
    registry = FakeRegistry(auth="bearer")
    async with client_for(registry) as client:
        manifest = await client.get_manifest(REPO, "latest")
        descriptor = manifest.layer_by_title("COG.md")
        assert descriptor is not None
        assert await client.get_blob(REPO, descriptor, max_bytes=1024) == COG_MD
        assert await client.get_blob(REPO, descriptor.digest, max_bytes=1024) == COG_MD


async def test_get_blob_tampered_byte_is_rejected():
    tampered = bytearray(COG_MD)
    tampered[10] ^= 0x01
    registry = FakeRegistry(auth="open", blob_body_override=bytes(tampered))
    async with client_for(registry) as client:
        with pytest.raises(OCIDigestMismatch):
            await client.get_blob(REPO, sha256(COG_MD), max_bytes=1024)


async def test_get_blob_oversized_via_descriptor_content_length_and_body():
    registry = FakeRegistry(auth="open")
    async with client_for(registry) as client:
        # Descriptor says it is too big: refused before any request.
        big = Descriptor(MEDIA_TYPE_PIXI_LOCK, sha256(PIXI_LOCK), len(PIXI_LOCK), {TITLE_ANNOTATION: "pixi.lock"})
        with pytest.raises(OCITooLarge):
            await client.get_blob(REPO, big, max_bytes=1024)
        assert registry.requests == []
        # Content-Length says it is too big: refused before reading the body.
        with pytest.raises(OCITooLarge, match="cap is 1024"):
            await client.get_blob(REPO, sha256(PIXI_LOCK), max_bytes=1024)

    # Chunked (no Content-Length): the streamed body trips the cap.
    registry = FakeRegistry(auth="open", blob_content_length=None)
    async with client_for(registry) as client:
        with pytest.raises(OCITooLarge, match="exceeds"):
            await client.get_blob(REPO, sha256(PIXI_LOCK), max_bytes=1024)
        # And a chunked body within the cap still verifies and returns.
        assert await client.get_blob(REPO, sha256(PIXI_TOML), max_bytes=1024) == PIXI_TOML

    registry = FakeRegistry(auth="open", blob_content_length="garbage")
    async with client_for(registry) as client:
        with pytest.raises(OCIProtocolError, match="malformed Content-Length"):
            await client.get_blob(REPO, sha256(PIXI_TOML), max_bytes=1024)


async def test_get_blob_redirect_to_other_host_drops_authorization():
    registry = FakeRegistry(auth="bearer", blob_redirect=True)
    async with client_for(registry) as client:
        body = await client.get_blob(REPO, sha256(COG_MD), max_bytes=1024)
    assert body == COG_MD
    store_requests = [r for r in registry.requests if r.url.host == "blobs.example"]
    assert len(store_requests) == 1
    assert "Authorization" not in store_requests[0].headers
    registry_blob_requests = [
        r for r in registry.requests if r.url.host == "registry.example" and "/blobs/" in r.url.path
    ]
    assert registry_blob_requests[-1].headers["Authorization"] == "Bearer tok-1"


async def test_get_blob_not_found():
    registry = FakeRegistry(auth="open")
    async with client_for(registry) as client:
        with pytest.raises(OCINotFound):
            await client.get_blob(REPO, sha256(b"nope"), max_bytes=1024)


# --- tags ---------------------------------------------------------------------


async def test_list_tags_follows_link_pagination():
    registry = FakeRegistry(auth="bearer", tags=[f"v{i}" for i in range(7)], tag_page_size=3)
    async with client_for(registry) as client:
        tags = await client.list_tags(REPO)
    assert tags == [f"v{i}" for i in range(7)]
    pages = [r for r in registry.requests if r.url.path.endswith("/tags/list")]
    # 1 anonymous probe + 3 authenticated pages.
    assert len(pages) == 4
    assert len(registry.token_requests) == 1


async def test_list_tags_handles_null_tags_and_rejects_malformed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"name": REPO, "tags": None})

    async with OCIClient(REGISTRY, transport=httpx.MockTransport(handler)) as client:
        assert await client.list_tags(REPO) == []

    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"name": REPO, "tags": "latest"})

    async with OCIClient(REGISTRY, transport=httpx.MockTransport(bad)) as client:
        with pytest.raises(OCIProtocolError, match="malformed 'tags'"):
            await client.list_tags(REPO)


async def test_list_tags_pagination_bounded_and_same_origin():
    def endless(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Link": f'</v2/{REPO}/tags/list?last=x>; rel="next"'}, json={"tags": ["x"]}
        )

    async with OCIClient(REGISTRY, transport=httpx.MockTransport(endless)) as client:
        assert await client.list_tags(REPO) == ["x"]

    def offsite(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Link": '<https://evil.example/v2/x/tags/list>; rel="next"'}, json={"tags": ["x"]}
        )

    async with OCIClient(REGISTRY, transport=httpx.MockTransport(offsite)) as client:
        with pytest.raises(OCIProtocolError, match="off the registry origin"):
            await client.list_tags(REPO)


# --- input validation -------------------------------------------------------


@pytest.mark.parametrize(
    "repo",
    ["Cogs/upper", "cogs//double", "/leading", "trailing/", "cogs/../etc", "a b", "", "cogs/x?y=1", "x" * 256],
)
async def test_invalid_repo_rejected_before_any_request(repo):
    registry = FakeRegistry(auth="open")
    async with client_for(registry) as client:
        with pytest.raises(OCIInvalidReference):
            await client.get_manifest(repo, "latest")
        with pytest.raises(OCIInvalidReference):
            await client.list_tags(repo)
        with pytest.raises(ValueError):
            await client.get_blob(repo, sha256(b""), max_bytes=10)
    assert registry.requests == []


@pytest.mark.parametrize("ref", ["", ".hidden", "tag/with/slash", "t" * 129, "sha256:short", "sha512:" + "a" * 128])
async def test_invalid_ref_rejected_before_any_request(ref):
    registry = FakeRegistry(auth="open")
    async with client_for(registry) as client:
        with pytest.raises(OCIInvalidReference):
            await client.get_manifest(REPO, ref)
    assert registry.requests == []


@pytest.mark.parametrize("digest", ["latest", "sha256:" + "G" * 64, "sha256:" + "a" * 63, "sha256:" + "a" * 64 + "/x"])
async def test_invalid_digest_rejected_before_any_request(digest):
    registry = FakeRegistry(auth="open")
    async with client_for(registry) as client:
        with pytest.raises(OCIInvalidReference):
            await client.get_blob(REPO, digest, max_bytes=10)
    assert registry.requests == []


async def test_valid_repo_grammar_accepted():
    for repo in ("a", "cogs/cog-x-1", "org_name/repo.name", "a/b/c", "double__under/ok", "dash--dash"):
        oci._validate_repo(repo)


def test_client_requires_http_origin():
    with pytest.raises(ValueError):
        OCIClient("registry.example")
    with pytest.raises(ValueError):
        OCIClient("ftp://registry.example")
    assert OCIClient("https://registry.example/").base_url == "https://registry.example"


# --- layer selection --------------------------------------------------------


def parsed_manifest() -> Manifest:
    return oci._parse_manifest(MANIFEST, MANIFEST_DIGEST, content_type_fallback="")


def test_select_bundle_layers_picks_cog_md_pixi_toml_and_named_manifest():
    manifest = parsed_manifest()
    selected = select_bundle_layers(manifest, manifest_file="cog.yaml")
    assert list(selected) == ["COG.md", "pixi.toml", "cog.yaml"]
    assert selected["COG.md"].digest == sha256(COG_MD)
    assert selected["cog.yaml"].media_type == MEDIA_TYPE_NEBI_ASSET


def test_select_bundle_layers_never_returns_lockfile_and_dedupes():
    manifest = parsed_manifest()
    selected = select_bundle_layers(
        manifest, manifest_file="pixi.toml", extra_titles=["pixi.lock", "context/system.md", "COG.md", "missing.txt"]
    )
    assert list(selected) == ["COG.md", "pixi.toml", "context/system.md"]
    # Even a lockfile published under another title is dropped by media type.
    relabelled = Manifest(
        media_type=MEDIA_TYPE_OCI_MANIFEST,
        digest=MANIFEST_DIGEST,
        config=None,
        layers=(Descriptor(MEDIA_TYPE_PIXI_LOCK, sha256(PIXI_LOCK), len(PIXI_LOCK), {TITLE_ANNOTATION: "COG.md"}),),
    )
    assert select_bundle_layers(relabelled) == {}


def test_select_bundle_layers_without_pixi_layers():
    manifest = oci._parse_manifest(build_manifest(include_pixi=False), "sha256:" + "0" * 64, content_type_fallback="")
    assert list(select_bundle_layers(manifest)) == ["COG.md"]


async def test_fetch_bundle_files_end_to_end():
    registry = FakeRegistry(auth="bearer-basic")
    async with client_for(registry, credentials=CREDS, token_url=OVERRIDE_TOKEN_URL) as client:
        manifest = await client.get_manifest(REPO, "latest")
        files = await fetch_bundle_files(client, REPO, manifest, manifest_file="cog.yaml")
    assert files == {"COG.md": COG_MD, "pixi.toml": PIXI_TOML, "cog.yaml": COG_YAML}
    assert len(registry.token_requests) == 1
    assert not any("/blobs/" + sha256(PIXI_LOCK) in r.url.path for r in registry.requests)


async def test_fetch_bundle_files_single_bad_file_raises():
    registry = FakeRegistry(auth="open")
    async with client_for(registry) as client:
        manifest = await client.get_manifest(REPO, "latest")
        with pytest.raises(OCITooLarge):
            await fetch_bundle_files(client, REPO, manifest, max_bytes_per_file=len(COG_MD) - 1)
        registry.blobs[sha256(PIXI_TOML)] = PIXI_TOML + b"# appended\n"
        with pytest.raises(OCIDigestMismatch):
            await fetch_bundle_files(client, REPO, manifest)


# --- live (opt-in) ------------------------------------------------------------

LIVE_URL = os.environ.get("COLLAB_HUB_TEST_OCI_URL", "")
LIVE_REPO = os.environ.get("COLLAB_HUB_TEST_OCI_REPO", "")

live_registry = pytest.mark.skipif(
    not (LIVE_URL and LIVE_REPO),
    reason="set COLLAB_HUB_TEST_OCI_URL and COLLAB_HUB_TEST_OCI_REPO to run against a real registry",
)


@live_registry
async def test_live_registry_reads_pixi_toml_layer():
    """Pull a real Nebi artifact and read its ``pixi.toml`` layer.

    Optional: ``COLLAB_HUB_TEST_OCI_REF`` (default ``latest``),
    ``COLLAB_HUB_TEST_OCI_USERNAME`` / ``_PASSWORD``, ``_CA_BUNDLE``, ``_TOKEN_URL``.
    """
    username = os.environ.get("COLLAB_HUB_TEST_OCI_USERNAME", "")
    credentials = None
    if username:
        credentials = BasicCredentials(username, os.environ.get("COLLAB_HUB_TEST_OCI_PASSWORD", ""))
    async with OCIClient(
        LIVE_URL,
        credentials=credentials,
        token_url=os.environ.get("COLLAB_HUB_TEST_OCI_TOKEN_URL") or None,
        ca_bundle_path=os.environ.get("COLLAB_HUB_TEST_OCI_CA_BUNDLE") or None,
        timeout_seconds=30.0,
    ) as client:
        tags = await client.list_tags(LIVE_REPO)
        ref = os.environ.get("COLLAB_HUB_TEST_OCI_REF", "latest")
        manifest = await client.get_manifest(LIVE_REPO, ref)
        files = await fetch_bundle_files(client, LIVE_REPO, manifest)

    assert tags, "repository has no tags"
    assert manifest.layer_by_title("pixi.toml") is not None
    assert files["pixi.toml"].strip()
    print(
        f"\nlive: {LIVE_URL} {LIVE_REPO}:{ref} -> {manifest.digest} tags={len(tags)} "
        f"files={ {title: len(body) for title, body in files.items()} }"
    )
