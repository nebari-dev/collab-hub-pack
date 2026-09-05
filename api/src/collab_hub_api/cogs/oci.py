"""Generic OCI Distribution client for indexing Cog bundles.

Everything the indexer needs from a registry that *every* OCI registry speaks:
manifests, digest-verified blobs, tag lists, and the bearer-token challenge
dance from the distribution token spec. Nothing here knows what Harbor, quay,
or GHCR are; vendor-specific concerns (repository enumeration, webhooks,
credential shape) live behind ``cogs.registry.RegistrySource`` adapters.

Public interface (stable for the adapters and the indexer):

- :class:`OCIClient` — one registry, optionally pre-authenticated.
- :class:`Descriptor` / :class:`Manifest` — the parsed content descriptors.
- :func:`select_bundle_layers` / :func:`fetch_bundle_files` — pick and fetch
  exactly the small layers the bundle reader needs.
- The :class:`OCIError` hierarchy — one base so callers can record a failure
  per artifact without knowing which step failed.

Two guarantees the rest of the Cog pipeline pins on:

- **No unverified bytes.** Every manifest and blob body is hashed and compared
  with the digest it was requested by (or the one the registry asserted)
  before it is returned. A mismatch is :class:`OCIDigestMismatch`, never data.
- **No unbounded reads.** Every body is streamed under a cap; the cap is
  checked against ``Content-Length`` first and then against the bytes actually
  received, so neither a missing nor a lying length header lets a body past it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

TITLE_ANNOTATION = "org.opencontainers.image.title"

MEDIA_TYPE_OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
MEDIA_TYPE_OCI_INDEX = "application/vnd.oci.image.index.v1+json"
MEDIA_TYPE_DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
MEDIA_TYPE_DOCKER_MANIFEST_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"

MEDIA_TYPE_PIXI_CONFIG = "application/vnd.pixi.config.v1+toml"
MEDIA_TYPE_PIXI_TOML = "application/vnd.pixi.toml.v1+toml"
MEDIA_TYPE_PIXI_LOCK = "application/vnd.pixi.lock.v1+yaml"
MEDIA_TYPE_NEBI_ASSET = "application/vnd.nebi.asset.v1"

DEFAULT_MAX_MANIFEST_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_BUNDLE_FILE_BYTES = 256 * 1024
DEFAULT_TIMEOUT_SECONDS = 10.0

COG_ENTRY_FILE = "COG.md"
DEFAULT_BUNDLE_TITLES: frozenset[str] = frozenset({COG_ENTRY_FILE, "pixi.toml", "cog.yaml"})
"""Layer titles the indexer always wants. The lockfile is never among them."""

LOCKFILE_TITLE = "pixi.lock"

# The Accept list a manifest GET advertises. Registries answer with whichever
# of these the reference resolves to; anything else is a protocol error.
MANIFEST_ACCEPT = ", ".join(
    (
        MEDIA_TYPE_OCI_MANIFEST,
        MEDIA_TYPE_OCI_INDEX,
        MEDIA_TYPE_DOCKER_MANIFEST,
        MEDIA_TYPE_DOCKER_MANIFEST_LIST,
    )
)
_INDEX_MEDIA_TYPES = frozenset({MEDIA_TYPE_OCI_INDEX, MEDIA_TYPE_DOCKER_MANIFEST_LIST})

# An index (multi-platform list) is followed to the first child that carries a
# pixi.toml layer. Cog indexes are expected to be tiny; the bound keeps a
# hostile or accidental thousand-entry index from turning one read into a sweep.
MAX_INDEX_CHILDREN = 8
# Tag listing follows ``Link: rel="next"``; bounded so a registry that keeps
# handing out next links cannot hold the indexer forever.
MAX_TAG_PAGES = 50
# Token and tag-list responses are small JSON documents; cap them well below
# the manifest cap so a misbehaving endpoint cannot make us buffer megabytes.
MAX_TOKEN_RESPONSE_BYTES = 64 * 1024
MAX_TAG_PAGE_BYTES = 1024 * 1024
# The token spec says a missing ``expires_in`` means 60 seconds. We drop a
# cached token a little before its deadline so a request never leaves with a
# token that expires in flight.
DEFAULT_TOKEN_TTL_SECONDS = 60
TOKEN_EXPIRY_MARGIN_SECONDS = 10
# Bound the token cache: one entry per (service, scope) the registry has
# challenged for, which is one per repository in practice.
MAX_CACHED_TOKENS = 256
# Blob GETs are commonly redirected to object storage; a short chain is normal,
# a long one is not.
MAX_BLOB_REDIRECTS = 5
_STREAM_CHUNK_BYTES = 64 * 1024

# Distribution spec grammar for repository names and references. Validated
# before any URL is built so a caller (or a registry's own listing) cannot
# smuggle path segments or query strings into a request.
_REPO_COMPONENT = r"[a-z0-9]+(?:(?:\.|_|__|-+)[a-z0-9]+)*"
_REPO_RE = re.compile(rf"^{_REPO_COMPONENT}(?:/{_REPO_COMPONENT})*$")
_MAX_REPO_LENGTH = 255
_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
# Only sha256 is accepted: it is the algorithm every registry and the Nebi
# publisher use, and it is the one this module can verify.
_DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")

# ``WWW-Authenticate`` scanner. A parameter is ``key=value`` (quoted or bare);
# a bare token that is *not* followed by ``=`` starts a new challenge scheme.
# Trying the parameter pattern first is what keeps ``Bearer realm=...`` from
# being read as a parameter named ``Bearer``.
_AUTH_PARAM_RE = re.compile(r'([A-Za-z0-9._~+/-]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]*))')
_AUTH_SCHEME_RE = re.compile(r"([A-Za-z0-9._~+/-]+)(?=\s|,|$)")
_LINK_NEXT_RE = re.compile(r'<([^>]*)>\s*;(?:[^,]*?;)?\s*rel\s*=\s*"?next"?', re.IGNORECASE)


def _monotonic() -> float:
    """Clock behind the token cache; a module-level indirection so tests can advance it."""
    return time.monotonic()


class OCIError(Exception):
    """Base for every failure this module reports."""


class OCIAuthError(OCIError):
    """The registry refused the credential (401 after a token was presented, or no usable challenge)."""


class OCINotFound(OCIError):
    """The repository, reference, or blob does not exist (404)."""


class OCIDigestMismatch(OCIError):
    """The bytes received do not hash to the descriptor's digest. Never trust the payload."""


