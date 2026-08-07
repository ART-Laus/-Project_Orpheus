"""Тесты парсеров RuTracker и клиента qBittorrent (на фейковом HTTP)."""

import http.server
import threading
import urllib.parse
from pathlib import Path

from orpheus.qbit_client import QbitClient
from orpheus.sources.rutracker import (
    RutrackerClient,
    RutrackerSource,
    format_from_title,
    parse_login_form,
    parse_search_page,
    parse_size,
    parse_topic_magnet,
)

SEARCH_HTML = """\
<html><body>
<table>
<tr class="tCenter hl-tr">
  <td><a class="fTLink" href="forum/viewforum.php?f=102">Потерянное качество</a></td>
  <td><a class="med tLink hl-tags bold" href="viewtopic.php?t=1000001">Artist - Album [FLAC] [Lossless]</a></td>
  <td class="tor-size">1.5 GB</td>
  <td class="seedmed"><b>42</b></td>
  <td class="leechmed"><b>3</b></td>
</tr>
<tr class="tCenter hl-tr">
  <td><a class="fTLink" href="forum/viewforum.php?f=103">MP3</a></td>
  <td><a class="med tLink hl-tags" href="viewtopic.php?t=1000002">Artist - Album [MP3 320 kbps]</a></td>
  <td class="tor-size">320 MB</td>
  <td class="seed"><b>17</b></td>
  <td class="leech"><b>0</b></td>
</tr>
<tr class="tCenter hl-tr">
  <td><a class="fTLink" href="forum/viewforum.php?f=103">MP3</a></td>
  <td><a class="med tLink hl-tags" href="viewtopic.php?t=1000003">Artist - Album [MP3 V0]</a></td>
  <td class="tor-size">250 MB</td>
  <td class="seedmed"><b>0</b></td>
</tr>
</table>
</body></html>"""

TOPIC_HTML = """\
<html><body>
<a class="magnet-link" href="magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01&amp;tr=http%3A%2F%2Frutracker.org%2Fann%3Fpk%3Dx">Скачать</a>
</body></html>"""

LOGIN_HTML = """\
<html><body>
<form method="post">
<input type="hidden" name="bb_session" value="session123" />
<input type="hidden" name="cap_sid" value="c4ptcha_sid" />
<input type="text" name="login_username" />
<input type="password" name="login_password" />
</form>
</body></html>"""

LOGIN_HTML_NO_CAPTCHA = """\
<html><body>
<form method="post">
<input type="hidden" name="bb_session" value="session123" />
<input type="text" name="login_username" />
<input type="password" name="login_password" />
</form>
</body></html>"""


def test_parse_size():
    assert parse_size("1.5 GB") == int(1.5 * 1024**3)
    assert parse_size("320 MB") == 320 * 1024**2
    assert parse_size("—") == 0


def test_format_from_title():
    assert format_from_title("Album [FLAC] [Lossless]") == (".flac", 0)
    assert format_from_title("Album [MP3 320 kbps]") == (".mp3", 320)
    assert format_from_title("Album [MP3 V0]") == (".mp3", 0)
    assert format_from_title("Album [AAC 320]") == (".m4a", 320)


def test_parse_search_page():
    topics = parse_search_page(SEARCH_HTML)
    assert len(topics) == 3
    t1, t2, t3 = topics
    assert t1.topic_id == 1000001
    assert t1.title == "Artist - Album [FLAC] [Lossless]"
    assert t1.extension == ".flac"
    assert t1.seeders == 42
    assert t1.size_bytes == int(1.5 * 1024**3)
    assert t2.extension == ".mp3" and t2.bitrate == 320
    assert t3.bitrate == 0 and t3.seeders == 0


def test_parse_topic_magnet():
    magnet = parse_topic_magnet(TOPIC_HTML)
    assert magnet and "xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01" in magnet
    assert "&" in magnet


def test_parse_login_form():
    form = parse_login_form(LOGIN_HTML)
    assert form == {"bb_session": "session123", "cap_sid": "c4ptcha_sid"}


