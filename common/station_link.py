"""Control link between the gNB station and the UE station: one JSON object per line over TCP.

The UE station listens (LinkServer), the gNB station connects (LinkClient) over any IP path between the two
laptops: the same Wi-Fi, a travel router, or Tailscale across the internet. Only control goes over it (start /
stop the UE, arm a scheduled switch, status); the demo traffic and the CSI feedback go over the 5G radio.

    request : {"id": 7, "key": "<shared key>", "op": "status", "args": {}}
    reply   : {"id": 7, "ok": true, "result": {...}}   or   {"id": 7, "ok": false, "error": "..."}

Every request carries the shared key from both profiles (station.key); a request without it is refused, so
another device on a shared network cannot drive the UE. One connection, one request at a time: replies come
back in order and a timed-out connection is dropped and reopened.
"""

from __future__ import annotations

import hmac
import json
import logging
import socket
import socketserver
import threading
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

DEFAULT_PORT = 9200
MAX_LINE = 1 << 20


class LinkError(ConnectionError):
    """The other station is unreachable, timed out, or refused the request."""


Handler = Callable[[dict[str, Any]], Any]


class LinkServer:
    """UE station side: answers requests with handlers[op](args); runs on its own threads."""

    def __init__(self, handlers: dict[str, Handler], key: str, host: str = "0.0.0.0", port: int = DEFAULT_PORT):
        if not key:
            raise ValueError("station.key is empty: set the same shared key in both station profiles")
        self.handlers, self.key = handlers, key
        self.last_request = 0.0  # time.monotonic() of the last authenticated request (link status)
        self.peer = ""
        outer = self

        class _Conn(socketserver.StreamRequestHandler):
            def handle(self):  # noqa: D401 - one client connection
                peer = f"{self.client_address[0]}:{self.client_address[1]}"
                log.info("link: %s connected", peer)
                while True:
                    try:
                        line = self.rfile.readline(MAX_LINE)
                    except OSError:
                        break
                    if not line:
                        break
                    reply = outer._answer(line, peer)
                    try:
                        self.wfile.write((json.dumps(reply) + "\n").encode())
                    except OSError:
                        break
                log.info("link: %s disconnected", peer)

        class _Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server = _Server((host, int(port)), _Conn)
        self.address = self._server.server_address
        self._thread = threading.Thread(target=self._server.serve_forever, name="link-server", daemon=True)

    def start(self) -> "LinkServer":
        self._thread.start()
        log.info("link: listening on %s:%d", *self.address[:2])
        return self

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def connected(self, within_s: float = 3.0) -> bool:
        """A gNB station spoke to us within the last within_s seconds (it polls status twice a second)."""
        return time.monotonic() - self.last_request < within_s

    def _answer(self, line: bytes, peer: str) -> dict[str, Any]:
        try:
            req = json.loads(line)
        except ValueError:
            return {"id": None, "ok": False, "error": "not JSON"}
        rid = req.get("id")
        if not hmac.compare_digest(str(req.get("key", "")), self.key):
            log.warning("link: refused a request from %s (wrong key)", peer)
            return {"id": rid, "ok": False, "error": "wrong key"}
        self.last_request, self.peer = time.monotonic(), peer.rsplit(":", 1)[0]
        op = req.get("op")
        handler = self.handlers.get(op)
        if handler is None:
            return {"id": rid, "ok": False, "error": f"unknown op {op!r}"}
        try:
            return {"id": rid, "ok": True, "result": handler(req.get("args") or {})}
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            log.exception("link: %s failed", op)
            return {"id": rid, "ok": False, "error": str(exc) or type(exc).__name__}


class LinkClient:
    """gNB station side. call() raises LinkError on any failure; the next call reconnects.

    delay_s adds that much one-way delay to each request and to each reply (testing: emulate a relayed link).
    """

    def __init__(self, host: str, key: str, port: int = DEFAULT_PORT, timeout_s: float = 3.0, delay_s: float = 0.0):
        self.host, self.port, self.key = host, int(port), key
        self.timeout_s, self.delay_s = float(timeout_s), float(delay_s)
        self._sock: socket.socket | None = None
        self._rfile = None
        self._lock = threading.Lock()
        self._id = 0
        self.rtt_s = float("nan")  # round trip of the last successful request
        self.last_ok = 0.0  # time.monotonic() of the last successful request

    def close(self) -> None:
        with self._lock:
            self._drop()

    def _drop(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock, self._rfile = None, None

    def connected(self, within_s: float = 3.0) -> bool:
        return time.monotonic() - self.last_ok < within_s

    def call(self, op: str, timeout_s: float | None = None, **args: Any) -> Any:
        timeout = self.timeout_s if timeout_s is None else float(timeout_s)
        with self._lock:
            self._id += 1
            req = {"id": self._id, "key": self.key, "op": op, "args": args}
            t0 = time.monotonic()
            try:
                if self._sock is None:
                    self._sock = socket.create_connection((self.host, self.port), timeout=timeout)
                    self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    self._rfile = self._sock.makefile("rb")
                self._sock.settimeout(timeout)
                if self.delay_s:
                    time.sleep(self.delay_s)
                self._sock.sendall((json.dumps(req) + "\n").encode())
                while True:  # skip a late reply to an earlier, timed-out request
                    line = self._rfile.readline(MAX_LINE)
                    if not line:
                        raise LinkError(f"UE station {self.host}:{self.port} closed the link")
                    reply = json.loads(line)
                    if reply.get("id") == req["id"]:
                        break
                if self.delay_s:
                    time.sleep(self.delay_s)
            except (OSError, ValueError) as exc:
                self._drop()
                if isinstance(exc, LinkError):
                    raise
                raise LinkError(f"UE station {self.host}:{self.port}: {exc or type(exc).__name__}") from exc
            if not reply.get("ok"):
                raise LinkError(f"UE station refused {op}: {reply.get('error')}")
            self.rtt_s, self.last_ok = time.monotonic() - t0, time.monotonic()
            return reply.get("result")