class OCITooLarge(OCIError):
    """A manifest or blob exceeded the configured size cap; the body was not retained."""


class OCIProtocolError(OCIError):
    """Malformed manifest, challenge, token response, or an unexpected status."""


class OCIInvalidReference(OCIProtocolError, ValueError):
    """A repository name, tag, or digest does not match the distribution grammar.

    Raised before any request is made. It is an :class:`OCIError` so the
    indexer can record it per artifact like any other failure, and a
    :class:`ValueError` because it is the caller's input that is wrong.
    """


@dataclass(frozen=True)
class BasicCredentials:
    """Username/password presented to the token endpoint (or as HTTP basic auth)."""

    username: str
    password: str

    def header(self) -> str:
        return "Basic " + base64.b64encode(f"{self.username}:{self.password}".encode()).decode("ascii")

    def __repr__(self) -> str:
        # Keep the secret out of tracebacks, logs, and assertion messages.
        return f"BasicCredentials(username={self.username!r}, password='***')"


@dataclass(frozen=True)
class Descriptor:
    media_type: str
    digest: str
    size: int
    annotations: Mapping[str, str] = field(default_factory=dict)

    @property
    def title(self) -> str | None:
        return self.annotations.get(TITLE_ANNOTATION)


@dataclass(frozen=True)
class Manifest:
    media_type: str
    digest: str
    """sha256 of the body; checked against ``Docker-Content-Digest`` (when sent) and against a digest reference."""
    config: Descriptor | None
    layers: tuple[Descriptor, ...]
    annotations: Mapping[str, str] = field(default_factory=dict)
    raw: bytes = b""

    def layer_by_title(self, title: str) -> Descriptor | None:
        for layer in self.layers:
            if layer.title == title:
                return layer
        return None


@dataclass(frozen=True)
class _Challenge:
    scheme: str
    params: Mapping[str, str]


