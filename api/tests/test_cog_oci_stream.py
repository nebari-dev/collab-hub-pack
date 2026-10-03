"""The streamed reads the Hub's ``/v2/`` surface is built on (issue #179).

``OCIClient.fetch_manifest`` returns a manifest as stored (an index is not
followed), and ``OCIClient.open_blob`` returns a :class:`BlobStream` whose
bytes are hashed as they pass. The property under test for the stream is the
one the relay depends on: a body that is wrong -- too long, too short, the
wrong bytes, cut off -- never has its last chunk released.
"""

from __future__ import annotations

import hashlib
import json
import logging

import httpx
import pytest

from collab_hub_api.cogs import oci
from collab_hub_api.cogs.oci import (
    MEDIA_TYPE_OCI_INDEX,
    MEDIA_TYPE_OCI_MANIFEST,
    MEDIA_TYPE_PIXI_CONFIG,
    BlobStream,
    OCIClient,
    OCIDigestMismatch,
    OCIInvalidReference,
    OCINotFound,
    OCIProtocolError,
    OCITooLarge,
    OCITransportError,
    index_children,
    is_index_manifest,
    is_sha256_digest,
    is_tag,
)

REGISTRY = "https://registry.example"
REPO = "cogs/alpha"
CHUNK = oci._STREAM_CHUNK_BYTES


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


BODY = bytes(range(256)) * 1024  # 256 KiB: four stream chunks
DIGEST = sha256(BODY)


def client_for(handler) -> OCIClient:
    return OCIClient(REGISTRY, transport=httpx.MockTransport(handler))


def blob_handler(body: bytes = BODY, *, headers: dict | None = None, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers=headers or {})

    return handler


async def drain(stream: BlobStream, **kwargs) -> tuple[bytes, BaseException | None]:
    """Everything the stream released, and what ended it (``None`` for a clean end)."""

    received = bytearray()
    try:
        async for chunk in stream.iter_verified(**kwargs):
            received.extend(chunk)
    except oci.OCIError as exc:
        return bytes(received), exc
    return bytes(received), None


async def test_a_good_blob_streams_whole_in_chunks():
    async with client_for(blob_handler()) as client:
        stream = await client.open_blob(REPO, DIGEST)
        assert stream.digest == DIGEST and stream.content_length == len(BODY)
        chunks = [chunk async for chunk in stream.iter_verified(max_bytes=len(BODY), expected_size=len(BODY))]
    assert b"".join(chunks) == BODY
    assert len(chunks) == 4 and all(len(chunk) <= CHUNK for chunk in chunks), "relayed chunk by chunk, not buffered"


async def test_an_empty_blob_streams_nothing():
    async with client_for(blob_handler(b"")) as client:
        stream = await client.open_blob(REPO, sha256(b""))
        assert await drain(stream, max_bytes=10, expected_size=0) == (b"", None)


@pytest.mark.parametrize(
    ("body", "kwargs", "error", "match"),
    [
        # Same length, one byte different: only the hash can tell.
        (BODY[:-1] + b"\x00", {"max_bytes": len(BODY), "expected_size": len(BODY)}, OCIDigestMismatch, "hashes to"),
        (BODY + b"x", {"max_bytes": len(BODY) * 2, "expected_size": len(BODY)}, OCIDigestMismatch, "longer than"),
        (BODY[:-1], {"max_bytes": len(BODY), "expected_size": len(BODY)}, OCIDigestMismatch, "declared"),
        (BODY, {"max_bytes": len(BODY) - 1}, OCITooLarge, "exceeds the"),
    ],
)
async def test_a_wrong_blob_never_has_its_last_chunk_released(body, kwargs, error, match):
    async with client_for(blob_handler(body)) as client:
        stream = await client.open_blob(REPO, DIGEST)
        received, raised = await drain(stream, **kwargs)
    assert isinstance(raised, error) and match in str(raised)
    assert len(received) <= len(body) - 1 and len(received) < len(BODY)
    assert BODY.startswith(received)


