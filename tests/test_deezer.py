"""Тесты клиента и источника Deezer (на фейковом HTTP-сервере).

Публичный API (api.deezer.com) + gw-light + media API + CDN эмулируются
одним сервером.
"""

import http.server
import json
import threading
import urllib.parse
from pathlib import Path

import pytest

from orpheus.deezer_client import DeezerClient
from orpheus.models import Album, Track
from orpheus.sources.base import SourceError
from orpheus.sources.deezer import DeezerSource

TRACKS = [
    {
        "id": "111",
        "title": "Around the World",
        "duration": 204,
        "artist": {"id": "a1", "name": "Daft Punk"},
        "album": {"id": "A1", "title": "Homework"},
    },
    {  # другая длительность — должен отсечься фильтром
        "id": "333",
        "title": "Around the World",
        "duration": 180,
        "artist": {"id": "a1", "name": "Daft Punk"},
        "album": {"id": "A2", "title": "Homework (Live)"},
    },
    {  # нет id — пропускается
        "id": "",
        "title": "Around the World",
        "duration": 204,
        "artist": {"id": "a1", "name": "Daft Punk"},
        "album": {"id": "A1", "title": "Homework"},
    },
]

ALBUMS = [
    {
        "id": "A1",
        "title": "Homework",
        "artist": {"id": "a1", "name": "Daft Punk"},
    },
    {  # не тот исполнитель
        "id": "A2",
        "title": "Homework",
        "artist": {"id": "a9", "name": "Cover Band"},
    },
]

ALBUM_TRACKS = {
    "A1": [
        {"id": "501", "title": "Daftendirekt", "duration": 157, "disk_number": 1, "track_position": 1},
        {"id": "502", "title": "WDPK 83.7 FM", "duration": 28, "disk_number": 2, "track_position": 5},
    ]
}

# Какие форматы доступны «аккаунту» (web_hq/web_lossless)
ACCOUNT_OPTIONS = {"web_hq": True, "web_lossless": True, "license_token": "lic-1"}

# track_id -> данные из song.getData
TRACK_DATA = {
    "111": {"TRACK_TOKEN": "tok-111", "FILESIZE_MP3_320": "1000", "FILESIZE_FLAC": "2000"},
    "501": {"TRACK_TOKEN": "tok-501", "FILESIZE_MP3_320": "1000"},
    "502": {"TRACK_TOKEN": "tok-502", "FILESIZE_MP3_320": "1000"},
}

# media API: token -> url
MEDIA_URLS = {
    "tok-111": "http://127.0.0.1:{port}/cdn/111.mp3",
    "tok-501": "http://127.0.0.1:{port}/cdn/501.mp3",
    "tok-502": "http://127.0.0.1:{port}/cdn/502.mp3",
}

FILE_BYTES = b"ID3\x04\x00\x00\x00\x00\x00\x00fake-mp3-bytes-for-tests"


class FakeDeezerServer(http.server.BaseHTTPRequestHandler):
    """Мини-сервер: публичный API + gw-light + media API + CDN."""

    valid_arl = "test-arl"

    def _cookie_ok(self) -> bool:
        cookie = self.headers.get("Cookie") or ""
        return f"arl={self.valid_arl}" in cookie

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def _media(self, media_id):
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.end_headers()
        self.wfile.write(FILE_BYTES)

    def do_POST(self):
        if self.path.startswith("/media-api"):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            token = (body.get("track_tokens") or [""])[0]
            url = MEDIA_URLS.get(token)
            if not url:
                self._json({"error": "no source"}, code=404)
                return
            self._json(
                {
                    "data": [
                        {
                            "media": [
                                {
                                    "sources": [
                                        {"url": url.format(port=self.server.server_address[1])}
                                    ]
                                }
                            ]
                        }
                    ]
                }
            )
            return
        if not self.path.startswith("/gw-light"):
            self._json({"error": "not found"}, code=404)
            return
        if not self._cookie_ok():
            self._json({"error": ["INVALID_SESSION"]})
            return
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        method = (query.get("method") or [""])[0]
        if method == "deezer.getUserData":
            self._json(
                {
                    "results": {
                        "USER": {
                            "USER_ID": 42,
                            "LOGIN": "tester",
                            "COUNTRY": "US",
                            "OPTIONS": ACCOUNT_OPTIONS,
                        },
                        "checkForm": "check-abc",
                        "SESSION_ID": "sess-1",
                        "OFFER_NAME": "Deezer Premium",
                    }
                }
            )
        elif method == "song.getData":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            if isinstance(body, list):
                body = body[0] if body else {}
            sng_id = str(body.get("SNG_ID", ""))
            data = TRACK_DATA.get(sng_id)
            if not data:
                self._json({"error": ["DATA_ERROR", "unknown track"]})
                return
            self._json({"results": data})
        else:
            self._json({"results": {"ERROR": f"unknown method {method}"}})

    def do_GET(self):
        if self.path.startswith("/cdn/"):
            self._media(self.path.split("/")[-1])
            return
        path = urllib.parse.urlsplit(self.path).path
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        if path == "/search":
            self._json({"data": TRACKS})
        elif path == "/search/album":
            self._json({"data": ALBUMS})
        elif path.startswith("/album/"):
            album_id = path.split("/")[-1]
            self._json({"id": album_id, "title": "Homework", "tracks": {"data": ALBUM_TRACKS.get(album_id, [])}})
        else:
            self._json({"error": {"code": 404, "message": "not found"}}, code=404)

    def log_message(self, *args):
        pass