@dataclass
class _CachedToken:
    header: str
    expires_at: float


class OCIClient:
    """Minimal OCI Distribution client over ``httpx``.

    ``base_url`` is the registry origin (``https://harbor.example.com`` or an
    in-cluster ``http://harbor-core.harbor.svc``). ``token_url`` pins the
    bearer-token endpoint so an in-cluster caller is not bounced through the
    gateway when the challenge's ``realm`` advertises the external URL; the
    ``service``/``scope`` parameters from the challenge are still honoured.
    ``transport`` exists for tests (``httpx.MockTransport``).

    Authentication follows the registry, never anticipates it: the first
    request goes out anonymously, a 401 is answered per its
    ``WWW-Authenticate`` challenge (Bearer → token endpoint, Basic → the
    credential directly), and the request is retried exactly once. Tokens are
    cached per scope until shortly before ``expires_in`` so a sweep over one
    repository costs one token round trip, not one per blob.
    """

    def __init__(
        self,
        base_url: str,
        *,
        credentials: BasicCredentials | None = None,
        token_url: str | None = None,
        ca_bundle_path: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        origin = httpx.URL(base_url.rstrip("/"))
        if origin.scheme not in ("http", "https") or not origin.host:
            raise ValueError("base_url must be an http(s) origin")
        self._base_url = str(origin)
        self._origin = origin
        self._credentials = credentials
        self._token_url = token_url
        self._max_manifest_bytes = max_manifest_bytes
        self._tokens: dict[str, _CachedToken] = {}
        # scope hint (what we expect the registry to ask for) -> scope the
        # registry actually challenged with, so cached tokens are found on the
        # next request even when the two differ.
        self._scope_aliases: dict[str, str] = {}
        # Set once a registry answers with a Basic challenge; from then on the
        # credential goes out proactively instead of costing a 401 per request.
        self._use_basic = False

        client_kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(timeout_seconds),
            "follow_redirects": False,
            "max_redirects": MAX_BLOB_REDIRECTS,
        }
        if ca_bundle_path:
            client_kwargs["verify"] = ca_bundle_path
        if transport is not None:
            client_kwargs["transport"] = transport
        self._http = httpx.AsyncClient(**client_kwargs)

    @property
    def base_url(self) -> str:
        return self._base_url

    async def __aenter__(self) -> OCIClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- public reads -------------------------------------------------------

    async def get_manifest(self, repo: str, ref: str) -> Manifest:
        """Fetch a manifest by tag or digest; follow an index to the child carrying pixi layers.

        When ``ref`` resolves to an index, the returned :class:`Manifest` is the
        selected child and its ``digest`` is the child's, which is what an
        install must pin on.
        """
        _validate_repo(repo)
        _validate_ref(ref)
        manifest = await self._fetch_manifest(repo, ref)
        if not _is_index(manifest.media_type, manifest.raw):
            return manifest
        return await self._resolve_index(repo, manifest)

    async def get_blob(self, repo: str, descriptor: Descriptor | str, *, max_bytes: int) -> bytes:
        """Fetch a blob and verify its sha256 against the descriptor digest before returning it."""
        _validate_repo(repo)
        digest = descriptor.digest if isinstance(descriptor, Descriptor) else descriptor
        _validate_digest(digest)
        if isinstance(descriptor, Descriptor) and descriptor.size > max_bytes:
            # The manifest already told us the answer; do not spend a request on it.
            raise OCITooLarge(f"blob {digest} is {descriptor.size} bytes, cap is {max_bytes}")
        response = await self._send(
            f"/v2/{repo}/blobs/{digest}",
            headers={},
            scope_hint=_pull_scope(repo),
            follow_redirects=True,
        )
        body = await _read_bounded(response, max_bytes, what=f"blob {digest}")
        _verify_digest(body, digest, what=f"blob {digest}")
        return body

    async def list_tags(self, repo: str) -> list[str]:
        """List a repository's tags, following ``Link: rel="next"`` pagination."""
        _validate_repo(repo)
        tags: list[str] = []
        seen: set[str] = set()
        path: str | None = f"/v2/{repo}/tags/list"
        for _page in range(MAX_TAG_PAGES):
            if path is None:
                break
            response = await self._send(path, headers={}, scope_hint=_pull_scope(repo))
            body = await _read_bounded(response, MAX_TAG_PAGE_BYTES, what="tag list")
            payload = _parse_json_object(body, what="tag list")
            listed = payload.get("tags")
            if listed is None:
                listed = []
            if not isinstance(listed, list):
                raise OCIProtocolError("tag list has a malformed 'tags' field")
            for tag in listed:
                if isinstance(tag, str) and tag not in seen:
                    seen.add(tag)
                    tags.append(tag)
            path = self._next_page_path(response.headers.get_list("link"))
        return tags

    # -- manifests ----------------------------------------------------------

    async def _fetch_manifest(self, repo: str, ref: str) -> Manifest:
        response = await self._send(
            f"/v2/{repo}/manifests/{ref}",
            headers={"Accept": MANIFEST_ACCEPT},
            scope_hint=_pull_scope(repo),
        )
        asserted = response.headers.get("docker-content-digest")
        content_type = response.headers.get("content-type", "")
        body = await _read_bounded(response, self._max_manifest_bytes, what=f"manifest {repo}:{ref}")

        # Verification order: what the caller asked for, then what the registry
        # asserted, then our own hash. A digest ref is the strongest claim; a
        # tag can only be checked against the registry's assertion.
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if _DIGEST_RE.match(ref) and ref != digest:
            raise OCIDigestMismatch(f"manifest body does not hash to the requested digest {ref}")
        if asserted is not None:
            if not _DIGEST_RE.match(asserted):
                raise OCIProtocolError("registry sent a malformed Docker-Content-Digest header")
            if asserted != digest:
                raise OCIDigestMismatch(
                    f"manifest body does not hash to the registry's Docker-Content-Digest {asserted}"
                )
        return _parse_manifest(body, digest, content_type_fallback=content_type)

    async def _resolve_index(self, repo: str, index: Manifest) -> Manifest:
        payload = _parse_json_object(index.raw, what="index")
        children = payload.get("manifests")
        if not isinstance(children, list):
            raise OCIProtocolError("index has no 'manifests' list")
        for entry in children[:MAX_INDEX_CHILDREN]:
            child_ref = entry.get("digest") if isinstance(entry, dict) else None
            if not isinstance(child_ref, str) or not _DIGEST_RE.match(child_ref):
                raise OCIProtocolError("index entry has a malformed digest")
            child = await self._fetch_manifest(repo, child_ref)
            if _is_index(child.media_type, child.raw):
                # Nested indexes are legal but not something a Cog publisher
                # produces; skip rather than recurse.
                continue
            if any(layer.media_type == MEDIA_TYPE_PIXI_TOML for layer in child.layers):
                return child
        raise OCIProtocolError("index has no child manifest carrying a pixi.toml layer")

    def _next_page_path(self, link_headers: list[str]) -> str | None:
        for header in link_headers:
            match = _LINK_NEXT_RE.search(header)
            if not match:
                continue
            target = self._origin.join(match.group(1))
            # A next link that points off-origin would carry our Authorization
            # header to a third party; treat it as the protocol error it is.
            if (target.scheme, target.host, target.port) != (self._origin.scheme, self._origin.host, self._origin.port):
                raise OCIProtocolError("tag list pagination link points off the registry origin")
            return target.raw_path.decode("ascii")
        return None

    # -- transport + auth ---------------------------------------------------

    async def _send(
        self,
        path: str,
        *,
        headers: Mapping[str, str],
        scope_hint: str,
        follow_redirects: bool = False,
    ) -> httpx.Response:
        """GET ``path`` on the registry, answering one 401 challenge.

        Returns a *streaming* 2xx response the caller must drain with
        :func:`_read_bounded`. 404 → :class:`OCINotFound`; a second 401 →
        :class:`OCIAuthError`; anything else non-2xx → :class:`OCIProtocolError`.
        """
        url = self._base_url + path
        request_headers = dict(headers)
        cached = self._proactive_auth(scope_hint)
        if cached:
            request_headers["Authorization"] = cached
        response = await self._http.send(
            self._http.build_request("GET", url, headers=request_headers),
            stream=True,
            follow_redirects=follow_redirects,
        )
        if response.status_code != 401:
            return await _check_status(response, path)

        challenges = _parse_challenges(response.headers.get_list("www-authenticate"))
        await response.aclose()
        authorization = await self._answer_challenge(challenges, scope_hint)
        request_headers["Authorization"] = authorization
        response = await self._http.send(
            self._http.build_request("GET", url, headers=request_headers),
            stream=True,
            follow_redirects=follow_redirects,
        )
        if response.status_code == 401:
            await response.aclose()
            raise OCIAuthError(f"registry rejected the credential for {path}")
        return await _check_status(response, path)

    def _proactive_auth(self, scope_hint: str) -> str | None:
        if self._use_basic and self._credentials is not None:
            return self._credentials.header()
        key = self._scope_aliases.get(scope_hint, scope_hint)
        cached = self._tokens.get(key)
        if cached is None:
            return None
        if cached.expires_at <= _monotonic():
            del self._tokens[key]
            return None
        return cached.header

    async def _answer_challenge(self, challenges: list[_Challenge], scope_hint: str) -> str:
        bearer = next((c for c in challenges if c.scheme == "bearer"), None)
        if bearer is not None:
            return await self._bearer_token(bearer.params, scope_hint)
        if any(c.scheme == "basic" for c in challenges):
            if self._credentials is None:
                raise OCIAuthError("registry requires basic credentials and none are configured")
            self._use_basic = True
            return self._credentials.header()
        raise OCIAuthError("registry returned 401 without a Bearer or Basic challenge")

    async def _bearer_token(self, params: Mapping[str, str], scope_hint: str) -> str:
        token_url = self._token_url or params.get("realm", "")
        realm = httpx.URL(token_url) if token_url else None
        if realm is None or realm.scheme not in ("http", "https") or not realm.host:
            raise OCIProtocolError("bearer challenge has no usable realm and no token_url is configured")
        service = params.get("service", "")
        scope = params.get("scope", "")
        query: dict[str, str] = {}
        if service:
            query["service"] = service
        if scope:
            query["scope"] = scope

        headers: dict[str, str] = {"Accept": "application/json"}
        if self._credentials is not None:
            headers["Authorization"] = self._credentials.header()
        response = await self._http.send(
            self._http.build_request("GET", realm, params=query, headers=headers),
            stream=True,
        )
        if response.status_code in (401, 403):
            await response.aclose()
            raise OCIAuthError("token endpoint rejected the credential")
        if not 200 <= response.status_code < 300:
            await response.aclose()
            raise OCIProtocolError(f"token endpoint returned HTTP {response.status_code}")
        body = await _read_bounded(response, MAX_TOKEN_RESPONSE_BYTES, what="token response")
        payload = _parse_json_object(body, what="token response")
        # The token spec's field is ``token``; OAuth2-shaped endpoints return
        # ``access_token``. Accept both, prefer the spec's.
        token = payload.get("token") or payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise OCIProtocolError("token response carries no token")
        header = f"Bearer {token}"

        expires_in = payload.get("expires_in", DEFAULT_TOKEN_TTL_SECONDS)
        if not isinstance(expires_in, (int, float)) or isinstance(expires_in, bool):
            expires_in = DEFAULT_TOKEN_TTL_SECONDS
        ttl = expires_in - TOKEN_EXPIRY_MARGIN_SECONDS
        if ttl > 0:
            key = scope or scope_hint
            self._scope_aliases[scope_hint] = key
            self._store_token(key, header, _monotonic() + ttl)
        return header

    def _store_token(self, key: str, header: str, expires_at: float) -> None:
        now = _monotonic()
        for stale in [k for k, v in self._tokens.items() if v.expires_at <= now]:
            del self._tokens[stale]
        while len(self._tokens) >= MAX_CACHED_TOKENS:
            del self._tokens[next(iter(self._tokens))]
        self._tokens[key] = _CachedToken(header=header, expires_at=expires_at)


