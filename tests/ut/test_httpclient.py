"""Outbound HTTPS client tests: chunked decode + local socket integration."""

import compat  # noqa: F401

import unittest
import asyncio
import time

import app.httpclient as httpclient
from app.httpclient import _decode_chunked, https_request, HttpError
from app.util import send_all
from helpers import find_free_port, no_tls, start_raw_server, read_http_request


class TestDecodeChunked(unittest.TestCase):
    def test_decode_chunked(self):
        self.assertEqual(_decode_chunked(b"3\r\nfoo\r\n3\r\nbar\r\n0\r\n\r\n", 8192), b"foobar")
        self.assertEqual(_decode_chunked(b"5;name=value\r\nhello\r\n0\r\n\r\n", 8192), b"hello")
        with self.assertRaises(HttpError):
            _decode_chunked(b"5\r\nhello\r\n0\r\n\r\n", 3)


class TestHttpsRequestLocal(unittest.TestCase):
    def setUp(self):
        self._orig_wrap = httpclient._wrap_tls
        httpclient._wrap_tls = no_tls

    def tearDown(self):
        httpclient._wrap_tls = self._orig_wrap

    def test_tls_default_is_unverified_for_lan(self):
        context = httpclient._make_tls_context(None)
        self.assertEqual(context.verify_mode, httpclient.tls.CERT_NONE)

    def test_response_bodies(self):
        # Content-Length body
        port = find_free_port()

        def handler(conn):
            read_http_request(conn)
            body = b'{"ok":true}'
            send_all(
                conn,
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body,
            )

        start_raw_server(handler, port)
        code, _hdr, body = https_request("127.0.0.1", port, "POST", "/x", {}, b"hi")
        self.assertEqual(code, 200)
        self.assertEqual(body, b'{"ok":true}')

        # Chunked body
        port = find_free_port()

        def handler(conn):
            read_http_request(conn)
            send_all(
                conn,
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
                b"Connection: close\r\n\r\n"
                b"5\r\nhello\r\n3\r\nfoo\r\n0\r\n\r\n",
            )

        start_raw_server(handler, port)
        code, _hdr, body = https_request("127.0.0.1", port, "GET", "/x", {}, None)
        self.assertEqual(code, 200)
        self.assertEqual(body, b"hellofoo")

        # UTF-8 body
        utf8_body = "Hello, world".encode("utf-8")
        port = find_free_port()

        def handler(conn):
            read_http_request(conn)
            send_all(
                conn,
                b"HTTP/1.1 200 OK\r\nContent-Length: "
                + str(len(utf8_body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + utf8_body,
            )

        start_raw_server(handler, port)
        code, _hdr, got = https_request("127.0.0.1", port, "GET", "/", {}, None)
        self.assertEqual(got.decode("utf-8"), "Hello, world")

    def test_large_body_posts_without_expect(self):
        port = find_free_port()
        big = b"x" * 4096

        def handler(conn):
            # Head and body arrive in one burst, so drain by Content-Length
            # rather than by reading the head.
            data = read_http_request(conn)
            assert b"Expect:" not in data
            body = data.partition(b"\r\n\r\n")[2]
            resp = str(len(body)).encode()
            send_all(
                conn,
                b"HTTP/1.1 200 OK\r\nContent-Length: "
                + str(len(resp)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + resp,
            )

        start_raw_server(handler, port)
        code, _hdr, body = https_request("127.0.0.1", port, "POST", "/up", {}, big)
        self.assertEqual(code, 200)
        self.assertEqual(body, b"4096")


def _keepalive_handler(conn, count, seen):
    """Serve up to ``count`` requests on one connection."""
    for _ in range(count):
        data = read_http_request(conn)
        if not data:
            return
        seen.append(data)
        body = str(len(seen)).encode()
        send_all(
            conn,
            b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body,
        )


class TestKeepAlive(unittest.TestCase):
    """One warm connection per endpoint; a dead one is replaced transparently."""

    def setUp(self):
        self._orig_wrap = httpclient._wrap_tls
        httpclient._wrap_tls = no_tls

    def tearDown(self):
        httpclient._wrap_tls = self._orig_wrap
        httpclient.close_keepalive()

    def test_reuse_and_stale_recovery(self):
        # Two requests over one connection: the second skips the handshake and
        # advertises keep-alive instead of close.
        port = find_free_port()
        seen = []
        start_raw_server(lambda c: _keepalive_handler(c, 2, seen), port)
        got = https_request("127.0.0.1", port, "POST", "/a", {}, b"{}", keep_alive=True)
        self.assertEqual(got[2], b"1")
        self.assertIsNotNone(httpclient._cached_conn)
        got = https_request("127.0.0.1", port, "POST", "/b", {}, b"{}", keep_alive=True)
        self.assertEqual(got[2], b"2")
        self.assertEqual(len(seen), 2)
        self.assertIn(b"Connection: keep-alive", seen[1])
        httpclient.close_keepalive()

        # The peer closed after responding: the next request notices and
        # reconnects rather than failing.
        port = find_free_port()
        seen = []
        start_raw_server(lambda c: _keepalive_handler(c, 1, seen), port)
        https_request("127.0.0.1", port, "POST", "/a", {}, b"{}", keep_alive=True)
        self.assertIsNotNone(httpclient._cached_conn)
        fresh = []
        start_raw_server(lambda c: _keepalive_handler(c, 1, fresh), port)
        got = https_request("127.0.0.1", port, "POST", "/b", {}, b"{}", keep_alive=True)
        self.assertEqual(got[2], b"1")
        self.assertEqual(len(fresh), 1)

    def test_not_cached_when_unusable(self):
        # A "Connection: close" reply leaves nothing behind...
        port = find_free_port()

        def handler(conn):
            read_http_request(conn)
            send_all(conn, b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")

        start_raw_server(handler, port)
        code, _hdr, _body = https_request(
            "127.0.0.1", port, "POST", "/a", {}, b"{}", keep_alive=True
        )
        self.assertEqual(code, 200)
        self.assertIsNone(httpclient._cached_conn)

        # ...and neither does a caller that did not opt in.
        port = find_free_port()
        seen = []
        start_raw_server(lambda c: _keepalive_handler(c, 1, seen), port)
        https_request("127.0.0.1", port, "POST", "/a", {}, b"{}")
        self.assertIsNone(httpclient._cached_conn)
        self.assertIn(b"Connection: close", seen[0])


class TestHttpsRequestAsync(unittest.TestCase):
    """Async client: content-length / chunked / EOF bodies over plain TCP."""

    def setUp(self):
        self._orig_ctx = httpclient._make_tls_context
        httpclient._make_tls_context = lambda ca_path: None  # plain TCP

    def tearDown(self):
        asyncio.run(httpclient.close_keepalive_async())
        httpclient._make_tls_context = self._orig_ctx

    def test_async_response_bodies(self):
        # Content-Length body.
        port = find_free_port()

        def handler(conn):
            read_http_request(conn)
            body = b'{"ok":true}'
            send_all(
                conn,
                b"HTTP/1.1 200 OK\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body,
            )

        start_raw_server(handler, port)
        code, _h, body = asyncio.run(
            httpclient.https_request_async("127.0.0.1", port, "POST", "/x", {}, b"hi")
        )
        self.assertEqual(code, 200)
        self.assertEqual(body, b'{"ok":true}')

        # Chunked body.
        port = find_free_port()

        def handler(conn):
            read_http_request(conn)
            send_all(
                conn,
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
                b"Connection: close\r\n\r\n5\r\nhello\r\n3\r\nfoo\r\n0\r\n\r\n",
            )

        start_raw_server(handler, port)
        code, _h, body = asyncio.run(
            httpclient.https_request_async("127.0.0.1", port, "GET", "/x", {}, None)
        )
        self.assertEqual(body, b"hellofoo")

        # EOF-terminated body (no Content-Length / chunked).
        port = find_free_port()

        def handler(conn):
            read_http_request(conn)
            send_all(conn, b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\ndone")

        start_raw_server(handler, port)
        code, _h, body = asyncio.run(
            httpclient.https_request_async("127.0.0.1", port, "GET", "/", {}, None)
        )
        self.assertEqual(body, b"done")

    def test_async_timeout(self):
        # Server accepts but never answers: the timeout must surface as HttpError.
        port = find_free_port()
        start_raw_server(lambda conn: time.sleep(2), port)
        with self.assertRaises(HttpError):
            asyncio.run(
                httpclient.https_request_async(
                    "127.0.0.1", port, "GET", "/", {}, None, timeout=0.3
                )
            )

    def test_async_keepalive_reuses_one_connection(self):
        port = find_free_port()
        seen = []

        def handler(conn):
            for number in range(2):
                seen.append(read_http_request(conn))
                body = str(number).encode("utf-8")
                send_all(
                    conn,
                    b"HTTP/1.1 200 OK\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\nConnection: keep-alive\r\n\r\n"
                    + body,
                )

        start_raw_server(handler, port)

        async def run():
            first = await httpclient.https_request_async(
                "127.0.0.1", port, "POST", "/a", {}, b"{}", keep_alive=True
            )
            second = await httpclient.https_request_async(
                "127.0.0.1", port, "POST", "/b", {}, b"{}", keep_alive=True
            )
            return first, second

        first, second = asyncio.run(run())
        self.assertEqual(first[2], b"0")
        self.assertEqual(second[2], b"1")
        self.assertEqual(len(seen), 2)
        self.assertIn(b"Connection: keep-alive", seen[0])
        self.assertIn(b"Connection: keep-alive", seen[1])


if __name__ == "__main__":
    unittest.main(globals())
