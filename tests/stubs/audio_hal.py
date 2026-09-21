"""Socket-based audio hardware mock for app-flow testing.

Replaces real I2S with TCP sockets so an external CPython process can
stream audio in (mic) and receive audio out (speaker).

Usage in run_agent.py:
    from audio_hal import AudioInput, AudioOutput
    audio_in = AudioInput(port=19301)
    audio_out = AudioOutput(port=19302)
"""

import errno
import socket
import _thread
import time

ACCEPT_TIMEOUT_SEC = 30


def _conn_alive(conn):
    """True if the peer has not closed the connection.

    Only safe for connections whose peer never sends data (speaker
    clients): a non-blocking recv returns b'' on EOF and raises EAGAIN
    when the connection is alive but idle. Used to skip stale backlog
    entries left behind when the client timed out and reconnected.
    """
    try:
        conn.settimeout(0)
        data = conn.recv(1)
        return bool(data)
    except OSError as e:
        return bool(e.args) and e.args[0] in (errno.EAGAIN, errno.EALREADY)
    finally:
        conn.settimeout(ACCEPT_TIMEOUT_SEC)


class AudioInput:
    """Mock microphone: reads PCM from a TCP connection.

    Listens on the given port. The external test client connects and
    streams raw PCM16-LE data. read() blocks until data is available.
    """

    def __init__(self, port, rate=16000, **kwargs):
        self._rate = rate
        self._port = port
        self._conn = None
        self._closed = False
        self._no_client = False
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(socket.getaddrinfo("0.0.0.0", port)[0][-1])
        self._srv.listen(1)
        # Short accept poll: when no client is connected yet, read()
        # returns silence quickly so the agent's listen loop keeps its
        # timeout checks running instead of stalling in accept().
        self._srv.settimeout(0.2)
        print("[AudioIn] Listening on port {}".format(port))

    @property
    def rate(self):
        return self._rate

    def _ensure_conn(self):
        """Accept a pending mic client if one is ready (short poll)."""
        if self._conn is not None or self._no_client:
            return
        try:
            self._conn, addr = self._srv.accept()
            self._conn.settimeout(ACCEPT_TIMEOUT_SEC)
            print("[AudioIn] Client connected: {}".format(addr))
        except OSError:
            pass  # no client yet; read() returns silence and retries

    def read(self, nbytes):
        """Read PCM bytes from socket. Blocks until data available."""
        if self._closed:
            return b""
        try:
            self._ensure_conn()
            if self._conn is None:
                return b""
            data = bytearray()
            while len(data) < nbytes:
                chunk = self._conn.recv(nbytes - len(data))
                if not chunk:
                    # Mic client streams once per wake round; after it
                    # leaves, fail fast instead of waiting for another.
                    self._conn = None
                    self._no_client = True
                    return bytes(data) if data else b""
                data.extend(chunk)
            return bytes(data)
        except OSError:
            return b""

    def deinit(self):
        self._closed = True
        try:
            if self._conn:
                self._conn.close()
        except OSError:
            pass
        try:
            self._srv.close()
        except OSError:
            pass


class AudioOutput:
    """Mock speaker: sends PCM to a TCP connection.

    Listens on the given port. The external test client connects to
    receive TTS output PCM. write() blocks until a client is connected
    and keeps the connection open across writes; end() closes it (the
    client plays the collected utterance on close).

    With pace=True (default, used by the unix-dev audio bridge) writes
    sleep for the audio duration so "playback done" matches a real
    speaker; e2e passes pace=False since nothing audible is played.
    """

    def __init__(self, port, rate=16000, pace=True, **kwargs):
        self._rate = rate
        self._port = port
        self._pace = pace
        self._conn = None
        self._closed = False
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(socket.getaddrinfo("0.0.0.0", port)[0][-1])
        self._srv.listen(1)
        self._srv.settimeout(ACCEPT_TIMEOUT_SEC)
        print("[AudioOut] Listening on port {}".format(port))

    @property
    def rate(self):
        return self._rate

    def _ensure_conn(self):
        if self._conn is None:
            while True:
                try:
                    conn, addr = self._srv.accept()
                except OSError as e:
                    print("[AudioOut] accept failed: {}".format(e))
                    raise
                if not _conn_alive(conn):
                    try:
                        conn.close()
                    except OSError:
                        pass
                    continue
                self._conn = conn
                print("[AudioOut] Client connected: {}".format(addr))
                return

    def write(self, pcm):
        """Send PCM bytes to connected client. Connection stays open until end()."""
        if self._closed:
            return
        off = 0
        reconnects = 3
        while off < len(pcm):
            try:
                self._ensure_conn()
            except OSError:
                return
            try:
                sent = self._conn.send(pcm[off:])
                if sent == 0:
                    raise OSError(errno.ECONNRESET)
                off += sent
            except OSError as e:
                # Client went away (e.g. bridge speaker reconnect): drop
                # the dead connection and pick up the fresh one.
                print("[AudioOut] write failed: {}".format(e))
                self.end()
                reconnects -= 1
                if reconnects <= 0:
                    return
        # Like real I2S, pace writes to the audio clock: TCP buffers
        # would otherwise absorb whole utterances instantly, making the
        # agent's "playback done" run far ahead of audible playback.
        if self._pace:
            time.sleep(len(pcm) / (2.0 * self._rate))

    def end(self):
        """Close the current playback connection (client plays on close)."""
        if self._conn:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None

    def deinit(self):
        self._closed = True
        self.end()
        try:
            self._srv.close()
        except OSError:
            pass