def select_bundle_layers(
    manifest: Manifest,
    *,
    manifest_file: str | None = None,
    extra_titles: Iterable[str] = (),
) -> dict[str, Descriptor]:
    """Return the descriptors, keyed by title, the bundle reader needs.

    Always ``COG.md`` and ``pixi.toml`` when present, the profile manifest named
    by the frontmatter (``manifest_file``), and any ``extra_titles``. Never the
    lockfile: ``pixi.lock`` (by title or media type) is dropped even when asked
    for, because it is the one large layer and the card never needs it. Layers
    without a title annotation cannot be selected. Titles that are requested
    but absent from the manifest are simply missing from the result; the
    reader decides whether that is an error.
    """
    wanted: list[str] = [COG_ENTRY_FILE, "pixi.toml"]
    if manifest_file:
        wanted.append(manifest_file)
    wanted.extend(extra_titles)

    selected: dict[str, Descriptor] = {}
    for title in wanted:
        if title in selected or title == LOCKFILE_TITLE:
            continue
        layer = manifest.layer_by_title(title)
        if layer is None or layer.media_type == MEDIA_TYPE_PIXI_LOCK:
            continue
        selected[title] = layer
    return selected


async def fetch_bundle_files(
    client: OCIClient,
    repo: str,
    manifest: Manifest,
    *,
    manifest_file: str | None = None,
    extra_titles: Iterable[str] = (),
    max_bytes_per_file: int = DEFAULT_MAX_BUNDLE_FILE_BYTES,
) -> dict[str, bytes]:
    """Fetch the selected layers and return ``{title: bytes}`` for the bundle reader.

    Any single oversized or digest-mismatched layer raises; the indexer records
    that failure against the artifact rather than indexing a partial bundle.
    """
    layers = select_bundle_layers(manifest, manifest_file=manifest_file, extra_titles=extra_titles)
    files: dict[str, bytes] = {}
    for title, descriptor in layers.items():
        files[title] = await client.get_blob(repo, descriptor, max_bytes=max_bytes_per_file)
    return files