class FakeRutrackerServer(http.server.BaseHTTPRequestHandler):
    """Мини-сервер, имитирующий rutracker: login, index, search, viewtopic."""

    logged_in = False
    require_captcha = False

    def do_GET(self):
        if self.path.startswith("/forum/index.php"):
            body = ("Выход" if self.logged_in else "login_username").encode("cp1251")
        elif self.path.startswith("/forum/login.php"):
            body = (LOGIN_HTML if self.require_captcha else LOGIN_HTML_NO_CAPTCHA).encode("cp1251")
        elif self.path.startswith("/forum/tracker.php"):
            body = SEARCH_HTML.encode("cp1251")
        elif self.path.startswith("/forum/viewtopic.php"):
            body = TOPIC_HTML.encode("cp1251")
        else:
            body = b""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=windows-1251")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        data = urllib.parse.parse_qs(self.rfile.read(length).decode("cp1251"))
        if data.get("login_username") and data.get("login_password"):
            if self.require_captcha and not data.get("cap_code"):
                body = "неверный код с картинки".encode("cp1251")
            else:
                type(self).logged_in = (
                    data["login_username"][0] == "user" and data["login_password"][0] == "pass"
                )
                body = b""
        else:
            body = b""
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _serve():
    server = http.server.HTTPServer(("127.0.0.1", 0), FakeRutrackerServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def _client(port, cache):
    return RutrackerClient(base_url=f"http://127.0.0.1:{port}/forum", cache_dir=cache)


def test_client_login_flow(tmp_path):
    server, port = _serve()
    try:
        client = _client(port, tmp_path)
        assert client.login("user", "pass") == "ok"
        assert client.is_logged_in()
        assert (tmp_path / "rutracker_session.txt").exists()

        topics = client.search("Artist Album")
        assert len(topics) == 3
        assert client.magnet(1000001) is not None
    finally:
        server.shutdown()


def test_client_login_captcha(tmp_path):
    server, port = _serve()
    try:
        FakeRutrackerServer.require_captcha = True
        FakeRutrackerServer.logged_in = False
        client = _client(port, tmp_path)
        assert client.login("user", "pass") == "captcha"
        assert (tmp_path / "rutracker_captcha.jpg").exists()
        assert client.login("user", "pass", captcha_code="1234") == "ok"
        assert client.is_logged_in()
    finally:
        FakeRutrackerServer.require_captcha = False
        server.shutdown()


def test_source_search_album_filters_by_quality(tmp_path):
    server, port = _serve()
    try:
        from orpheus.models import Album
        from orpheus.qbit_client import QbitClient

        FakeRutrackerServer.logged_in = True
        src = RutrackerSource(
            client=_client(port, tmp_path),
            qbit=QbitClient(),
            torrents_dir=tmp_path / "torrents",
            min_quality="flac",
        )
        cands = src.search_album(Album(spotify_id="x" * 22, name="Album", artist_names=["Artist"]))
        assert len(cands) == 1
        assert cands[0].extension == ".flac"
        assert cands[0].extra["topic_id"] == 1000001
    finally:
        server.shutdown()


def test_qbit_client_login_and_add():
    """QbitClient на фейковом HTTP: минимальная проверка путей запросов."""

    class FakeQbit(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            data = urllib.parse.parse_qs(self.rfile.read(length).decode())
            if self.path == "/api/v2/auth/login":
                body = b"Ok."
            elif self.path == "/api/v2/torrents/add":
                self.server.added = data
                body = b"Ok."
            elif self.path == "/api/v2/torrents/delete":
                body = b"Ok."
            else:
                body = b""
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/api/v2/torrents/info"):
                body = (
                    b'[{"hash":"h1","name":"x","progress":1.0,"state":"uploading",'
                    b'"save_path":"/srv/torrents/t1","magnet_uri":"magnet:?xt=urn:btih:h1"}]'
                )
            elif self.path.startswith("/api/v2/torrents/files"):
                body = b'[{"name":"Album/a.mp3","size":100},{"name":"Album/b.mp3","size":200}]'
            else:
                body = b"[]"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    qserver = http.server.HTTPServer(("127.0.0.1", 0), FakeQbit)
    qthread = threading.Thread(target=qserver.serve_forever, daemon=True)
    qthread.start()
    try:
        qbit = QbitClient(base_url=f"http://127.0.0.1:{qserver.server_address[1]}", username="admin", password="p")
        assert qbit.login() is True
        h = qbit.add_torrent("magnet:?xt=urn:btih:h1", "/srv/torrents/t1")
        assert h == "h1"
        t = qbit.wait_complete("h1", timeout_s=10)
        assert t["progress"] == 1.0
        assert qbit.content_dir(t) == "/srv/torrents/t1/Album"
    finally:
        qserver.shutdown()