async def test_a_single_chunk_blob_that_is_wrong_releases_nothing():
    async with client_for(blob_handler(b"tampered")) as client:
        stream = await client.open_blob(REPO, sha256(b"original"))
        assert (await drain(stream, max_bytes=100))[0] == b""


async def test_a_connection_cut_mid_body_is_a_transport_error():
    class Cut(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield BODY[:CHUNK]
            yield BODY[CHUNK : 2 * CHUNK]
            raise httpx.ReadError("connection reset by registry.example")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Cut())

    async with client_for(handler) as client:
        stream = await client.open_blob(REPO, DIGEST)
        assert stream.content_length is None
        received, raised = await drain(stream, max_bytes=len(BODY))
    assert isinstance(raised, OCITransportError)
    # The httpx class, never its message: that one names the host.
    assert str(raised) == f"blob {DIGEST}: ReadError while reading"
    assert received == BODY[:CHUNK]


async def test_an_encoded_body_is_refused_before_any_byte():
    class Raw(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield BODY

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Raw(), headers={"Content-Encoding": "gzip"})

    async with client_for(handler) as client:
        stream = await client.open_blob(REPO, DIGEST)
        received, raised = await drain(stream, max_bytes=len(BODY))
    assert received == b"" and isinstance(raised, OCIProtocolError) and "Content-Encoding" in str(raised)


async def test_the_stream_is_closed_however_it_ends():
    async with client_for(blob_handler()) as client:
        stream = await client.open_blob(REPO, DIGEST)
        iterator = stream.iter_verified(max_bytes=len(BODY))
        assert await anext(iterator)
        await iterator.aclose()  # a client that went away
        assert stream._response.is_closed
        await stream.aclose()  # idempotent
        unread = await client.open_blob(REPO, DIGEST)
        await unread.aclose()
        assert unread._response.is_closed


async def test_open_blob_follows_redirects_and_maps_statuses():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "storage.example":
            return httpx.Response(200, content=BODY)
        if request.url.path.endswith(DIGEST):
            return httpx.Response(307, headers={"Location": f"https://storage.example/{DIGEST}?sig=abc"})
        if request.url.path.endswith("a" * 64):
            return httpx.Response(500, text="registry.example exploded")
        return httpx.Response(404, text="nope")

    async with client_for(handler) as client:
        stream = await client.open_blob(REPO, DIGEST)
        assert (await drain(stream, max_bytes=len(BODY), expected_size=len(BODY))) == (BODY, None)
        with pytest.raises(OCINotFound):
            await client.open_blob(REPO, "sha256:" + "b" * 64)
        with pytest.raises(OCIProtocolError, match="HTTP 500") as caught:
            await client.open_blob(REPO, "sha256:" + "a" * 64)
        assert "exploded" not in str(caught.value) and "registry.example" not in str(caught.value)
        for bad in ("latest", "sha256:short", "md5:" + "a" * 32):
            with pytest.raises(OCIInvalidReference):
                await client.open_blob(REPO, bad)
        with pytest.raises(OCIInvalidReference):
            await client.open_blob("Not/Valid", DIGEST)


def test_content_length_is_read_only_when_usable():
    assert BlobStream(httpx.Response(200, headers={"Content-Length": "12"}), DIGEST).content_length == 12
    assert BlobStream(httpx.Response(200, headers={"Content-Length": "-1"}), DIGEST).content_length is None
    assert BlobStream(httpx.Response(200, headers={"Content-Length": "many"}), DIGEST).content_length is None


def _index(children: list[dict] | None) -> bytes:
    document: dict = {"schemaVersion": 2, "mediaType": MEDIA_TYPE_OCI_INDEX}
    if children is not None:
        document["manifests"] = children
    return json.dumps(document).encode()