def _serve():
    server = http.server.HTTPServer(("127.0.0.1", 0), FakeDeezerServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


@pytest.fixture()
def deezer():
    server, port = _serve()
    yield (
        DeezerClient(
            arl="test-arl",
            public_base=f"http://127.0.0.1:{port}",
            gw_base=f"http://127.0.0.1:{port}/gw-light",
            media_api=f"http://127.0.0.1:{port}/media-api",
            request_interval=0,
            stream_interval=0,
        ),
        DeezerSource(
            arl="test-arl",
            request_interval=0,
            stream_interval=0,
        ),
        port,
    )
    server.shutdown()


def _patch_source(client, source):
    source.client = client


def _track(**overrides):
    values = dict(
        spotify_id="x" * 22,
        name="Around the World",
        artist_names=["Daft Punk"],
        duration_ms=204000,
    )
    values.update(overrides)
    return Track(**values)


def _album(**overrides):
    values = dict(
        spotify_id="a" * 22,
        name="Homework",
        artist_names=["Daft Punk"],
        track_ids=["501", "502"],
    )
    values.update(overrides)
    return Album(**values)


def test_available_requires_arl():
    assert not DeezerSource().available()


def test_account_info_and_search(deezer):
    client, _source, _port = deezer
    info = client.account_info()
    assert info["user_id"] == 42 and info["login"] == "tester"
    assert "FLAC" in info["formats"] and "MP3_320" in info["formats"]
    results = client.search("Daft Punk Around the World")
    assert results and results[0]["id"] == "111"


def test_search_track_filters_duration(deezer):
    client, source, _port = deezer
    _patch_source(client, source)
    cands = source.search_track(_track())
    assert len(cands) == 1
    assert cands[0].extra["track_id"] == "111"
    assert cands[0].bitrate == 320 and cands[0].extension == ".mp3"


def test_download_track_picks_best_quality(deezer, tmp_path):
    client, source, _port = deezer
    _patch_source(client, source)
    cands = source.search_track(_track())
    dest = source.download_track(cands[0], tmp_path)
    assert dest is not None and dest.read_bytes() == FILE_BYTES


def test_search_album_matches_title_and_artist(deezer):
    client, source, _port = deezer
    _patch_source(client, source)
    cands = source.search_album(_album())
    assert len(cands) == 1
    assert cands[0].extra["album_id"] == "A1"


def test_download_album_numbering_across_discs(deezer, tmp_path):
    client, source, _port = deezer
    _patch_source(client, source)
    cands = source.search_album(_album())
    dest_dir = source.download_album(cands[0], tmp_path)
    assert dest_dir is not None
    names = sorted(p.name for p in dest_dir.iterdir())
    assert names == ["01. Daftendirekt.mp3", "06. WDPK 83.7 FM.mp3"]


def test_download_album_parallel_matches_sequential(deezer, tmp_path):
    client, source, _port = deezer
    _patch_source(client, source)
    cands = source.search_album(_album())
    parallel = DeezerSource(
        arl="test-arl",
        request_interval=0,
        stream_interval=0,
        parallel=4,
    )
    _patch_source(client, parallel)
    dest_dir = parallel.download_album(cands[0], tmp_path / "par")
    assert dest_dir is not None
    names = sorted(p.name for p in dest_dir.iterdir())
    assert names == ["01. Daftendirekt.mp3", "06. WDPK 83.7 FM.mp3"]
    for p in dest_dir.iterdir():
        assert p.read_bytes() == FILE_BYTES


def test_gw_invalid_arl():
    server, port = _serve()
    try:
        client = DeezerClient(
            arl="bad-arl",
            public_base=f"http://127.0.0.1:{port}",
            gw_base=f"http://127.0.0.1:{port}/gw-light",
            media_api=f"http://127.0.0.1:{port}/media-api",
            request_interval=0,
            stream_interval=0,
        )
        with pytest.raises(SourceError):
            client.account_info()
    finally:
        server.shutdown()


def test_track_media_returns_flac_when_requested(deezer):
    client, _source, _port = deezer
    media = client.track_media(111, prefer_flac=True)
    assert media is not None and media["format"] == "FLAC"
    assert media["track_token"] == "tok-111"
    assert client.media_extension(media) == ".flac"


def test_stream_url_goes_through_media_api(deezer):
    client, _source, _port = deezer
    media = client.track_media(111)
    assert media is not None
    url = client.stream_url(media)
    assert "/cdn/" in url and "111.mp3" in url


def test_stream_url_requires_token():
    client = DeezerClient(arl="x")
    with pytest.raises(SourceError):
        client.stream_url({"format": "MP3_320"})
