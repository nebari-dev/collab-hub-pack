"""A stub Postgres server that real psycopg connections can connect to.

Pool-saturation behavior can only be observed against a pool that is *healthy*
— one that hands out connections until it runs out, so later callers actually
queue. Pointing a pool at an unreachable port proves the timeout path, not the
queuing path, and the repo has no Postgres harness in CI (see
``docs/frames-operations.md``), so this speaks just enough of the v3 wire
protocol for libpq to complete a connection:

- decline the SSL/GSS negotiation requests libpq opens with,
- answer the startup packet with ``AuthenticationOk`` + a few
  ``ParameterStatus`` rows + ``BackendKeyData`` + ``ReadyForQuery``,
- answer any statement with an ``ErrorResponse`` (``42P01 undefined_table``)
  followed by ``ReadyForQuery(idle)``.

That is deliberately all: the connections are real, checkouts and returns are
real, and any SQL that does reach the server fails fast and cleanly instead of
hanging a test. Always reporting an *idle* transaction status keeps psycopg's
commit/rollback on context exit a no-op, so returned connections go straight
back into the pool. Real schema/query behavior belongs in the deferred
real-Postgres integration harness, not here.

``stall()`` switches it into the other failure mode a pool cannot see: a server
that completes the handshake, hands over a connection, accepts the statement,
and then simply does not answer. Acquisition bounds do nothing about that one —
only keeping the write off the request path does.
"""

from __future__ import annotations

import socket
import struct
import threading

SSL_REQUEST_CODE = 80877103
GSS_REQUEST_CODE = 80877104

_SERVER_PARAMETERS = (
    ("server_version", "16.0 (nexus-stub)"),
    ("client_encoding", "UTF8"),
    ("standard_conforming_strings", "on"),
    ("integer_datetimes", "on"),
    ("DateStyle", "ISO, MDY"),
    ("TimeZone", "UTC"),
)


def _message(kind: bytes, body: bytes) -> bytes:
    return kind + struct.pack("!I", len(body) + 4) + body


def _parameter_status(name: str, value: str) -> bytes:
    return _message(b"S", name.encode() + b"\x00" + value.encode() + b"\x00")


def _error_response() -> bytes:
    fields = b"".join(
        code + text.encode() + b"\x00"
        for code, text in (
            (b"S", "ERROR"),
            (b"V", "ERROR"),
            (b"C", "42P01"),
            (b"M", "stub postgres server has no relations"),
        )
    )
    return _message(b"E", fields + b"\x00")


_READY_FOR_QUERY = _message(b"Z", b"I")


class StubPostgresServer:
    """A listening stub server; ``url`` is a conninfo string psycopg accepts."""

    def __init__(self):
        self._socket = socket.socket()
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(128)
        self._stopping = threading.Event()
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._sessions: list[socket.socket] = []
        self._lock = threading.Lock()
        self.connections_accepted = 0
        self.statements_received = 0
        self._stalling = threading.Event()
        self._resumed = threading.Event()
        self._resumed.set()

    @property
    def url(self) -> str:
        port = self._socket.getsockname()[1]
        return f"postgresql://nexus:secret@127.0.0.1:{port}/frames"

    def start(self) -> None:
        self._accept_thread.start()

    def stall(self) -> None:
        """Accept statements from now on without ever answering them."""

        self._resumed.clear()
        self._stalling.set()

    def resume(self) -> None:
        """Answer the stalled statements and go back to replying immediately."""

        self._stalling.clear()
        self._resumed.set()

    def stop(self) -> None:
        self.resume()
        self._stopping.set()
        self._socket.close()
        with self._lock:
            sessions = list(self._sessions)
        for session in sessions:
            try:
                session.close()
            except OSError:
                pass
        self._accept_thread.join(timeout=5)

    def _accept_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                client, _ = self._socket.accept()
            except OSError:
                return
            with self._lock:
                self._sessions.append(client)
                self.connections_accepted += 1
            threading.Thread(target=self._session, args=(client,), daemon=True).start()

    def _session(self, client: socket.socket) -> None:
        try:
            if not self._handshake(client):
                return
            self._serve_statements(client)
        except OSError:
            return
        finally:
            try:
                client.close()
            except OSError:
                pass

    def _handshake(self, client: socket.socket) -> bool:
        while True:
            header = _recv_exactly(client, 8)
            if header is None:
                return False
            length, code = struct.unpack("!II", header)
            if code in (SSL_REQUEST_CODE, GSS_REQUEST_CODE):
                # Decline encryption; libpq retries in cleartext on the same socket.
                client.sendall(b"N")
                continue
            if _recv_exactly(client, length - 8) is None:
                return False
            break

        client.sendall(_message(b"R", struct.pack("!I", 0)))
        for name, value in _SERVER_PARAMETERS:
            client.sendall(_parameter_status(name, value))
        client.sendall(_message(b"K", struct.pack("!II", 1234, 5678)))
        client.sendall(_READY_FOR_QUERY)
        return True

    def _serve_statements(self, client: socket.socket) -> None:
        while True:
            kind = _recv_exactly(client, 1)
            if kind is None or kind == b"X":  # Terminate
                return
            header = _recv_exactly(client, 4)
            if header is None:
                return
            (length,) = struct.unpack("!I", header)
            if length > 4 and _recv_exactly(client, length - 4) is None:
                return
            if kind in (b"Q", b"S"):  # simple Query, or Sync ending an extended one
                with self._lock:
                    self.statements_received += 1
                if self._stalling.is_set():
                    # Outlast anything under test, but never wedge teardown.
                    self._resumed.wait(timeout=30)
                client.sendall(_error_response() + _READY_FOR_QUERY)


def _recv_exactly(client: socket.socket, size: int) -> bytes | None:
    buffer = b""
    while len(buffer) < size:
        chunk = client.recv(size - len(buffer))
        if not chunk:
            return None
        buffer += chunk
    return buffer