async def test_fetch_manifest_returns_an_index_as_stored():
    child = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": MEDIA_TYPE_OCI_MANIFEST,
            "config": {"mediaType": MEDIA_TYPE_PIXI_CONFIG, "digest": sha256(b"{}"), "size": 2},
            "layers": [],
        }
    ).encode()
    index = _index([{"mediaType": MEDIA_TYPE_OCI_MANIFEST, "digest": sha256(child), "size": len(child)}])
    bodies = {sha256(index): index, "multi": index, sha256(child): child}

    def handler(request: httpx.Request) -> httpx.Response:
        ref = request.url.path.rsplit("/", 1)[-1]
        if ref not in bodies:
            return httpx.Response(404)
        return httpx.Response(200, content=bodies[ref], headers={"Docker-Content-Digest": sha256(bodies[ref])})

    async with client_for(handler) as client:
        for ref in ("multi", sha256(index)):
            fetched = await client.fetch_manifest(REPO, ref)
            assert fetched.raw == index and fetched.digest == sha256(index), "not resolved to a child"
            assert is_index_manifest(fetched)
            assert [entry.digest for entry in index_children(fetched)] == [sha256(child)]
        image = await client.fetch_manifest(REPO, sha256(child))
        assert not is_index_manifest(image) and index_children(image) == []
        with pytest.raises(OCINotFound):
            await client.fetch_manifest(REPO, "missing")
        with pytest.raises(OCIDigestMismatch):
            # Asked for one digest, handed another manifest's bytes.
            bodies["sha256:" + "c" * 64] = child
            await client.fetch_manifest(REPO, "sha256:" + "c" * 64)
        with pytest.raises(OCIInvalidReference):
            await client.fetch_manifest(REPO, "not a ref")
        with pytest.raises(OCIInvalidReference):
            await client.fetch_manifest("Not/Valid", "latest")


def test_index_children_refuses_an_index_without_a_list():
    broken = oci._parse_manifest(_index(None), "sha256:" + "0" * 64, content_type_fallback="")
    with pytest.raises(OCIProtocolError, match="no 'manifests' list"):
        index_children(broken)


def test_the_reference_grammar_helpers():
    assert is_tag("latest") and is_tag("1.0.0-rc_1") and not is_tag("has space") and not is_tag(None)
    assert not is_tag("sha256:" + "a" * 64)
    assert is_sha256_digest("sha256:" + "a" * 64)
    assert not is_sha256_digest("sha256:" + "A" * 64) and not is_sha256_digest("sha512:" + "a" * 128)
    assert not is_sha256_digest(None)


def test_httpx_request_logs_lose_their_query_string(caplog):
    """A pre-signed storage URL's query is its credential; httpx would log it at INFO."""

    signed = httpx.URL("https://storage.example/blob/sha256:abc?X-Amz-Signature=secret-signature&x=1")
    plain = httpx.URL("https://registry.example/v2/")
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info('HTTP Request: %s %s "%s"', "GET", signed, "HTTP/1.1 200 OK")
        logging.getLogger("httpx").info('HTTP Request: %s %s "%s"', "GET", plain, "HTTP/1.1 200 OK")
        logging.getLogger("httpx").info("no arguments at all")
        logging.getLogger("httpx").info("mapping %(a)s", {"a": 1})
    messages = [record.getMessage() for record in caplog.records]
    assert messages[0] == 'HTTP Request: GET https://storage.example/blob/sha256:abc "HTTP/1.1 200 OK"'
    assert messages[1] == 'HTTP Request: GET https://registry.example/v2/ "HTTP/1.1 200 OK"'
    assert messages[2:] == ["no arguments at all", "mapping 1"]
    assert "secret-signature" not in caplog.text
    # Installed once, however many times the module is imported.
    assert sum(isinstance(f, oci._RequestLogQueryFilter) for f in logging.getLogger("httpx").filters) == 1
