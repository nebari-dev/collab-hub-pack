"""Generic OCI Distribution client for indexing Cog bundles.

Everything the indexer needs from a registry that *every* OCI registry speaks:
manifests, digest-verified blobs, tag lists, and the bearer-token challenge
dance from the distribution token spec.

Vendor neutrality means *registry* vendors: nothing here knows what Harbor,
quay, or GHCR are, and no request leaves ``/v2/`` or the token realm.
Repository enumeration, webhooks, and credential shape are vendor concerns
and live behind ``cogs.registry.RegistrySource`` adapters. The *artifact*
format the indexer exists to read (pixi and Nebi media types, the layer
titles a Cog bundle uses) is not a registry-vendor concern and is described
here so the layer-selection helper can name what it selects.

Public interface (stable for the adapters and the indexer):

- :class:`OCIClient` — one registry, optionally pre-authenticated.
- :class:`Descriptor` / :class:`Manifest` — the parsed content descriptors.
- :func:`select_bundle_layers` / :func:`fetch_bundle_files` — pick and fetch
  exactly the small layers the bundle reader needs.
- The :class:`OCIError` hierarchy — one base so callers can record a failure
  per artifact without knowing which step failed.

Guarantees the rest of the Cog pipeline pins on:

- **Blobs are verified.** Every blob body is hashed and compared with the
  digest it was requested by before it is returned. A mismatch is
  :class:`OCIDigestMismatch`, never data.
- **Manifests are verified against every digest available.** A manifest
  fetched by digest must hash to that digest; one fetched by tag must hash to
  the registry's ``Docker-Content-Digest`` when the header is present. A tag
  is a mutable pointer with no independent digest to check against, so a tag
  fetch without that header yields ``Manifest.digest`` = sha256 of the body:
  that hash is the content identity the indexer stores and installs pin, and
  every blob the manifest names is verified against the descriptors in it.
- **Reads are bounded.** Every body is streamed under a cap: ``Content-Length``
  is checked first, then the running total of bytes received, and the read is
  rejected once the total exceeds the cap. At most one stream chunk (64 KiB)
  beyond the cap may be read before rejection. Requests ask for
  ``Accept-Encoding: identity`` and encoded responses are refused, so
  decompression cannot inflate a chunk. Redirect hops are followed by this
  module, one streamed hop at a time, never buffered.
- **Failures are one hierarchy.** Transport and URL errors from ``httpx`` are
  wrapped in :class:`OCITransportError` / :class:`OCIProtocolError` so an
  ``except OCIError`` records every failure mode per artifact.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import ssl
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

# The artifact format Cog bundles are published in (pixi workspace + one asset
# layer per bundled file). Artifact format, not registry vendor: see the
# module docstring.
MEDIA_TYPE_PIXI_CONFIG = "application/vnd.pixi.config.v1+toml"
MEDIA_TYPE_PIXI_TOML = "application/vnd.pixi.toml.v1+toml"
MEDIA_TYPE_PIXI_LOCK = "application/vnd.pixi.lock.v1+yaml"
MEDIA_TYPE_NEBI_ASSET = "application/vnd.nebi.asset.v1"

DEFAULT_MAX_MANIFEST_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_BUNDLE_FILE_BYTES = 256 * 1024
DEFAULT_TIMEOUT_SECONDS = 10.0

COG_ENTRY_FILE = "COG.md"
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
# handing out next links cannot hold the indexer forever. Running out of pages
# with a next link still pending is an error, not a silently shorter list.
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
# Bound the token cache and its companions (scope aliases, per-key locks): one
# entry per (token endpoint, service, scope) the registry has challenged for,
# which is one per repository in practice.
MAX_CACHED_TOKENS = 256
# Blob GETs are commonly redirected to object storage; a short chain is normal,
# a long one is not.
MAX_BLOB_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_STREAM_CHUNK_BYTES = 64 * 1024

# Distribution spec grammar for repository names and references. Validated
# (with fullmatch, so a trailing newline is not accepted) before any URL is
# built so a caller, or a registry's own listing, cannot smuggle path segments
# or query strings into a request.
_REPO_COMPONENT = r"[a-z0-9]+(?:(?:\.|_|__|-+)[a-z0-9]+)*"
_REPO_RE = re.compile(rf"{_REPO_COMPONENT}(?:/{_REPO_COMPONENT})*")
_MAX_REPO_LENGTH = 255
_TAG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")
# Only sha256 is accepted: it is the algorithm every registry and the Cog
# publisher use, and it is the one this module can verify.
_DIGEST_RE = re.compile(r"sha256:[a-f0-9]{64}")

# ``WWW-Authenticate`` scanner. A parameter is ``key=value`` (quoted or bare);
# a bare token that is *not* followed by ``=`` starts a new challenge scheme.
# Trying the parameter pattern first is what keeps ``Bearer realm=...`` from
# being read as a parameter named ``Bearer``.
_AUTH_PARAM_RE = re.compile(r'([A-Za-z0-9._~+/-]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]*))')
_AUTH_SCHEME_RE = re.compile(r"([A-Za-z0-9._~+/-]+)(?=\s|,|$)")
_LINK_NEXT_RE = re.compile(r'<([^>]*)>\s*;(?:[^,]*?;)?\s*rel\s*=\s*"?next"?', re.IGNORECASE)

_TokenKey = tuple[str, str, str]
"""(token endpoint, service, scope): what makes one bearer token interchangeable with another."""


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


class OCITransportError(OCIError):
    """The registry could not be reached or the connection failed mid-transfer (timeouts, resets).

    Distinct from :class:`OCIProtocolError` because it is the one failure an
    indexer may reasonably retry on the next sweep without recording the
    artifact as broken. The message names the httpx error class only; URLs
    and headers are never echoed.
    """


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


@dataclass(repr=False)
class _CachedToken:
    header: str
    expires_at: float

    def __repr__(self) -> str:
        # The bearer token is a credential; keep it out of reprs and debug output.
        return f"_CachedToken(header='Bearer ***', expires_at={self.expires_at!r})"


class OCIClient:
    """Minimal OCI Distribution client over ``httpx``.

    ``base_url`` is the registry origin (``https://harbor.example.com`` or an
    in-cluster ``http://harbor-core.harbor.svc``); a path prefix is rejected
    at construction because every request path is built from ``/v2/`` and a
    prefix would be silently discarded. ``token_url`` pins the
    bearer-token endpoint so an in-cluster caller is not bounced through the
    gateway when the challenge's ``realm`` advertises the external URL; the
    ``service``/``scope`` parameters from the challenge are still honoured.
    ``transport`` exists for tests (``httpx.MockTransport``).

    Authentication follows the registry, never anticipates it: the first
    request goes out anonymously, a 401 is answered per its
    ``WWW-Authenticate`` challenge (Bearer → token endpoint, Basic → the
    credential directly), and the request is retried exactly once. Tokens are
    cached per (token endpoint, service, scope) until shortly before
    ``expires_in`` so a sweep over one repository costs one token round trip,
    not one per blob; concurrent misses on the same key mint one token. With
    ``credentials`` set and no ``token_url``, the Basic credential is presented
    to whatever ``realm`` the registry's challenge advertises — the token
    spec's design; pin ``token_url`` when the endpoint must not be the
    registry's choice.

    One client may be shared by concurrent coroutines. Construction fails
    (``ValueError``/``OSError``) for a malformed ``base_url`` or an unreadable
    ``ca_bundle_path``: those are configuration errors and belong at startup.
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
        try:
            origin = httpx.URL(base_url.rstrip("/"))
        except httpx.InvalidURL as exc:
            raise ValueError("base_url is not a valid URL") from exc
        if origin.scheme not in ("http", "https") or not origin.host:
            raise ValueError("base_url must be an http(s) origin")
        if origin.path not in ("", "/") or origin.query or origin.fragment:
            # ``self._origin.join("/v2/...")`` would silently discard a path
            # prefix; refuse it here, at startup, like the other config errors.
            raise ValueError("base_url must be a bare origin, without a path, query, or fragment")
        self._base_url = str(origin)
        self._origin = origin
        self._credentials = credentials
        self._token_url = token_url
        self._max_manifest_bytes = max_manifest_bytes
        self._tokens: dict[_TokenKey, _CachedToken] = {}
        # scope hint (what we expect the registry to ask for) -> the key the
        # registry's challenge actually produced, so cached tokens are found on
        # the next request even when the two differ.
        self._scope_aliases: dict[str, _TokenKey] = {}
        # Serialises token minting per key so concurrent misses share one token.
        self._token_locks: dict[_TokenKey, asyncio.Lock] = {}
        # Set once a registry answers with a Basic challenge; from then on the
        # credential goes out proactively instead of costing a 401 per request.
        self._use_basic = False

        client_kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(timeout_seconds),
            "follow_redirects": False,
            # Identity only: a compressed body could inflate past the cap
            # between the wire and the chunk we measure.
            "headers": {"Accept-Encoding": "identity"},
        }
        if ca_bundle_path:
            # A private CA for an in-cluster or lab registry; a missing file
            # fails here, at startup, rather than on the first request.
            client_kwargs["verify"] = ssl.create_default_context(cafile=ca_bundle_path)
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
        install must pin on. See the module docstring for what ``digest`` is
        verified against for a tag versus a digest reference.
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
            allow_redirects=True,
        )
        body = await _read_bounded(response, max_bytes, what=f"blob {digest}")
        _verify_digest(body, digest, what=f"blob {digest}")
        return body

    async def list_tags(self, repo: str) -> list[str]:
        """List a repository's tags, following ``Link: rel="next"`` pagination.

        Every tag is checked against the tag grammar; a registry that lists
        something else is reporting a malformed document, not a tag.
        """
        _validate_repo(repo)
        tags: list[str] = []
        seen: set[str] = set()
        path = f"/v2/{repo}/tags/list"
        for _page in range(MAX_TAG_PAGES):
            response = await self._send(path, headers={}, scope_hint=_pull_scope(repo))
            body = await _read_bounded(response, MAX_TAG_PAGE_BYTES, what="tag list")
            payload = _parse_json_object(body, what="tag list")
            listed = payload.get("tags")
            if listed is None:
                listed = []
            if not isinstance(listed, list):
                raise OCIProtocolError("tag list has a malformed 'tags' field")
            for tag in listed:
                if not isinstance(tag, str) or not _TAG_RE.fullmatch(tag):
                    raise OCIProtocolError("tag list contains a malformed tag")
                if tag not in seen:
                    seen.add(tag)
                    tags.append(tag)
            next_path = self._next_page_path(response.headers.get_list("link"))
            if next_path is None:
                return tags
            path = next_path
        raise OCIProtocolError(f"tag list did not end within {MAX_TAG_PAGES} pages")

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
        if _DIGEST_RE.fullmatch(ref) and ref != digest:
            raise OCIDigestMismatch(f"manifest body does not hash to the requested digest {ref}")
        if asserted is not None:
            if not _DIGEST_RE.fullmatch(asserted):
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
            descriptor = _parse_descriptor(entry, what="index entry")
            if descriptor.size > self._max_manifest_bytes:
                raise OCITooLarge(f"index child {descriptor.digest} is {descriptor.size} bytes")
            child = await self._fetch_manifest(repo, descriptor.digest)
            if len(child.raw) != descriptor.size:
                raise OCIProtocolError(f"index child {descriptor.digest} does not match its declared size")
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
            target = _join_url(self._origin, match.group(1), what="tag list pagination link")
            # A next link that points off-origin would carry our Authorization
            # header to a third party; treat it as the protocol error it is.
            if not _same_origin(target, self._origin):
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
        allow_redirects: bool = False,
    ) -> httpx.Response:
        """GET ``path`` on the registry, answering one 401 challenge.

        Returns a *streaming* 2xx response the caller must drain with
        :func:`_read_bounded`. 404 → :class:`OCINotFound`; a second 401 →
        :class:`OCIAuthError`; anything else non-2xx → :class:`OCIProtocolError`.
        With ``allow_redirects`` the 3xx chain is followed here, one streamed
        hop at a time, dropping ``Authorization`` when a hop leaves the origin.
        """
        url = self._origin.join(path)
        request_headers = dict(headers)
        presented = self._proactive_auth(scope_hint)
        if presented:
            request_headers["Authorization"] = presented
        response = await self._dispatch(url, request_headers, what=path)
        if response.status_code == 401:
            challenges = _parse_challenges(response.headers.get_list("www-authenticate"))
            await response.aclose()
            request_headers["Authorization"] = await self._answer_challenge(challenges, scope_hint, presented)
            response = await self._dispatch(url, request_headers, what=path)
            if response.status_code == 401:
                await response.aclose()
                raise OCIAuthError(f"registry rejected the credential for {path}")
        if allow_redirects:
            response = await self._follow_redirects(response, url, request_headers, what=path)
        return await _check_status(response, path)

    async def _dispatch(self, url: httpx.URL, headers: Mapping[str, str], *, what: str) -> httpx.Response:
        """One streamed GET; the only place httpx transport errors are raised."""
        request = self._http.build_request("GET", url, headers=dict(headers))
        try:
            return await self._http.send(request, stream=True, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise OCITransportError(f"{what}: {type(exc).__name__}") from exc

    async def _follow_redirects(
        self,
        response: httpx.Response,
        url: httpx.URL,
        headers: Mapping[str, str],
        *,
        what: str,
    ) -> httpx.Response:
        for _hop in range(MAX_BLOB_REDIRECTS):
            if response.status_code not in _REDIRECT_STATUSES:
                return response
            location = response.headers.get("location")
            # Closing a streaming response discards its body without reading it.
            await response.aclose()
            if not location:
                raise OCIProtocolError(f"{what}: redirect without a Location header")
            target = _join_url(url, location, what=f"{what} redirect")
            hop_headers = dict(headers)
            if not _same_origin(target, self._origin):
                # Object storage must never see the registry credential. Every
                # hop is compared against the *registry* origin, not the
                # previous hop's, so a second hop between storage URLs cannot
                # win the header back. This is stricter than httpx's own rule,
                # which keeps the header on an http→https upgrade of the same
                # host.
                hop_headers.pop("Authorization", None)
            response = await self._dispatch(target, hop_headers, what=what)
            url = target
        if response.status_code in _REDIRECT_STATUSES:
            await response.aclose()
            raise OCIProtocolError(f"{what}: more than {MAX_BLOB_REDIRECTS} redirects")
        return response

    def _proactive_auth(self, scope_hint: str) -> str | None:
        if self._use_basic and self._credentials is not None:
            return self._credentials.header()
        key = self._scope_aliases.get(scope_hint)
        if key is None:
            return None
        cached = self._tokens.get(key)
        if cached is None:
            return None
        if cached.expires_at <= _monotonic():
            del self._tokens[key]
            return None
        return cached.header

    async def _answer_challenge(self, challenges: list[_Challenge], scope_hint: str, presented: str | None) -> str:
        bearer = next((c for c in challenges if c.scheme == "bearer"), None)
        if bearer is not None:
            return await self._bearer_token(bearer.params, scope_hint, presented)
        if any(c.scheme == "basic" for c in challenges):
            if self._credentials is None:
                raise OCIAuthError("registry requires basic credentials and none are configured")
            self._use_basic = True
            return self._credentials.header()
        raise OCIAuthError("registry returned 401 without a Bearer or Basic challenge")

    async def _bearer_token(self, params: Mapping[str, str], scope_hint: str, presented: str | None) -> str:
        endpoint = self._token_url or params.get("realm", "")
        realm = _parse_url(endpoint, what="token endpoint") if endpoint else None
        if realm is None or realm.scheme not in ("http", "https") or not realm.host:
            raise OCIProtocolError("bearer challenge has no usable realm and no token_url is configured")
        service = params.get("service", "")
        scope = params.get("scope", "")
        key: _TokenKey = (str(realm), service, scope)

        lock = self._token_locks.get(key)
        if lock is None:
            lock = self._token_locks[key] = asyncio.Lock()
            # Past MAX_CACHED_TOKENS distinct keys in flight this can evict a
            # lock a coroutine still holds, and a later miss on that key mints
            # a duplicate token. Harmless (both tokens work) and unreachable
            # in practice; the bound is what matters.
            _bound(self._token_locks)
        async with lock:
            cached = self._tokens.get(key)
            if cached is not None and cached.header == presented:
                # The registry just refused this very token; it is no longer good.
                del self._tokens[key]
                cached = None
            if cached is not None and cached.expires_at > _monotonic():
                # Another coroutine minted it while we waited for the lock.
                self._alias(scope_hint, key)
                return cached.header
            header, ttl = await self._mint_token(realm, service, scope)
            if ttl > 0:
                self._alias(scope_hint, key)
                self._store_token(key, header, _monotonic() + ttl)
            return header

    async def _mint_token(self, realm: httpx.URL, service: str, scope: str) -> tuple[str, float]:
        query: dict[str, str] = {}
        if service:
            query["service"] = service
        if scope:
            query["scope"] = scope
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._credentials is not None:
            headers["Authorization"] = self._credentials.header()
        request = self._http.build_request("GET", realm, params=query, headers=headers)
        try:
            response = await self._http.send(request, stream=True, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise OCITransportError(f"token endpoint: {type(exc).__name__}") from exc
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
        expires_in = payload.get("expires_in", DEFAULT_TOKEN_TTL_SECONDS)
        if not isinstance(expires_in, (int, float)) or isinstance(expires_in, bool):
            expires_in = DEFAULT_TOKEN_TTL_SECONDS
        return f"Bearer {token}", expires_in - TOKEN_EXPIRY_MARGIN_SECONDS

    def _alias(self, scope_hint: str, key: _TokenKey) -> None:
        self._scope_aliases[scope_hint] = key
        _bound(self._scope_aliases)

    def _store_token(self, key: _TokenKey, header: str, expires_at: float) -> None:
        now = _monotonic()
        for stale in [k for k, v in self._tokens.items() if v.expires_at <= now]:
            del self._tokens[stale]
        self._tokens[key] = _CachedToken(header=header, expires_at=expires_at)
        _bound(self._tokens)


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

    ``manifest_file`` is the profile-manifest name from ``COG.md``'s
    frontmatter — a file this call fetches. A caller that has not parsed
    ``COG.md`` yet either calls twice (once for ``COG.md``, once with
    ``manifest_file``) or passes the expected title via ``extra_titles``.

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


def _validate_repo(repo: object) -> None:
    if not isinstance(repo, str) or len(repo) > _MAX_REPO_LENGTH or not _REPO_RE.fullmatch(repo):
        raise OCIInvalidReference(f"invalid repository name {repo!r}")


def _validate_ref(ref: object) -> None:
    if not isinstance(ref, str) or not (_TAG_RE.fullmatch(ref) or _DIGEST_RE.fullmatch(ref)):
        raise OCIInvalidReference(f"invalid reference {ref!r}: expected a tag or a sha256 digest")


def _validate_digest(digest: object) -> None:
    if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
        raise OCIInvalidReference(f"invalid digest {digest!r}: expected sha256:<64 hex>")


def _parse_url(value: str, *, what: str) -> httpx.URL:
    try:
        return httpx.URL(value)
    except (httpx.InvalidURL, TypeError) as exc:
        raise OCIProtocolError(f"{what} is not a valid URL") from exc


def _join_url(base: httpx.URL, target: str, *, what: str) -> httpx.URL:
    try:
        joined = base.join(target)
    except (httpx.InvalidURL, TypeError) as exc:
        raise OCIProtocolError(f"{what} is not a valid URL") from exc
    if joined.scheme not in ("http", "https") or not joined.host:
        raise OCIProtocolError(f"{what} is not an http(s) URL")
    return joined


def _same_origin(a: httpx.URL, b: httpx.URL) -> bool:
    # httpx drops default ports, so https://h and https://h:443 compare equal.
    return (a.scheme, a.host, a.port) == (b.scheme, b.host, b.port)


def _bound(mapping: dict) -> None:
    """Drop the oldest entries so a dict never grows past the token-cache cap."""
    while len(mapping) > MAX_CACHED_TOKENS:
        del mapping[next(iter(mapping))]


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
    """Drain a streaming response into memory, rejecting it once more than ``cap`` bytes arrive.

    ``Content-Length`` is only a hint (it may be absent for chunked bodies, or
    wrong); the running total is what the cap is enforced on. One stream chunk
    (64 KiB) beyond the cap may be read before rejection. Encoded bodies are
    refused because decoding could inflate a chunk past what was measured.
    """
    try:
        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if encoding not in ("", "identity"):
            raise OCIProtocolError(f"{what}: registry sent a Content-Encoding this client does not accept")
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError:
                raise OCIProtocolError(f"{what}: malformed Content-Length header") from None
            if length > cap:
                raise OCITooLarge(f"{what} is {length} bytes, cap is {cap}")
        buffer = bytearray()
        try:
            async for chunk in response.aiter_bytes(_STREAM_CHUNK_BYTES):
                if len(buffer) + len(chunk) > cap:
                    raise OCITooLarge(f"{what} exceeds the {cap}-byte cap")
                buffer.extend(chunk)
        except httpx.HTTPError as exc:
            raise OCITransportError(f"{what}: {type(exc).__name__} while reading") from exc
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
    if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
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
