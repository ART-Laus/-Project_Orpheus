"""Тесты клиента и источника Яндекс.Музыки (на фейковом HTTP-сервере)."""

import http.server
import json
import threading
import urllib.parse
from pathlib import Path

import pytest

from orpheus.models import Track
from orpheus.quality import QualityPolicy, filter_and_rank
from orpheus.sources.base import SourceError
from orpheus.sources.yandex import YandexSource
from orpheus.yandex_client import YandexClient

TRACKS = [
    {
        "id": "111",
        "title": "Into You",
        "durationMs": 204000,
        "available": True,
        "artists": [{"id": "a1", "name": "Ariana Grande"}],
    },
    {  # другая длительность — должен отсечься фильтром
        "id": "333",
        "title": "Into You",
        "durationMs": 180000,
        "available": True,
        "artists": [{"id": "a1", "name": "Ariana Grande"}],
    },
    {  # недоступен в регионе
        "id": "444",
        "title": "Into You",
        "durationMs": 204000,
        "available": False,
        "artists": [{"id": "a1", "name": "Ariana Grande"}],
    },
    {  # только aac 256 — допустимо, но уступает mp3 320
        "id": "222",
        "title": "Into You (Remix)",
        "durationMs": 204000,
        "available": True,
        "artists": [{"id": "a2", "name": "Some DJ"}],
    },
]

INFOS = {
    "111": [
        {"codec": "mp3", "bitrateInKbps": 128, "directLink": ""},
        {"codec": "mp3", "bitrateInKbps": 320, "directLink": "/get-file?id=111"},
        {"codec": "aac", "bitrateInKbps": 256, "directLink": ""},
    ],
    "222": [{"codec": "aac", "bitrateInKbps": 256, "directLink": "/get-file?id=222"}],
    "333": [{"codec": "mp3", "bitrateInKbps": 320, "directLink": "/get-file?id=333"}],
    "444": [{"codec": "mp3", "bitrateInKbps": 320, "directLink": "/get-file?id=444"}],
}

FILE_BYTES = b"ID3\x04\x00\x00\x00\x00\x00\x00fake-mp3-bytes-for-tests"

ALBUMS = [
    {
        "id": "A1",
        "title": "Hot Fuss",
        "artists": [{"id": "a3", "name": "The Killers"}],
        "trackCount": 2,
    },
    {  # не тот исполнитель
        "id": "A2",
        "title": "Hot Fuss",
        "artists": [{"id": "a4", "name": "Cover Band"}],
        "trackCount": 1,
    },
]

ALBUM_TRACKS = {
    "A1": [
        {
            "id": "501",
            "title": "Jenny Was a Friend of Mine",
            "durationMs": 244000,
            "trackNumber": 1,
            "available": True,
            "artists": [{"id": "a3", "name": "The Killers"}],
        },
        {
            "id": "502",
            "title": "Mr. Brightside",
            "durationMs": 223000,
            "trackNumber": 2,
            "available": True,
            "artists": [{"id": "a3", "name": "The Killers"}],
        },
    ]
}

INFOS["501"] = [{"codec": "mp3", "bitrateInKbps": 320, "directLink": "/get-file?id=501"}]
INFOS["502"] = [{"codec": "mp3", "bitrateInKbps": 320, "directLink": "/get-file?id=502"}]


class FakeYandexServer(http.server.BaseHTTPRequestHandler):
    """Мини-сервер, имитирующий api.music.yandex.net."""

    valid_token = "test-token"

    def _auth_ok(self) -> bool:
        return self.headers.get("Authorization") == f"OAuth {self.valid_token}"

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._auth_ok():
            self._json({"error": "forbidden"}, code=401)
            return
        if self.path.startswith("/account/status"):
            self._json(
                {
                    "account": {"uid": 123, "login": "testuser"},
                    "subscription": {"plus": {"HasPlus": True}},
                    "permissions": {},
                }
            )
        elif self.path.startswith("/search"):
            params = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if params.get("type") == ["album"]:
                self._json(
                    {
                        "result": {
                            "albums": {"results": ALBUMS, "totalResults": len(ALBUMS)}
                        }
                    }
                )
            else:
                self._json(
                    {
                        "search-result": {
                            "type": "track",
                            "page": 0,
                            "perPage": 10,
                            "totalResults": len(TRACKS),
                            "tracks": {"results": TRACKS, "totalResults": len(TRACKS)},
                        }
                    }
                )
        elif self.path.startswith("/albums/"):
            album_id = self.path.split("/")[2].split("?")[0]
            self._json(
                {
                    "result": {
                        "id": album_id,
                        "title": ALBUM_TRACKS[album_id][0]["title"] if album_id in ALBUM_TRACKS else "",
                        "volumes": [ALBUM_TRACKS.get(album_id, [])],
                    }
                }
            )
        elif self.path.startswith("/tracks/"):
            track_id = self.path.split("/")[2].split("?")[0]
            self._json(INFOS.get(track_id, []))
        elif self.path.startswith("/get-file"):
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.end_headers()
            self.wfile.write(FILE_BYTES)
        else:
            self._json({"error": "not found"}, code=404)

    def log_message(self, *args):
        pass


