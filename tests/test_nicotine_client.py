import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from orpheus.models import Track
from orpheus.nicotine_client import NicotineClient, NicotineError
from orpheus.sources.nicotine import NicotineSource

TOKEN = 12345
TRANSFER_ID = "peer1%2Fmusic%2FBand%2FSong.mp3"


def _json_resp(handler: BaseHTTPRequestHandler, data, code=200):
    body = json.dumps(data).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class FakeNicotineBridge(BaseHTTPRequestHandler):
    results = [
        {
            "username": "peer1",
            "path": "/music/Band/Song.mp3",
            "size": 11000000,
            "bitrate": 320,
            "length": 200,
        },
        {
            "username": "peer2",
            "path": "/trash/Band - Song (radio).mp3",
            "size": 9000000,
            "bitrate": 128,
            "length": 195,
        },
    ]
    transfer_state = {"status": "Finished", "size": 11000000,
                      "path": "/tmp/nicotine/abc123/Song.mp3"}
    searches = []
    downloads = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        size = int(self.headers.get("Content-Length", 0) or 0)
        body = json.loads(self.rfile.read(size).decode()) if size else {}
        if self.path == "/search":
            self.searches.append(body.get("query", ""))
            _json_resp(self, {"token": TOKEN})
        elif self.path == "/download":
            self.downloads.append(body)
            _json_resp(
                self,
                {"id": urllib.parse.quote(body["username"] + body["path"], safe="")},
            )
        else:
            _json_resp(self, {"error": "unknown"}, 404)

    def do_GET(self):
        if self.path == "/ping":
            _json_resp(self, {"ok": True, "connected": True, "username": "u"})
        elif self.path.startswith("/results"):
            _json_resp(self, {"term": "Band Song", "results": self.results})
        elif self.path.startswith("/transfer"):
            _json_resp(self, self.transfer_state)
        else:
            _json_resp(self, {"error": "unknown"}, 404)


@pytest.fixture
def server():
    srv = HTTPServer(("127.0.0.1", 0), FakeNicotineBridge)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()


def _url(server) -> str:
    return f"http://127.0.0.1:{server.server_port}"


def test_ping(server):
    client = NicotineClient(base_url=_url(server))
    resp = client.ping()
    assert resp["ok"] is True
    assert resp["connected"] is True
    assert client.connected()


def test_search_returns_deduped_results(server):
    client = NicotineClient(base_url=_url(server), search_timeout_s=1)
    results = client.search("Band Song")
    assert FakeNicotineBridge.searches == ["Band Song"]
    assert len(results) == 2
    assert results[0]["bitrate"] == 320


def test_enqueue_download(server):
    client = NicotineClient(base_url=_url(server))
    tid = client.enqueue_download("peer1", "/music/Band/Song.mp3", 11000000)
    assert tid == "peer1%2Fmusic%2FBand%2FSong.mp3"
    assert FakeNicotineBridge.downloads == [
        {"username": "peer1", "path": "/music/Band/Song.mp3", "size": 11000000}
    ]


def test_wait_download_finished(server):
    client = NicotineClient(base_url=_url(server))
    state = client.wait_download(TRANSFER_ID)
    assert state["status"] == "Finished"
    assert state["path"].endswith("Song.mp3")


def test_wait_download_timeout(server):
    FakeNicotineBridge.transfer_state = {"status": "Queued", "size": 1, "path": None}
    try:
        client = NicotineClient(base_url=_url(server))
        state = client.wait_download(TRANSFER_ID, timeout_s=3)
        assert state["status"] == "Timeout"
    finally:
        FakeNicotineBridge.transfer_state = {"status": "Finished", "size": 11000000,
                                             "path": "/tmp/nicotine/abc123/Song.mp3"}


def test_error_when_bridge_down():
    client = NicotineClient(base_url="http://127.0.0.1:1")
    assert client.ping() is None
    with pytest.raises(NicotineError):
        client.search("x")


def test_source_search_and_download(server, tmp_path):
    src = NicotineSource(
        client=NicotineClient(base_url=_url(server), search_timeout_s=1),
        download_dir=tmp_path / "dl",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )
    track = Track(spotify_id="t1", name="Song", artist_names=["Band"], duration_ms=200_000)
    cands = src.search_track(track)
    assert cands[0].bitrate == 320
    assert cands[0].extra["username"] == "peer1"

    finished = tmp_path / "dl" / "abc123" / "Song.mp3"
    finished.parent.mkdir(parents=True)
    finished.write_bytes(b"fake-audio")
    staging = tmp_path / "staging"
    staging.mkdir(parents=True)
    FakeNicotineBridge.transfer_state = {"status": "Finished", "size": 11000000,
                                         "path": str(finished)}
    try:
        dest = src.download_track(cands[0], staging)
    finally:
        FakeNicotineBridge.transfer_state = {"status": "Finished", "size": 11000000,
                                             "path": "/tmp/nicotine/abc123/Song.mp3"}
    assert dest is not None and dest.name == "Song.mp3"
    assert not finished.exists()


def test_source_download_failure_returns_none(server, tmp_path):
    FakeNicotineBridge.transfer_state = {"status": "User logged off", "size": 1, "path": None}
    try:
        src = NicotineSource(
            client=NicotineClient(base_url=_url(server)),
            download_dir=tmp_path / "dl",
            data_dir=tmp_path / "data",
            log_dir=tmp_path / "logs",
        )
        cands = src.search_track(Track(spotify_id="t2", name="Song", artist_names=["Band"]))
        assert src.download_track(cands[0], tmp_path / "staging") is None
    finally:
        FakeNicotineBridge.transfer_state = {"status": "Finished", "size": 11000000,
                                             "path": "/tmp/nicotine/abc123/Song.mp3"}
