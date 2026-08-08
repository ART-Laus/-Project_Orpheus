"""Zaycev.net: русскоязычный каталог с прямыми mp3-ссылками.

Сайт — SPA на Next.js: поиск и страницы треков отдают JSON в
<script id="__NEXT_DATA__">. Трек содержит id, artistName, track,
bitrate, duration; downloadEnabled показывает доступность скачивания.

Рабочая схема скачивания:
  POST /api/external/track/filezmeta  {trackIds: [...], subscription: null}
      -> {"tracks": [{"id": ..., "streaming": "...", "download": "..."}]}
  GET  /api/external/track/download/{download} -> прямая ссылка на mp3

Качество у большинства треков 128-320 kbps — источник используется
как «последний шанс», когда другие источники не нашли трек.
"""

from __future__ import annotations

import json
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ..models import Album, Track
from .base import Candidate, MusicSource, SourceError

BASE = "https://zaycev.net"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


class ZaycevClient:
    def __init__(self, base_url: str = BASE, timeout_s: int = 20):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor()
        )

    def _fetch(self, path: str, data: bytes | None = None) -> str:
        try:
            headers = {"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(
                self.base_url + path, data=data, headers=headers
            )
            with self._opener.open(req, timeout=self.timeout_s) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise SourceError(f"zaycev: HTTP {exc.code} {path}") from exc
        except urllib.error.URLError as exc:
            raise SourceError(f"zaycev: {exc.reason}") from exc

    @staticmethod
    def _next_data(html_text: str) -> dict:
        m = _NEXT_DATA_RE.search(html_text)
        if not m:
            return {}
        try:
            return json.loads(m.group(1))
        except ValueError:
            return {}

    def search(self, query: str) -> list[dict]:
        """Поиск треков: список {id, artist, title, bitrate, duration_ms}.

        Ищем по полному запросу; если выдача пустая (или сайт редиректит
        на страницу артиста), повторяем по одному слову из названия.
        """
        out = self._search_once(query)
        if out:
            return out
        words = [w for w in re.split(r"\s+", query) if len(w) >= 4]
        for word in words:
            out = self._search_once(word)
            if out:
                return out
        return []

    def _search_once(self, query: str) -> list[dict]:
        page = self._fetch("/search?" + urllib.parse.urlencode({"query_search": query, "type": "all"}))
        data = self._next_data(page)
        playlist = (
            data.get("props", {})
            .get("initialReduxState", {})
            .get("playlist", {})
            .get("info", {})
        )
        out: list[dict] = []
        if not isinstance(playlist, dict):
            return out
        for tid, info in playlist.items():
            if not isinstance(info, dict):
                continue
            if info.get("downloadEnabled") is False:
                continue
            bitrate = info.get("bitrate") or 0
            duration = _parse_duration(info.get("duration") or "")
            out.append(
                {
                    "id": str(tid),
                    "artist": info.get("artistName") or "",
                    "title": info.get("track") or "",
                    "bitrate": bitrate,
                    "duration_ms": duration,
                }
            )
        return out

    def _filezmeta(self, track_ids: list[str]) -> dict[str, str]:
        """Хеши streaming/download для треков: {id: {streaming, download}}."""
        body = json.dumps({"trackIds": track_ids, "subscription": None}).encode()
        try:
            text = self._fetch("/api/external/track/filezmeta", data=body)
        except SourceError:
            return {}
        try:
            payload = json.loads(text)
        except ValueError:
            return {}
        out: dict[str, str] = {}
        for item in payload.get("tracks") or []:
            out[str(item["id"])] = item
        return out

    def download_url(self, track_id: str) -> str | None:
        """Прямая ссылка на mp3 через хеш download."""
        meta = self._filezmeta([track_id]).get(track_id)
        if not meta or not meta.get("download"):
            return None
        try:
            url = self._fetch(f"/api/external/track/download/{meta['download']}").strip()
        except SourceError:
            return None
        if not url.startswith("http"):
            return None
        return url


def _parse_duration(text: str) -> int:
    """"03:50" -> 230000 мс; "1:02:33" -> 3753000 мс; 0 если не распознано."""
    m = re.match(r"(\d+):(\d{2}):(\d{2})", (text or "").strip())
    if m:
        h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return (h * 60 + mi) * 60_000 + s * 1000
    m = re.match(r"(\d+):(\d{2})", (text or "").strip())
    if m:
        mi, s = int(m.group(1)), int(m.group(2))
        return (mi * 60 + s) * 1000
    return 0


class ZaycevSource(MusicSource):
    """Источник Zaycev: русские треки, «последний шанс»."""

    name = "zaycev"

    def __init__(self, client: ZaycevClient | None = None):
        self.client = client or ZaycevClient()

    def available(self) -> bool:
        try:
            self.client._fetch("/")
            return True
        except Exception:
            return False

    def search_track(self, track: Track) -> list[Candidate]:
        query = " ".join(track.artist_names + [track.name]).strip()
        want_ms = track.duration_ms
        want_artist = " ".join(track.artist_names).lower()
        cands = []
        for item in self.client.search(query):
            if item["duration_ms"] and want_ms:
                if abs(item["duration_ms"] - want_ms) > 15_000:
                    continue
            if item["bitrate"] and item["bitrate"] < 128:
                continue
            # фильтр по артисту: имена должны пересекаться (вхождение любой части)
            item_artist = (item["artist"] or "").lower()
            if want_artist and item_artist:
                wa = _norm(want_artist)
                ia = _norm(item_artist)
                if wa not in ia and ia not in wa and not _parts_overlap(wa, ia):
                    continue
            cands.append(
                Candidate(
                    source=self.name,
                    filename=f"{item['artist']} - {item['title']}",
                    bitrate=item["bitrate"],
                    length_s=item["duration_ms"] // 1000,
                    extension=".mp3",
                    extra={"track_id": item["id"], "title": item["title"], "artist": item["artist"]},
                )
            )
        return cands

    def download_track(self, cand: Candidate, dest_dir: Path) -> Path | None:
        track_id = cand.extra.get("track_id")
        if not track_id:
            return None
        url = self.client.download_url(track_id)
        if not url:
            return None
        dest = dest_dir / f"zaycev-{track_id}.mp3"
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Referer": self.client.base_url + "/"}
            )
            with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as out:
                shutil.copyfileobj(resp, out)
            return dest
        except Exception:
            return None


def _norm(text: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", "", text.lower())


def _parts_overlap(a: str, b: str) -> bool:
    """Пересечение значимых слов имён исполнителей (для feat-записей)."""

    def words(text: str) -> set[str]:
        return {w for w in re.split(r"[^a-zа-яё0-9]+", text) if len(w) >= 3}

    wa, wb = words(a), words(b)
    return bool(wa & wb)
