"""Transparent model byte transport; all HTTP/model logic runs in the guest."""
from __future__ import annotations

import selectors
import socket
import threading
from urllib.parse import urlsplit


def relay(left: socket.socket, right: socket.socket, stop: threading.Event):
    with selectors.DefaultSelector() as selector:
        selector.register(left, selectors.EVENT_READ, right)
        selector.register(right, selectors.EVENT_READ, left)
        while not stop.is_set() and selector.get_map():
            for event, _ in selector.select(timeout=0.2):
                source, target = event.fileobj, event.data
                try:
                    data = source.recv(65536)
                    if data:
                        target.sendall(data)
                    else:
                        selector.unregister(source)
                        target.shutdown(socket.SHUT_WR)
                except OSError:
                    return


class SocketBridge:
    def __init__(self, listener, connect):
        self.listener = listener
        self.connect = connect
        self.stop = threading.Event()
        self.connections = set()
        self.threads = []
        self.lock = threading.Lock()
        listener.settimeout(0.2)
        listener.listen(32)
        self.accept_thread = threading.Thread(target=self.accept, daemon=True)
        self.accept_thread.start()

    def accept(self):
        while not self.stop.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            thread = threading.Thread(target=self.serve, args=(client,), daemon=True)
            self.threads.append(thread)
            thread.start()

    def serve(self, client):
        target = None
        with self.lock:
            self.connections.add(client)
        try:
            target = self.connect()
            with self.lock:
                self.connections.add(target)
            relay(client, target, self.stop)
        except OSError:
            pass
        finally:
            with self.lock:
                self.connections.discard(client)
                self.connections.discard(target)
            client.close()
            if target is not None:
                target.close()

    def close(self):
        self.stop.set()
        self.listener.close()
        with self.lock:
            for connection in self.connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
        self.accept_thread.join(timeout=2)
        for thread in self.threads:
            thread.join(timeout=2)


def host_model_bridge(path: str, endpoint: str):
    parsed = urlsplit(endpoint)
    if parsed.scheme != 'http' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Full-agent Firecracker backend requires an HTTP model endpoint without URL credentials')
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    bridge = SocketBridge(listener, lambda: socket.create_connection((parsed.hostname, parsed.port or 80), timeout=10))
    return bridge, 'http://127.0.0.1:18080' + parsed.path.rstrip('/')


def guest_model_bridge():
    def connect():
        connection = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        connection.settimeout(10)
        connection.connect((socket.VMADDR_CID_HOST, 9000))
        return connection
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', 18080))
    return SocketBridge(listener, connect)
