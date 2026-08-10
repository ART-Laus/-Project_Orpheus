"""Источник RuTracker: поиск релизов и скачивание торрентами через qBittorrent.

Клиент работает по HTTP-сессии (куки в data/cache/rutracker_session.txt);
при логине может потребоваться капча — картинка сохраняется в файл,
код вводится пользователем один раз (CLI: orpheus rutracker login).

Парсинг:
  - login.php — форма входа + скрытые поля (bb_session, cap_sid);
  - tracker.php?nm=... — страница поиска: темы с тегами формата [FLAC], [MP3 320];
  - viewtopic.php?t=... — magnet-ссылка релиза.
"""

from __future__ import annotations

import html
import http.cookiejar
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ..models import Album
from ..qbit_client import QbitClient
from .base import Candidate, MusicSource, SourceError

SESSION_COOKIE_FILE = "rutracker_session.txt"
CAPTCHA_IMAGE = "rutracker_captcha.jpg"
LOGIN_VALUE = "%C2%F5%EE%E4"  # "Вход" в кодировке страницы (cp1251)

_SIZE_RE = re.compile(r"([\d.,]+)\s*(TB|GB|MB|KB)", re.I)
_SEEDERS_RE = re.compile(r">\s*(\d+)\s*<")
_FORMAT_TAG_RE = re.compile(r"\[([^\]]{2,20})\]")


@dataclass
class Topic:
    """Одна тема-релиз на странице поиска."""

    topic_id: int
    title: str
    forum: str = ""
    size_bytes: int = 0
    seeders: int = 0
    extension: str = ".mp3"
    bitrate: int = 0

    def to_candidate(self, source_name: str) -> Candidate:
        return Candidate(
            source=source_name,
            filename=f"{self.forum} - {self.title}",
            size=self.size_bytes,
            bitrate=self.bitrate,
            extension=self.extension,
            extra={"topic_id": self.topic_id},
        )


def format_from_title(title: str) -> tuple[str, int]:
    """(расширение, битрейт) по тегам формата в названии темы."""
    low = title.lower()
    if "[flac" in low or "[lossless" in low or "[ape]" in low or "[wavpack" in low:
        return ".flac", 0
    if "[aac" in low or "[m4a" in low or "[alac" in low:
        return ".m4a", 320
    if "[ogg" in low or "[opus" in low:
        return ".ogg", 320
    m = re.search(r"\[mp3\s*v?\s*(\d{3})", low)
    if m:
        return ".mp3", int(m.group(1))
    if "[mp3" in low:
        return ".mp3", 0
    return ".mp3", 0


def parse_size(text: str) -> int:
    m = _SIZE_RE.search(text or "")
    if not m:
        return 0
    value = float(m.group(1).replace(",", "."))
    mult = {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}[m.group(2).upper()]
    return int(value * mult)


def parse_search_page(html_text: str) -> list[Topic]:
    """Разбор страницы поиска tracker.php: строки таблицы с темами."""
    topics: list[Topic] = []
    rows = re.split(r"<tr[^>]*class=\"tCenter[^\"]*\"[^>]*>", html_text)
    for row in rows[1:]:
        m = re.search(r'viewtopic\.php\?t=(\d+)[^"]*"[^>]*>(.*?)</a>', row, re.S)
        if not m:
            continue
        topic_id = int(m.group(1))
        title = re.sub(r"<[^>]+>", "", m.group(2))
        title = html.unescape(title.strip())
        if not title:
            continue
        seed_match = re.search(r'class="[^"]*seed[^"]*"[^>]*>(?:<[^>]+>)*\s*(\d+)', row)
        seeders = int(seed_match.group(1)) if seed_match else 0
        ext, bitrate = format_from_title(title)
        topics.append(
            Topic(
                topic_id=topic_id,
                title=title,
                size_bytes=parse_size(row),
                seeders=seeders,
                extension=ext,
                bitrate=bitrate,
            )
        )
    return topics