def _serve():
    server = http.server.HTTPServer(("127.0.0.1", 0), FakeYandexServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def _client(port, token="test-token", token_file=None):
    return YandexClient(base_url=f"http://127.0.0.1:{port}", token=token, token_file=token_file)


def _track(**overrides):
    values = dict(
        spotify_id="x" * 22,
        name="Into You",
        artist_names=["Ariana Grande"],
        duration_ms=204000,
    )
    values.update(overrides)
    return Track(**values)


def test_client_account_status():
    server, port = _serve()
    try:
        status = _client(port).account_status()
        assert status["account"]["login"] == "testuser"
        assert status["subscription"]["plus"]["HasPlus"] is True
    finally:
        server.shutdown()


def test_client_invalid_token():
    server, port = _serve()
    try:
        with pytest.raises(SourceError, match="недействителен"):
            _client(port, token="bad").account_status()
    finally:
        server.shutdown()


def test_client_no_token(tmp_path):
    server, port = _serve()
    try:
        with pytest.raises(SourceError, match="нет токена"):
            _client(port, token="").account_status()
        token_file = tmp_path / "tok.txt"
        token_file.write_text("test-token", encoding="utf-8")
        assert _client(port, token="", token_file=token_file).token == "test-token"
        assert _client(port, token="explicit", token_file=token_file).token == "explicit"
    finally:
        server.shutdown()


def test_client_search_and_best_info():
    server, port = _serve()
    try:
        client = _client(port)
        results = client.search("Ariana Grande Into You")
        assert [r["id"] for r in results] == ["111", "333", "444", "222"]

        best = client.best_download_info("111")
        assert best["bitrateInKbps"] == 320 and best["codec"] == "mp3"
        link = client.direct_link(best)
        assert link == f"http://127.0.0.1:{port}/get-file?id=111"

        assert client.best_download_info("999") is None
    finally:
        server.shutdown()


def test_client_download(tmp_path):
    server, port = _serve()
    try:
        client = _client(port)
        best = client.best_download_info("111")
        dest = client.download(client.direct_link(best), tmp_path / "out.mp3")
        assert dest.read_bytes() == FILE_BYTES
    finally:
        server.shutdown()


def test_source_search_track():
    server, port = _serve()
    try:
        src = YandexSource(token="test-token", base_url=f"http://127.0.0.1:{port}")
        cands = src.search_track(_track())
        ids = [c.extra["track_id"] for c in cands]
        assert ids == ["111", "222"]

        c111 = cands[0]
        assert c111.extension == ".mp3" and c111.bitrate == 320
        assert c111.length_s == 204
        assert c111.filename == "Ariana Grande - Into You"
        assert cands[1].extension == ".m4a" and cands[1].bitrate == 256

        ranked = filter_and_rank(cands, QualityPolicy(), 204000)
        # политика: нативный AAC/M4A (rank 3) выше MP3 (rank 2)
        assert [c.extra["track_id"] for c in ranked] == ["222", "111"]
    finally:
        server.shutdown()


def test_source_search_track_duration_filter():
    server, port = _serve()
    try:
        src = YandexSource(token="test-token", base_url=f"http://127.0.0.1:{port}")
        # 210000 c — вне допуска ±5с от 204000 (результатов нет)
        assert src.search_track(_track(duration_ms=210000)) == []
        # 180000 совпадает ровно с треком 333
        cands = src.search_track(_track(duration_ms=180000))
        assert [c.extra["track_id"] for c in cands] == ["333"]
    finally:
        server.shutdown()


def test_source_download_track(tmp_path):
    server, port = _serve()
    try:
        src = YandexSource(token="test-token", base_url=f"http://127.0.0.1:{port}")
        cands = src.search_track(_track())
        dest = tmp_path / "dl"
        path = src.download_track(cands[0], dest)
        assert path and path.exists()
        assert path.read_bytes() == FILE_BYTES
        assert path.suffix == ".mp3"
    finally:
        server.shutdown()


def test_source_available():
    server, port = _serve()
    try:
        base = f"http://127.0.0.1:{port}"
        assert YandexSource(token="test-token", base_url=base).available() is True
        assert YandexSource(token="bad", base_url=base).available() is False
        assert YandexSource(token="", base_url=base).available() is False
    finally:
        server.shutdown()


def test_client_album_with_tracks():
    server, port = _serve()
    try:
        album = _client(port).album_with_tracks("A1")
        assert [t["id"] for t in album["volumes"][0]] == ["501", "502"]
        unknown = _client(port).album_with_tracks("999")
        assert not any(unknown.get("volumes") or [])
    finally:
        server.shutdown()


def test_source_search_album():
    server, port = _serve()
    try:
        from orpheus.models import Album

        src = YandexSource(token="test-token", base_url=f"http://127.0.0.1:{port}")
        cands = src.search_album(
            Album(spotify_id="a" * 22, name="Hot Fuss", artist_names=["The Killers"])
        )
        assert [c.extra["album_id"] for c in cands] == ["A1"]
        assert cands[0].source == "yandex"

        other = src.search_album(
            Album(spotify_id="a" * 22, name="Hot Fuss", artist_names=["No Such Band"])
        )
        assert other == []
    finally:
        server.shutdown()


def test_source_download_album(tmp_path):
    server, port = _serve()
    try:
        from orpheus.models import Album

        src = YandexSource(token="test-token", base_url=f"http://127.0.0.1:{port}")
        cands = src.search_album(
            Album(spotify_id="a" * 22, name="Hot Fuss", artist_names=["The Killers"])
        )
        dest = tmp_path / "album"
        folder = src.download_album(cands[0], dest)
        assert folder and folder.exists()
        files = sorted(p.name for p in folder.iterdir())
        assert files == ["01. Jenny Was a Friend of Mine.mp3", "02. Mr. Brightside.mp3"]
        assert (folder / files[0]).read_bytes() == FILE_BYTES
    finally:
        server.shutdown()
