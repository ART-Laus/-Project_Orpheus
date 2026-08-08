"""Bandcamp: поиск по каталогу и загрузка треков/альбомов.

API bandcamp: страница /search?q=... отдаёт HTML, из которого берём ссылки
на артистов и релизы; страница релиза содержит data-tralbum JSON с
дорожеками и прямыми URL на mp3 (128 kbps preview) или полным
файлом, если продавец разрешил бесплатное скачивание (free download).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ..models import Album, Track
from .base import Candidate, MusicSource, SourceError

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
BASE = "https://bandcamp.com"
_TRALBUM_RE = re.compile(r'data-tralbum="([^"]+)"', re.S)
_TRACK_URL_RE = re.compile(r"https?://[a-z0-9.-]+\.bandcamp\.com/track/([a-z0-9-]+)", re.I)
_ALBUM_URL_RE = re.compile(r"https?://[a-z0-9.-]+\.bandcamp\.com/album/([a-z0-9-]+)", re.I)


class BandcampClient:
    def __init__(self, base_url: str = BASE, timeout_s: int = 20):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def _fetch(self, path: str) -> str:
        try:
            with urllib.request.urlopen(
                urllib.request.Request(self.base_url + path, headers={"User-Agent": UA}),
                timeout=self.timeout_s,
            ) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                raise SourceError("bandcamp: заблокировано антиботом (403)") from exc
            raise SourceError(f"bandcamp: HTTP {exc.code} {path}") from exc
        except urllib.error.URLError as exc:
            raise SourceError(f"bandcamp: {exc.reason}") from exc

    def search(self, query: str) -> list[dict]:
        """Поиск: возвращает список {type, artist, title, url}."""
        page = self._fetch("/search?" + urllib.parse.urlencode({"q": query, "item_type": "t"}))
        out: list[dict] = []
        for m in _TRACK_URL_RE.finditer(page):
            out.append({"type": "track", "title": m.group(1), "url": m.group(0)})
        for m in _ALBUM_URL_RE.finditer(page):
            out.append({"type": "album", "title": m.group(1), "url": m.group(0)})
        # дедупликация с сохранением порядка
        seen: set[str] = set()
        uniq = []
        for item in out:
            if item["url"] not in seen:
                seen.add(item["url"])
                uniq.append(item)
        return uniq

    def track_data(self, url: str) -> dict | None:
        """Данные трека: {title, artist, duration_s, mp3, free}."""
        m = re.match(r"^(https?://[^/]+)(.*)$", url)
        if not m:
            return None
        host, path = m.groups()
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": UA}), timeout=self.timeout_s
            ) as resp:
                page = resp.read().decode("utf-8", errors="replace")
        except urllib.error.URLError:
            return None
        m = _TRALBUM_RE.search(page)
        if not m:
            return None
        try:
            data = json.loads(html_unescape(m.group(1)))
        except Exception:
            return None
        return _parse_tralbum(data)

    def album_data(self, url: str) -> dict | None:
        """Данные альбома: {artist, title, tracks: [{title, mp3, duration_s}]}."""
        m = re.match(r"^(https?://[^/]+)(.*)$", url)
        if not m:
            return None
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": UA}), timeout=self.timeout_s
            ) as resp:
                page = resp.read().decode("utf-8", errors="replace")
        except urllib.error.URLError:
            return None
        m = _TRALBUM_RE.search(page)
        if not m:
            return None
        try:
            data = json.loads(html_unescape(m.group(1)))
        except Exception:
            return None
        artist = (data.get("artist") or "").strip()
        title = (data.get("current", {}).get("title") or "").strip()
        tracks = []
        for t in data.get("trackinfo", []):
            if not isinstance(t, dict):
                continue
            file_info = t.get("file") or {}
            mp3 = file_info.get("mp3-128") or file_info.get("mp3-320") or ""
            duration = int((t.get("duration") or 0) * 1000)
            tracks.append(
                {"title": (t.get("title") or "").strip(), "mp3": mp3, "duration_ms": duration}
            )
        return {"artist": artist, "title": title, "tracks": tracks}


def html_unescape(text: str) -> str:
    import html

    return html.unescape(text)


def _parse_tralbum(data: dict) -> dict | None:
    artist = (data.get("artist") or "").strip()
    title = (data.get("current", {}).get("title") or "").strip()
    t = (data.get("trackinfo") or [{}])[0]
    file_info = t.get("file") or {}
    mp3 = file_info.get("mp3-128") or file_info.get("mp3-320") or ""
    duration = int((t.get("duration") or 0) * 1000)
    return {"artist": artist, "title": title, "mp3": mp3, "duration_ms": duration}


class BandcampSource(MusicSource):
    """Источник Bandcamp: треки и альбомы с прямыми mp3-ссылками."""

    name = "bandcamp"
    album_capable = True

    def __init__(self, client: BandcampClient | None = None):
        self.client = client or BandcampClient()

    def available(self) -> bool:
        try:
            self.client._fetch("/")
            return True
        except Exception:
            return False

    def search_track(self, track: Track) -> list[Candidate]:
        query = " ".join(track.artist_names + [track.name]).strip()
        cands = []
        for item in self.client.search(query):
            if item["type"] != "track":
                continue
            cands.append(
                Candidate(
                    source=self.name,
                    filename=item["title"],
                    extra={"url": item["url"]},
                )
            )
        return cands

    def download_track(self, cand: Candidate, dest_dir: Path) -> Path | None:
        data = self.client.track_data(cand.extra.get("url") or "")
        if not data or not data.get("mp3"):
            return None
        return _download_mp3(data["mp3"], dest_dir, cand.extra.get("url", "track"))

    def search_album(self, album: Album) -> list[Candidate]:
        artist = " ".join(album.artist_names) if album.artist_names else ""
        query = f"{artist} {album.name}".strip()
        cands = []
        for item in self.client.search(query):
            if item["type"] != "album":
                continue
            cands.append(
                Candidate(
                    source=self.name,
                    filename=item["title"],
                    extra={"url": item["url"]},
                )
            )
        return cands

    def download_album(self, cand: Candidate, dest_dir: Path) -> Path | None:
        data = self.client.album_data(cand.extra.get("url") or "")
        if not data or not data.get("tracks"):
            return None
        folder = dest_dir / "bandcamp-album"
        folder.mkdir(parents=True, exist_ok=True)
        got = 0
        for i, t in enumerate(data["tracks"], 1):
            if not t.get("mp3"):
                continue
            dest = folder / f"{i:02d}. {_safe(t['title'])}.mp3"
            if _download_mp3(t["mp3"], dest, f"track{i}"):
                got += 1
        return folder if got else None


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name or "track").strip() or "track"


def _download_mp3(url: str, dest_dir: Path, name: str) -> Path | None:
    if not url:
        return None
    dest = dest_dir / f"{_safe(name)}.mp3"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60
        ) as resp, open(dest, "wb") as out:
            import shutil

            shutil.copyfileobj(resp, out)
        return dest
    except Exception:
        return None
