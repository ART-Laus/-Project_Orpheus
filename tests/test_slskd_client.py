import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from orpheus.slskd_client import SlskdClient, SlskdError

SEARCH_ID = "11111111-1111-1111-1111-111111111111"


def _json_resp(handler: BaseHTTPRequestHandler, data, code=200):
    body = json.dumps(data).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class FakeSlskdServer(BaseHTTPRequestHandler):
    responses = [
        {
            "username": "peer1",
            "files": [
                {
                    "filename": "/music/Band/Song.mp3",
                    "size": 11000000,
                    "bitRate": 320,
                    "length": 200,
                }
            ],
        }
    ]
    enqueued = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        if self.path == "/api/v0/searches":
            _json_resp(self, {"id": SEARCH_ID})
        elif self.path.startswith("/api/v0/transfers/downloads/"):
            size = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(size).decode() if size else "[]")
            self.enqueued.extend(body)
            _json_resp(
                self,
                {"enqueued": [{"id": "transfer-1", "filename": body[0]["filename"]}]},
            )
        else:
            _json_resp(self, {}, 404)

    def do_GET(self):
        if self.path == f"/api/v0/searches/{SEARCH_ID}":
            _json_resp(self, {"state": "Completed"})
        elif self.path == f"/api/v0/searches/{SEARCH_ID}/responses":
            _json_resp(self, self.responses)
        elif self.path == "/api/v0/transfers/downloads":
            _json_resp(self, [])
        else:
            _json_resp(self, {}, 404)


@pytest.fixture
def server():
    srv = HTTPServer(("127.0.0.1", 0), FakeSlskdServer)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()


def test_search_returns_responses(server):
    client = SlskdClient(base_url=f"http://127.0.0.1:{server.server_port}", api_key="k")
    responses = client.search("Band Song")
    assert responses[0]["username"] == "peer1"
    assert responses[0]["files"][0]["bitRate"] == 320


def test_enqueue_sends_size(server):
    client = SlskdClient(base_url=f"http://127.0.0.1:{server.server_port}", api_key="k")
    tid = client.enqueue_download("peer1", "/music/Band/Song.mp3", 11000000)
    assert tid == "transfer-1"
    assert FakeSlskdServer.enqueued == [{"filename": "/music/Band/Song.mp3", "size": 11000000}]


def test_api_key_from_config(tmp_path):
    cfg = tmp_path / "slskd.yml"
    cfg.write_text(
        "web:\n  authentication:\n    api_keys:\n      orpheus:\n        key: secret-key-123\n",
        encoding="utf-8",
    )
    assert SlskdClient.api_key_from_config(cfg) == "secret-key-123"


def test_error_when_server_down(tmp_path):
    client = SlskdClient(base_url="http://127.0.0.1:1", api_key="k")
    try:
        client.search("x")
        assert False
    except SlskdError:
        pass
