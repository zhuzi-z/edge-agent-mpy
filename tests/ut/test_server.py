"""HTTPServer integration tests over real sockets."""

import compat  # noqa: F401

import json
import time
import socket
import unittest

import app.config as config
from helpers import ServerHarness, raw_request
from app.controllers import WiFiManager
from app.util import send_all, uptime_seconds
from app.api.server import HTTPServer, route_index, route_info


class TestHTTPServer(ServerHarness, unittest.TestCase):
    def _build_server(self):
        wifi = WiFiManager(ssid="test", password="test123")
        server = HTTPServer(wifi, port=self.port)

        async def echo(server, method, path, body):
            return (200, "OK", "text/plain", body)

        server.register_route("POST", "/echo", echo)
        server.register_route("GET", "/", route_index)
        server.register_route("GET", "/info", route_info)
        return server

    def test_routes(self):
        self._start()
        # Echo round-trips a UTF-8 body verbatim.
        payload = "Hello, device! 🚀"
        status, _head, body = raw_request(self.port, "POST", "/echo", body=payload)
        self.assertEqual(status, 200)
        self.assertEqual(body, payload.encode("utf-8"))
        # Unknown paths return 404.
        status, _ = self._get("/unknown")
        self.assertEqual(status, 404)
        # /info reports device identity + system stats (used by LAN scanners).
        status, body = self._get("/info")
        self.assertEqual(status, 200)
        data = json.loads(body)
        for key in ("name", "memory", "disk", "heap", "gpio", "uptime_seconds"):
            self.assertIn(key, data)
        self.assertEqual(list(data["gpio"].keys()), ["38"])
        self.assertEqual(data["name"], "Edge Agent")
        self.assertIsInstance(data["heap"], list)
        # CORS preflight succeeds so the WebUI works from a file:// copy.
        status, head, _body = raw_request(self.port, "OPTIONS", "/info")
        self.assertEqual(status, 204)
        self.assertIn("Access-Control-Allow-Origin: *", head)
        self.assertIn("Access-Control-Allow-Private-Network: true", head)
        self.assertIn("Access-Control-Allow-Methods", head)
        self.assertIn("Access-Control-Allow-Headers", head)

    def test_index_etag_not_modified(self):
        self._start()
        status, head, body = raw_request(self.port, "GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("ETag:", head)
        etag = next(
            line.split(":", 1)[1].strip()
            for line in head.split("\r\n")
            if line.startswith("ETag:")
        )
        status, head, body = raw_request(self.port, "GET", "/", headers={"If-None-Match": etag})
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")
        self.assertIn("Cache-Control: no-cache, must-revalidate", head)

    def test_request_hardening(self):
        """Quiet clients are dropped and oversized bodies refused, and the
        server keeps serving afterwards."""
        self._start()
        # An over-long Content-Length is refused before anything is allocated.
        status, head, body, _ms = self._raw(
            "POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: {}\r\n\r\n".format(
                config.HTTP_MAX_BODY_SIZE + 1
            )
        )
        self.assertEqual(status, 413)
        self.assertIn("Payload Too Large", head)
        self.assertIn("error", json.loads(body))
        # A client that connects and never finishes its request is dropped at
        # the read timeout instead of pinning a socket until lwIP runs out.
        orig = config.HTTP_CLIENT_READ_TIMEOUT_SEC
        config.HTTP_CLIENT_READ_TIMEOUT_SEC = 1
        try:
            status, head, body, elapsed = self._raw("GET /info HTTP/1.1\r\n", timeout=5)
        finally:
            config.HTTP_CLIENT_READ_TIMEOUT_SEC = orig
        self.assertEqual((status, head, body), (0, "", ""))
        self.assertLess(elapsed, 3000)
        # Nothing was taken down: /info still answers, and uptime accumulates
        # through ticks_ms() wraparound instead of restarting at zero.
        status, body = self._get("/info")
        self.assertEqual(status, 200)
        first = json.loads(body)["uptime_seconds"]
        self.assertGreaterEqual(first, 0)
        time.sleep(0.02)
        self.assertGreaterEqual(uptime_seconds(), first)

    def _raw(self, request, timeout=5):
        """Send a raw request head and read the whole reply.

        Returns (status, head, body, elapsed_ms); status is 0 when the peer
        closed without answering, which is what a dropped client sees.
        """
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(socket.getaddrinfo("127.0.0.1", self.port)[0][-1])
        t0 = time.ticks_ms()
        send_all(s, request.encode("utf-8"))
        data = b""
        try:
            while True:
                chunk = s.recv(512)
                if not chunk:
                    break
                data += chunk
        except OSError:
            pass
        elapsed = time.ticks_diff(time.ticks_ms(), t0)
        s.close()
        hend = data.find(b"\r\n\r\n")
        if hend < 0:
            return 0, "", data.decode("utf-8"), elapsed
        head = data[:hend].split(b"\r\n")[0].decode("utf-8")
        try:
            status = int(head.split(" ")[1])
        except (IndexError, ValueError):
            status = 0
        return status, head, data[hend + 4 :].decode("utf-8"), elapsed


if __name__ == "__main__":
    unittest.main(globals())
