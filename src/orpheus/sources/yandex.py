"""Источник Яндекс.Музыки: поиск по трекам/альбомам и скачивание 320 kbps.

Per-track: результаты уточняются по длительности (± duration_tolerance_s);
Per-album: ищем альбом (название + исполнитель), качаем все его треки
с максимальным битрейтом; треки без качества >= 320 (mp3) / 256 (aac)
пропускаются. Сопоставление файлов с треками базы делает Downloader
(по номеру дорожки и названию + verify по длительности).

Файлы скачиваем как есть (mp3 320 / m4a aac 256) и не пережимаем.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..models import Album, Track
from ..yandex_client import YandexClient
from .base import Candidate, MusicSource, SourceError

_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_NON_WORD = re.compile(r"[^a-zа-яё0-9]+")


def _safe_name(text: str) -> str:
    return _ILLEGAL.sub("_", text).strip().strip(".") or "Трек"


def _norm(text: str) -> str:
    return _NON_WORD.sub("", (text or "").lower())


class YandexSource(MusicSource):
    name = "yandex"
    album_capable = True

    def __init__(
        self,
        token: str = "",
        token_file: Path | None = None,
        duration_tolerance_s: int = 5,
        max_results: int = 6,
        base_url: str | None = None,
    ):
        kwargs: dict = {"token": token, "token_file": token_file}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = YandexClient(**kwargs)
        self.duration_tolerance_s = duration_tolerance_s
        self.max_results = max_results

    def available(self) -> bool:
        if not self.client.token:
            return False
        try:
            self.client.account_status()
            return True
        except Exception:
            return False

    def search_track(self, track: Track) -> list[Candidate]:
        query = " ".join(track.artist_names + [track.name]).strip()
        try:
            results = self.client.search(query)
        except Exception as exc:
            raise SourceError(f"yandex: {exc}") from exc

        cands: list[Candidate] = []
        for result in results:
            if len(cands) >= self.max_results:
                break
            duration_ms = result.get("durationMs") or 0
            if duration_ms and abs(duration_ms - track.duration_ms) > self.duration_tolerance_s * 1000:
                continue
            if result.get("available") is False:
                continue
            track_id = str(result.get("id") or "")
            if not track_id:
                continue
            info = self.client.best_download_info(track_id)
            if not info:
                continue
            bitrate = info.get("bitrateInKbps") or 0
            codec = (info.get("codec") or "").lower()
            min_ok = 320 if codec == "mp3" else 256
            if codec not in ("mp3", "aac") or bitrate < min_ok:
                continue

            artists = " & ".join(
                a.get("name", "") for a in result.get("artists", []) if a.get("name")
            )
            title = result.get("title") or ""
            cands.append(
                Candidate(
                    source=self.name,
                    filename=f"{artists} - {title}",
                    bitrate=bitrate,
                    length_s=duration_ms // 1000,
                    extension=".mp3" if codec == "mp3" else ".m4a",
                    extra={"track_id": track_id, "title": title, "artist": artists},
                )
            )
        return cands

    def download_track(self, cand: Candidate, dest_dir: Path) -> Path | None:
        track_id = cand.extra.get("track_id", "")
        if not track_id:
            return None
        info = self.client.best_download_info(track_id)
        if not info:
            return None
        link = self.client.direct_link(info)
        if not link:
            return None
        ext = ".mp3" if (info.get("codec") or "").lower() == "mp3" else ".m4a"
        artist = _safe_name(cand.extra.get("artist") or "Трек")
        title = _safe_name(cand.extra.get("title") or "Трек")
        dest = dest_dir / f"{artist} - {title}{ext}"
        try:
            self.client.download(link, dest)
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(f"yandex: {exc}") from exc
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        return None

    # --- per-album ---------------------------------------------------------

    def search_album(self, album: Album) -> list[Candidate]:
        artist = " ".join(album.artist_names) if album.artist_names else ""
        query = f"{artist} {album.name}".strip()
        try:
            results = self.client.search(query, type_="album")
        except Exception as exc:
            raise SourceError(f"yandex: {exc}") from exc

        want_artist = _norm(album.artist_names[0]) if album.artist_names else ""
        want_title = _norm(album.name)
        cands: list[Candidate] = []
        for result in results:
            if len(cands) >= self.max_results:
                break
            if _norm(result.get("title") or "") != want_title:
                continue
            artists = [a.get("name", "") for a in result.get("artists", [])]
            if want_artist and not any(_norm(a) == want_artist for a in artists):
                continue
            title = result.get("title") or ""
            cands.append(
                Candidate(
                    source=self.name,
                    filename=f"{', '.join(artists)} - {title}",
                    extension=".mp3",
                    extra={"album_id": str(result.get("id") or "")},
                )
            )
        return cands

    def download_album(self, cand: Candidate, dest_dir: Path) -> Path | None:
        album_id = cand.extra.get("album_id", "")
        if not album_id:
            return None
        album = self.client.album_with_tracks(album_id)
        volumes = album.get("volumes") or []
        tracks = [t for disc in volumes for t in (disc or [])]
        if not tracks:
            return None
        dest_dir.mkdir(parents=True, exist_ok=True)
        ok = 0
        for t in tracks:
            track_id = str(t.get("id") or "")
            title = t.get("title") or ""
            if not track_id or t.get("available") is False or not title:
                continue
            info = self.client.best_download_info(track_id)
            if not info:
                continue
            bitrate = info.get("bitrateInKbps") or 0
            codec = (info.get("codec") or "").lower()
            min_ok = 320 if codec == "mp3" else 256
            if codec not in ("mp3", "aac") or bitrate < min_ok:
                continue
            link = self.client.direct_link(info)
            if not link:
                continue
            ext = ".mp3" if codec == "mp3" else ".m4a"
            number = t.get("trackNumber") or 0
            dest = dest_dir / f"{number:02d}. {_safe_name(title)}{ext}"
            try:
                self.client.download(link, dest)
                if dest.exists() and dest.stat().st_size > 0:
                    ok += 1
            except SourceError:
                continue
            except Exception:
                continue
        return dest_dir if ok else None
