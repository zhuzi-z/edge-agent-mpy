"""
Stub for the MicroPython ``tls`` module (Mbed TLS).
Uses CPython's ``ssl`` module for real TLS when available (appflow tests),
falls back to plain sockets for unit tests without a TLS server.
"""

PROTOCOL_TLS_CLIENT = 0
CERT_NONE = 0
CERT_OPTIONAL = 1
CERT_REQUIRED = 2

try:
    import ssl as _ssl

    _HAS_SSL = True
except ImportError:
    _HAS_SSL = False


class SSLContext:
    """Wraps CPython ssl.SSLContext or falls back to plain sockets."""

    def __init__(self, protocol=PROTOCOL_TLS_CLIENT):
        self._protocol = protocol
        self.verify_mode = CERT_NONE
        self._ca_path = None
        if _HAS_SSL:
            self._ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
            self._ctx.check_hostname = False
            self._ctx.verify_mode = _ssl.CERT_NONE
        else:
            self._ctx = None

    def load_verify_locations(self, path):
        """Load a CA bundle for certificate verification."""
        self._ca_path = path
        if self._ctx is not None:
            try:
                if isinstance(path, bytes):
                    self._ctx.load_verify_locations(cadata=path.decode("ascii"))
                else:
                    self._ctx.load_verify_locations(path)
                self._ctx.verify_mode = _ssl.CERT_REQUIRED
                self._ctx.check_hostname = False
            except Exception:
                pass

    def wrap_socket(self, sock, server_hostname=None, do_handshake_on_connect=True):
        """Wrap socket with TLS, or return raw socket if ssl unavailable.

        ``do_handshake_on_connect`` is accepted for compatibility with
        ``asyncio.open_connection(ssl=ctx)`` and forwarded to the native
        ``ssl`` module.
        """
        if self._ctx is not None:
            try:
                return self._ctx.wrap_socket(
                    sock,
                    server_hostname=server_hostname,
                    do_handshake_on_connect=do_handshake_on_connect,
                )
            except Exception:
                return sock
        return sock
