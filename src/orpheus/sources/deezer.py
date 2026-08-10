"""Источник Deezer: поиск через публичный API, скачивание MP3 320 (Premium).

Per-track: публичный поиск + фильтр по длительности (± duration_tolerance_s);
Per-album: поиск альбома (название + исполнитель), скачивание всех треков
альбома через gw-light (лучший доступный формат: MP3_320 на Premium, FLAC на
HiFi). Файлы складываются как есть — теги и раскладку делает Downloader.

Регион-блокировки и локальные ошибки не роняют прогон: трек пропускается
(download_track/download_album возвращают None).
"""

from __future__ import annotations

import re
from pathlib import Path

from ..deezer_client import DeezerClient
from ..models import Album, Track
from .base import Candidate, MusicSource, SourceError

_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_NON_WORD = re.compile(r"[^a-zа-яё0-9]+")


def _safe_name(text: str) -> str:
    return _ILLEGAL.sub("_", text).strip().strip(".") or "Трек"


def _norm(text: str) -> str:
    return _NON_WORD.sub("", (text or "").lower())


class DeezerSource(MusicSource):
    name = "deezer"
    album_capable = True

    def __init__(
        self,
        arl: str = "",
        arl_file: Path | None = None,
        duration_tolerance_s: int = 5,
        max_results: int = 6,
        request_interval: float = 0.25,
        stream_interval: float = 0.8,
    ):
        kwargs: dict = {}
        if arl:
            kwargs["arl"] = arl
        if arl_file:
            kwargs["arl_file"] = arl_file
        self.client = DeezerClient(**kwargs)
        self.duration_tolerance_s = duration_tolerance_s
        self.max_results = max_results
        self._account_ok: bool | None = None

    def available(self) -> bool:
        if not self.client.arl:
            return False
        if self._account_ok is None:
            try:
                info = self.client.account_info()
                self._account_ok = bool(info.get("user_id"))
            except Exception:
                self._account_ok = False
        return self._account_ok

    def search_track(self, track: Track) -> list[Candidate]:
        query = " ".join(track.artist_names + [track.name]).strip()
        try:
            results = self.client.search(query)
        except Exception as exc:
            raise SourceError(f"deezer: {exc}") from exc

        cands: list[Candidate] = []
        for result in results:
            if len(cands) >= self.max_results:
                break
            duration_s = result.get("duration") or 0
            if duration_s and (
                abs(duration_s * 1000 - track.duration_ms)
                > self.duration_tolerance_s * 1000
            ):
                continue
            track_id = str(result.get("id") or "")
            if not track_id:
                continue
            artist = (result.get("artist") or {}).get("name") or ""
            title = result.get("title") or ""
            cands.append(
                Candidate(
                    source=self.name,
                    filename=f"{artist} - {title}",
                    bitrate=320,
                    length_s=duration_s,
                    extension=".mp3",
                    extra={"track_id": track_id, "title": title, "artist": artist},
                )
            )
        return cands

    def download_track(self, cand: Candidate, dest_dir: Path) -> Path | None:
        track_id = cand.extra.get("track_id", "")
        if not track_id:
            return None
        dest: Path | None = None
        try:
            media = self.client.track_media(int(track_id))
            if not media:
                return None
            url = self.client.stream_url(media)
            artist = _safe_name(cand.extra.get("artist") or "Трек")
            title = _safe_name(cand.extra.get("title") or "Трек")
            ext = self.client.media_extension(media)
            dest = dest_dir / f"{artist} - {title}{ext}"
            self.client.download(url, dest)
        except Exception:
            return None
        if dest is not None and dest.exists() and dest.stat().st_size > 0:
            return dest
        return None

    # --- per-album ---------------------------------------------------------

    def search_album(self, album: Album) -> list[Candidate]:
        artist = " ".join(album.artist_names) if album.artist_names else ""
        query = f"{artist} {album.name}".strip()
        try:
            results = self.client.search_albums(query)
        except Exception as exc:
            raise SourceError(f"deezer: {exc}") from exc

        want_artist = _norm(album.artist_names[0]) if album.artist_names else ""
        want_title = _norm(album.name)
        cands: list[Candidate] = []
        for result in results:
            if len(cands) >= self.max_results:
                break
            if _norm(result.get("title") or "") != want_title:
                continue
            artist_obj = result.get("artist") or {}
            if want_artist and _norm(artist_obj.get("name") or "") != want_artist:
                continue
            cands.append(
                Candidate(
                    source=self.name,
                    filename=f"{artist_obj.get('name', '')} - {result.get('title', '')}",
                    extension=".mp3",
                    extra={"album_id": str(result.get("id") or "")},
                )
            )
        return cands

    @staticmethod
    def _tracklist(album: dict) -> list[dict]:
        """Треки альбома с абсолютными номерами (через диски), как в базе."""
        raw = (album.get("tracks") or {}).get("data") or []
        tracks = []
        for t in raw:
            disc = int(t.get("disk_number") or 1)
            pos = int(t.get("track_position") or 0)
            tracks.append({"disc": disc, "pos": pos, "data": t})
        tracks.sort(key=lambda t: (t["disc"], t["pos"] or 9999))
        disc_sizes: dict[int, int] = {}
        for t in tracks:
            disc_sizes[t["disc"]] = disc_sizes.get(t["disc"], 0) + 1
        offset = 0
        last_disc = tracks[0]["disc"] if tracks else 1
        absolute: list[tuple[int, dict]] = []
        for t in tracks:
            if t["disc"] != last_disc:
                offset += disc_sizes.get(last_disc, 0)
                last_disc = t["disc"]
            absolute.append((offset + (t["pos"] or 0), t["data"]))
        return absolute

    def download_album(self, cand: Candidate, dest_dir: Path) -> Path | None:
        album_id = cand.extra.get("album_id", "")
        if not album_id:
            return None
        try:
            album = self.client.album(album_id)
            tracks = self._tracklist(album)
            if not tracks:
                return None
            dest_dir.mkdir(parents=True, exist_ok=True)
            ok = 0
            for number, t in tracks:
                track_id = str(t.get("id") or "")
                title = t.get("title") or ""
                if not track_id or not title:
                    continue
                media = self.client.track_media(int(track_id))
                if not media:
                    continue
                url = self.client.stream_url(media)
                ext = self.client.media_extension(media)
                dest = dest_dir / f"{number:02d}. {_safe_name(title)}{ext}"
                self.client.download(url, dest)
                if dest.exists() and dest.stat().st_size > 0:
                    ok += 1
        except Exception:
            return None
        return dest_dir if ok else None