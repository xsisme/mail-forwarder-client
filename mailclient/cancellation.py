"""Cooperative cancellation with non-blocking interruption of active sockets."""
from contextlib import contextmanager
import io
import socket
import threading
import time


class Cancelled(Exception):
    pass


def check_cancel(stop_event):
    if stop_event is not None and stop_event.is_set():
        raise Cancelled()


class CancellationEvent(threading.Event):
    """Best-effort OS wakeup; CancellableSocket also polls on Windows."""

    def __init__(self):
        super().__init__()
        self._sockets = set()
        self._sockets_lock = threading.Lock()

    @staticmethod
    def _interrupt(sock):
        try:
            # Use the base implementation for SSL sockets too: do not mutate
            # SSLSocket's SSL state while another thread is inside a read.
            socket.socket.shutdown(sock, socket.SHUT_RDWR)
        except OSError:
            pass

    def set(self):
        super().set()
        with self._sockets_lock:
            sockets = tuple(self._sockets)
        for sock in sockets:
            self._interrupt(sock)

    def register(self, sock):
        with self._sockets_lock:
            self._sockets.add(sock)
            cancelled = self.is_set()
        if cancelled:
            self._interrupt(sock)

    def unregister(self, sock):
        with self._sockets_lock:
            self._sockets.discard(sock)


@contextmanager
def watch_socket(stop_event, sock):
    watched = isinstance(stop_event, CancellationEvent) and isinstance(sock, socket.socket)
    if watched:
        stop_event.register(sock)
    try:
        check_cancel(stop_event)
        yield
    finally:
        if watched:
            stop_event.unregister(sock)


class _CancellableReader(io.RawIOBase):
    def __init__(self, connection):
        super().__init__()
        self.connection = connection
        # Keep the real socket alive if urllib closes its HTTPConnection
        # before the response body is consumed. Never read through SocketIO:
        # it becomes unusable after the first short polling timeout.
        self.lease = connection.socket.makefile("rb", buffering=0)

    def readable(self):
        return True

    def readinto(self, buffer):
        return self.connection.recv_into(buffer)

    def close(self):
        if not self.closed:
            try:
                self.lease.close()
            finally:
                super().close()


class CancellableSocket:
    """Socket adapter with bounded waits beneath the protocol's buffer.

    Winsock shutdown need not wake Python's pending readiness wait. Short
    socket waits allow the worker itself to observe cancellation. A polling
    timeout never reaches BufferedReader, so fragmented lines remain intact.
    The configured timeout still bounds each receive / complete sendall.
    """

    POLL_SECONDS = 0.2

    def __init__(self, sock, stop_event):
        self.socket = sock
        self.stop_event = stop_event
        self.timeout = sock.gettimeout()

    def __getattr__(self, name):
        return getattr(self.socket, name)

    def _deadline(self):
        return None if self.timeout is None else time.monotonic() + self.timeout

    def _perform(self, operation, deadline):
        try:
            while True:
                check_cancel(self.stop_event)
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("timed out")
                self.socket.settimeout(self.POLL_SECONDS if remaining is None else min(self.POLL_SECONDS, remaining))
                try:
                    return operation()
                except TimeoutError:
                    # Retry the raw receive/send, never the buffered read.
                    continue
                except OSError:
                    check_cancel(self.stop_event)
                    raise
        finally:
            if self.socket.fileno() >= 0:
                self.socket.settimeout(self.timeout)

    def recv_into(self, buffer):
        return self._perform(lambda: self.socket.recv_into(buffer), self._deadline())

    def sendall(self, data, flags=0):
        view = memoryview(data).cast("B")
        deadline = self._deadline()
        while view:
            sent = self._perform(lambda: self.socket.send(view, flags), deadline)
            if sent == 0:
                raise ConnectionError("socket connection closed during send")
            view = view[sent:]

    def makefile(self, mode="rb", buffering=-1, **kwargs):
        if mode not in ("r", "rb", "br"):
            raise ValueError("mail transports require a binary readable socket file")
        raw = _CancellableReader(self)
        if buffering == 0:
            return raw
        return io.BufferedReader(raw, io.DEFAULT_BUFFER_SIZE if buffering is None or buffering < 0 else buffering)


def install_cancellable_io(connection, stop_event):
    """Install after the greeting / TLS upgrade, on the worker thread only."""
    if stop_event is None or not isinstance(connection.sock, socket.socket):
        return
    previous = getattr(connection, "file", None)
    if previous is not None:
        previous.close()
    connection.sock = CancellableSocket(connection.sock, stop_event)
    connection.file = connection.sock.makefile("rb")
