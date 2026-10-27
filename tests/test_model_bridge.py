import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cpu_swe_benchmark.model_bridge import host_model_bridge


def test_model_bridge_preserves_streaming_and_auth(tmp_path):
    release = threading.Event()
    observed = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            observed.append((self.path, self.headers.get('Authorization'), self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200)
            self.send_header('Content-Length', '10')
            self.end_headers()
            self.wfile.write(b'first')
            self.wfile.flush()
            release.wait(timeout=3)
            self.wfile.write(b'final')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    path = str(tmp_path/'model.sock')
    bridge, endpoint = host_model_bridge(path, f'http://127.0.0.1:{server.server_port}/v1')
    try:
        assert endpoint == 'http://127.0.0.1:18080/v1'
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(path)
            connection.sendall(b'POST /v1/chat/completions HTTP/1.0\r\nAuthorization: Bearer test-only\r\nContent-Length: 2\r\n\r\n{}')
            output = b''
            while not output.endswith(b'first'):
                output += connection.recv(4096)
            assert b'final' not in output
            release.set()
            while not output.endswith(b'firstfinal'):
                output += connection.recv(4096)
        assert observed == [('/v1/chat/completions', 'Bearer test-only', b'{}')]
    finally:
        release.set()
        bridge.close()
        server.shutdown()
        server.server_close()
    assert not bridge.accept_thread.is_alive()
    assert all(not thread.is_alive() for thread in bridge.threads)


@pytest.mark.parametrize('endpoint', ['https://example.com/v1', 'http://user:secret@example.com/v1', 'file:///tmp/model'])
def test_model_bridge_rejects_unsupported_endpoints(tmp_path, endpoint):
    with pytest.raises(ValueError):
        host_model_bridge(str(tmp_path/'model.sock'), endpoint)