def parse_topic_magnet(html_text: str) -> str | None:
    """magnet-ссылка со страницы релиза (первая попавшаяся btih)."""
    m = re.search(r'href="(magnet:\?xt=urn:btih:[a-fA-F0-9]+[^"]*)"', html_text)
    if not m:
        return None
    return html.unescape(m.group(1))


def parse_login_form(html_text: str) -> dict:
    """Скрытые поля формы входа: bb_session, cap_sid."""
    out: dict[str, str] = {}
    for field in ("bb_session", "cap_sid"):
        m = re.search(r'name="%s"\s+value="([^"]*)"' % field, html_text)
        if m:
            out[field] = html.unescape(m.group(1))
    return out


class CaptchaRequired(SourceError):
    """Для входа нужна капча: файл с картинкой сохранён в image_path."""

    def __init__(self, image_path: Path):
        super().__init__(f"требуется капча: открой {image_path} и введи код")
        self.image_path = image_path


class RutrackerClient:
    """HTTP-клиент форума rutracker с сохранением сессии."""

    def __init__(
        self,
        base_url: str = "https://rutracker.org/forum",
        cache_dir: Path | None = None,
        timeout_s: int = 20,
        proxy: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.cache_dir = cache_dir
        self.timeout_s = timeout_s
        self.proxy = proxy
        self.jar = http.cookiejar.LWPCookieJar()
        handlers: list = [urllib.request.HTTPCookieProcessor(self.jar)]
        if proxy and proxy.startswith(("http://", "https://")):
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        self._opener = urllib.request.build_opener(*handlers)
        if cache_dir and (cache_dir / SESSION_COOKIE_FILE).exists():
            try:
                self.jar.load(cache_dir / SESSION_COOKIE_FILE)
            except Exception:
                pass

    def _save_session(self) -> None:
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            try:
                self.jar.save(self.cache_dir / SESSION_COOKIE_FILE)
            except Exception:
                pass

    def _open(self, req: urllib.request.Request) -> str:
        """Открыть URL, вернуть HTML. SOCKS-прокси (socks5/socks5h) — через
        PySocks: Cloudflare не челленджит домашние IP, туннель на Windows
        (ssh -D / tailscale) даёт «домашний» выход для rutracker."""
        if self.proxy and self.proxy.startswith("socks"):
            import socket

            import socks

            parts = urllib.parse.urlsplit(self.proxy)
            socks.set_default_proxy(
                socks.SOCKS5, parts.hostname or "127.0.0.1", parts.port or 1080, rdns=True
            )
            original = socket.socket
            socket.socket = socks.socksocket  # type: ignore[assignment]
            try:
                with self._opener.open(req, timeout=self.timeout_s) as resp:
                    return resp.read().decode("cp1251", errors="replace")
            finally:
                socket.socket = original
                socks.set_default_proxy(None)
        with self._opener.open(req, timeout=self.timeout_s) as resp:
            return resp.read().decode("cp1251", errors="replace")

    def _get(self, path: str) -> str:
        try:
            return self._open(
                urllib.request.Request(
                    self.base_url + path, headers={"User-Agent": "Mozilla/5.0"}
                )
            )
        except urllib.error.URLError as exc:
            raise SourceError(f"rutracker: {exc.reason} ({self.base_url + path})") from exc

    def _post(self, path: str, data: dict[str, str]) -> str:
        body = urllib.parse.urlencode(data).encode()
        try:
            return self._open(
                urllib.request.Request(
                    self.base_url + path,
                    data=body,
                    headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/x-www-form-urlencoded"},
                )
            )
        except urllib.error.URLError as exc:
            raise SourceError(f"rutracker: {exc.reason}") from exc

    def is_logged_in(self) -> bool:
        page = self._get("/index.php")
        return "Выход" in page or "logout" in page.lower()

    def login(self, username: str, password: str, captcha_code: str | None = None) -> str:
        """Вход на форум. Возвращает "ok", "captcha" или "failed".

        При "captcha" картинка сохранена в data/cache/rutracker_captcha.jpg —
        нужно повторить login() с введённым кодом.
        """
        form = parse_login_form(self._get("/login.php"))
        data: dict[str, str] = {
            "login_username": username,
            "login_password": password,
            "login": LOGIN_VALUE,
        }
        if form.get("bb_session"):
            data["bb_session"] = form["bb_session"]
        if captcha_code is not None:
            data["cap_sid"] = form.get("cap_sid", "")
            data["cap_code"] = captcha_code
        elif form.get("cap_sid"):
            if self.cache_dir:
                image = self.cache_dir / CAPTCHA_IMAGE
                image.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with self._opener.open(
                        urllib.request.Request(
                            self.base_url + "/captcha.php?action=get&cap_sid=" + urllib.parse.quote(form["cap_sid"]),
                            headers={"User-Agent": "Mozilla/5.0"},
                        ),
                        timeout=self.timeout_s,
                    ) as resp:
                        image.write_bytes(resp.read())
                except urllib.error.URLError:
                    pass
                self._save_session()
                return "captcha"
            return "captcha"
        page = self._post("/login.php", data)
        if self.is_logged_in():
            self._save_session()
            return "ok"
        if "капч" in page.lower() or "captcha" in page.lower() or "Код с картинки" in page:
            return "captcha"
        return "failed"

    def search(self, query: str) -> list[Topic]:
        if not self.is_logged_in():
            raise SourceError("rutracker: не выполнен вход (orpheus rutracker login)")
        page = self._get("/tracker.php?" + urllib.parse.urlencode({"nm": query}))
        return parse_search_page(page)

    def magnet(self, topic_id: int) -> str | None:
        page = self._get(f"/viewtopic.php?t={topic_id}")
        return parse_topic_magnet(page)


def _passes_quality(topic: Topic, min_quality: str) -> bool:
    """Фильтр релизов по минимальному качеству (flac / mp3_320 / any)."""
    if min_quality == "any":
        return True
    if topic.extension == ".flac":
        return True
    if min_quality == "mp3_320":
        return topic.extension == ".mp3" and topic.bitrate >= 320
    return False


class RutrackerSource(MusicSource):
    """Альбомный источник: релизы с RuTracker через qBittorrent."""

    name = "rutracker"
    album_capable = True

    def __init__(
        self,
        client: RutrackerClient,
        qbit: QbitClient,
        torrents_dir: Path,
        min_quality: str = "flac",
    ):
        self.client = client
        self.qbit = qbit
        self.torrents_dir = torrents_dir
        self.min_quality = min_quality

    def available(self) -> bool:
        try:
            return self.client.is_logged_in()
        except Exception:
            return False

    def search_album(self, album: Album) -> list[Candidate]:
        artist = " ".join(album.artist_names) if album.artist_names else ""
        query = f"{artist} {album.name}".strip()
        topics = self.client.search(query)
        cands = []
        for t in topics:
            if not _passes_quality(t, self.min_quality):
                continue
            if t.seeders <= 0:
                continue
            cands.append(t.to_candidate(self.name))
        return cands

    def download_album(self, cand: Candidate, dest_dir: Path) -> Path | None:
        topic_id = cand.extra.get("topic_id")
        magnet = self.client.magnet(topic_id)
        if not magnet:
            return None
        save_path = self.torrents_dir / f"t{topic_id}"
        save_path.mkdir(parents=True, exist_ok=True)
        try:
            torrent_hash = self.qbit.add_torrent(magnet, save_path)
            torrent = self.qbit.wait_complete(torrent_hash)
            folder = Path(self.qbit.content_dir(torrent))
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(f"rutracker: qBittorrent: {exc}") from exc
        return folder if folder.exists() else None