# -- validation ---------------------------------------------------------------


def _pull_scope(repo: str) -> str:
    return f"repository:{repo}:pull"


def _validate_repo(repo: str) -> None:
    if len(repo) > _MAX_REPO_LENGTH or not _REPO_RE.match(repo):
        raise OCIInvalidReference(f"invalid repository name {repo!r}")


def _validate_ref(ref: str) -> None:
    if not (_TAG_RE.match(ref) or _DIGEST_RE.match(ref)):
        raise OCIInvalidReference(f"invalid reference {ref!r}: expected a tag or a sha256 digest")


def _validate_digest(digest: str) -> None:
    if not _DIGEST_RE.match(digest):
        raise OCIInvalidReference(f"invalid digest {digest!r}: expected sha256:<64 hex>")


# -- response handling --------------------------------------------------------


async def _check_status(response: httpx.Response, path: str) -> httpx.Response:
    if 200 <= response.status_code < 300:
        return response
    status = response.status_code
    await response.aclose()
    if status == 404:
        raise OCINotFound(f"registry has no {path}")
    # The body is deliberately not echoed: it may be a gateway's HTML page.
    raise OCIProtocolError(f"registry returned HTTP {status} for {path}")


async def _read_bounded(response: httpx.Response, cap: int, *, what: str) -> bytes:
    """Drain a streaming response into memory, never holding more than ``cap`` bytes.

    ``Content-Length`` is only a hint (it may be absent for chunked bodies, or
    wrong); the accumulated size is what the cap is enforced on.
    """
    try:
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError:
                raise OCIProtocolError(f"{what}: malformed Content-Length header") from None
            if length > cap:
                raise OCITooLarge(f"{what} is {length} bytes, cap is {cap}")
        buffer = bytearray()
        async for chunk in response.aiter_bytes(_STREAM_CHUNK_BYTES):
            if len(buffer) + len(chunk) > cap:
                raise OCITooLarge(f"{what} exceeds the {cap}-byte cap")
            buffer.extend(chunk)
        return bytes(buffer)
    finally:
        await response.aclose()


