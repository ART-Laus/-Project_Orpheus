"""Jamendo: каталог Creative Commons-музыки с официальным API.

API: https://api.jamendo.com/v3.0/... требует client_id (бесплатная
регистрация разработчика: https://developer.jamendo.com). Треки
скачиваются по прямым URL MP3; качество до 320 kbps (в зависимости
от лицензии и настроек трека).
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

API_BASE = "https://api.jamendo.com/v3.0"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


class JamendoClient:
    def __init__(self, client_id: str, timeout_s: int = 20):
        self.client_id = client_id
        self.timeout_s = timeout_s

    def _get(self, endpoint: str, params: dict) -> dict:
        params = {"client_id": self.client_id, "format": "json", **params}
        url = f"{API_BASE}/{endpoint}?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": UA}), timeout=self.timeout_s
            ) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise SourceError(f"jamendo: HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise SourceError(f"jamendo: {exc.reason}") from exc

    def search_tracks(self, query: str, limit: int = 10) -> list[dict]:
        data = self._get("tracks", {"search": query, "limit": limit})
        return data.get("results") or []

    def search_albums(self, query: str, limit: int = 10) -> list[dict]:
        data = self._get("albums", {"search": query, "limit": limit})
        return data.get("results") or []

    def album_tracks(self, album_id: str) -> list[dict]:
        data = self._get("tracks", {"album_id": album_id, "limit": 100})
        return data.get("results") or []


class JamendoSource(MusicSource):
    """Источник Jamendo: треки и альбомы по CC-лицензии."""

    name = "jamendo"
    album_capable = True

    def __init__(self, client_id: str, client: JamendoClient | None = None):
        self.client = client or JamendoClient(client_id)

    def available(self) -> bool:
        try:
            return bool(self.client.search_tracks("test", limit=1))
        except Exception:
            return False

    def search_track(self, track: Track) -> list[Candidate]:
        query = " ".join(track.artist_names + [track.name]).strip()
        cands = []
        for res in self.client.search_tracks(query):
            audio = res.get("audio") or ""
            if not audio:
                continue
            cands.append(
                Candidate(
                    source=self.name,
                    filename=res.get("name", ""),
                    bitrate=_bitrate_from_url(audio),
                    extension=".mp3",
                    length_s=int((res.get("duration") or 0)),
                    extra={"audio_url": audio},
                )
            )
        return cands

    def download_track(self, cand: Candidate, dest_dir: Path) -> Path | None:
        url = cand.extra.get("audio_url")
        if not url:
            return None
        dest = dest_dir / f"{_safe(cand.filename or 'track')}.mp3"
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": UA}), timeout=120
            ) as resp, open(dest, "wb") as out:
                shutil.copyfileobj(resp, out)
            return dest
        except Exception:
            return None

    def search_album(self, album: Album) -> list[Candidate]:
        artist = " ".join(album.artist_names) if album.artist_names else ""
        query = f"{artist} {album.name}".strip()
        cands = []
        for res in self.client.search_albums(query):
            cands.append(
                Candidate(
                    source=self.name,
                    filename=res.get("name", ""),
                    extra={"album_id": str(res.get("id", ""))},
                )
            )
        return cands

    def download_album(self, cand: Candidate, dest_dir: Path) -> Path | None:
        album_id = cand.extra.get("album_id")
        if not album_id:
            return None
        folder = dest_dir / "jamendo-album"
        folder.mkdir(parents=True, exist_ok=True)
        got = 0
        for res in self.client.album_tracks(album_id):
            audio = res.get("audio") or ""
            if not audio:
                continue
            dest = folder / f"{res.get('track_num', 0):02d}. {_safe(res.get('name', 'track'))}.mp3"
            try:
                with urllib.request.urlopen(
                    urllib.request.Request(audio, headers={"User-Agent": UA}), timeout=120
                ) as resp, open(dest, "wb") as out:
                    shutil.copyfileobj(resp, out)
                got += 1
            except Exception:
                continue
        return folder if got else None


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name or "track").strip() or "track"


def _bitrate_from_url(url: str) -> int:
    """Jamendo указывает битрейт в имени файла: file-320.mp3."""
    m = re.search(r"-(\d{3})\.mp3$", url or "")
    return int(m.group(1)) if m else 0