def _verify_digest(body: bytes, digest: str, *, what: str) -> None:
    actual = "sha256:" + hashlib.sha256(body).hexdigest()
    if actual != digest:
        raise OCIDigestMismatch(f"{what}: body hashes to {actual}")


def _parse_json_object(body: bytes, *, what: str) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise OCIProtocolError(f"{what} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise OCIProtocolError(f"{what} is not a JSON object")
    return payload


def _media_type_of(payload: Mapping[str, Any], content_type: str) -> str:
    declared = payload.get("mediaType")
    if isinstance(declared, str) and declared:
        return declared
    # Older OCI manifests omit mediaType; the response header carries it.
    return content_type.split(";", 1)[0].strip()


def _is_index(media_type: str, raw: bytes) -> bool:
    if media_type in _INDEX_MEDIA_TYPES:
        return True
    if media_type in (MEDIA_TYPE_OCI_MANIFEST, MEDIA_TYPE_DOCKER_MANIFEST):
        return False
    # No recognisable media type: fall back to the shape of the document.
    try:
        payload = json.loads(raw)
    except ValueError:
        return False
    return isinstance(payload, dict) and "manifests" in payload and "layers" not in payload


def _parse_manifest(body: bytes, digest: str, *, content_type_fallback: str) -> Manifest:
    payload = _parse_json_object(body, what="manifest")
    media_type = _media_type_of(payload, content_type_fallback)
    if _is_index(media_type, body):
        # An index has no config/layers of its own; the caller resolves it.
        return Manifest(
            media_type=media_type,
            digest=digest,
            config=None,
            layers=(),
            annotations=_string_map(payload.get("annotations")),
            raw=body,
        )
    config_raw = payload.get("config")
    config = _parse_descriptor(config_raw, what="config") if config_raw is not None else None
    layers_raw = payload.get("layers", [])
    if not isinstance(layers_raw, list):
        raise OCIProtocolError("manifest 'layers' is not a list")
    layers = tuple(_parse_descriptor(entry, what="layer") for entry in layers_raw)
    return Manifest(
        media_type=media_type,
        digest=digest,
        config=config,
        layers=layers,
        annotations=_string_map(payload.get("annotations")),
        raw=body,
    )


def _parse_descriptor(entry: Any, *, what: str) -> Descriptor:
    if not isinstance(entry, dict):
        raise OCIProtocolError(f"manifest {what} descriptor is not an object")
    media_type = entry.get("mediaType")
    digest = entry.get("digest")
    size = entry.get("size")
    if not isinstance(media_type, str) or not media_type:
        raise OCIProtocolError(f"manifest {what} descriptor has no mediaType")
    if not isinstance(digest, str) or not _DIGEST_RE.match(digest):
        raise OCIProtocolError(f"manifest {what} descriptor has a malformed digest")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise OCIProtocolError(f"manifest {what} descriptor has a malformed size")
    return Descriptor(
        media_type=media_type,
        digest=digest,
        size=size,
        annotations=_string_map(entry.get("annotations")),
    )


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str)}


# -- WWW-Authenticate ---------------------------------------------------------


def _parse_challenges(headers: list[str]) -> list[_Challenge]:
    """Parse ``WWW-Authenticate`` values into ``(scheme, params)`` challenges.

    Handles quoted and bare parameter values, several challenges in one header
    (``Bearer realm="...", Basic realm="..."``) and several header lines.
    Scheme names and parameter names are lower-cased; values are kept as sent.
    """
    challenges: list[_Challenge] = []
    for header in headers:
        scheme: str | None = None
        params: dict[str, str] = {}
        pos = 0
        length = len(header)
        while pos < length:
            if header[pos] in " \t,":
                pos += 1
                continue
            param = _AUTH_PARAM_RE.match(header, pos)
            if param:
                if scheme is not None:
                    quoted, bare = param.group(2), param.group(3)
                    value = re.sub(r"\\(.)", r"\1", quoted) if quoted is not None else (bare or "")
                    params.setdefault(param.group(1).lower(), value)
                # A parameter before any scheme belongs to nothing; drop it whole.
                pos = param.end()
                continue
            token = _AUTH_SCHEME_RE.match(header, pos)
            if token:
                if scheme is not None:
                    challenges.append(_Challenge(scheme, params))
                scheme = token.group(1).lower()
                params = {}
                pos = token.end()
                continue
            # Unparseable byte (e.g. a token68 credential blob): skip to the next
            # separator rather than fail the whole header.
            pos += 1
        if scheme is not None:
            challenges.append(_Challenge(scheme, params))
    return challenges
