"""Импорт выгрузок Exportify (CSV) — обходной путь без прямого доступа к API.

CSV — полные снимки плейлистов (и «Liked Songs»). Повторный импорт заменяет
плейлист целиком, треки обновляются идемпотентно (upsert по ключу).

Ключ плейлиста синтетический: csv:sha1(имя). Локальные треки без URI
получают синтетический ID (как в API-импортёре).
"""

from __future__ import annotations

import csv
import hashlib
import re
from pathlib import Path

from .config import Config
from .models import Album, Artist, Image, Playlist, Track, local_album_id, local_track_id
from .store import Store

LIKED_SONGS_NAME = "Liked Songs"
LIKED_FILE_NAMES = {"liked", "liked songs", "liked_songs", "likedsongs"}
URI_RE = re.compile(r"([0-9A-Za-z]{22})$")
SEPARATOR_RE = re.compile(r"[;,]")


def playlist_csv_key(name: str) -> str:
    return "csv:" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:16]


def is_liked_file(name: str) -> bool:
    return name.strip().lower() in LIKED_FILE_NAMES


def parse_uri(uri: str) -> str | None:
    if not uri:
        return None
    match = URI_RE.search(uri.strip())
    return match.group(1) if match else None


def split_names(value: str) -> list[str]:
    return [p.strip() for p in SEPARATOR_RE.split(value or "") if p.strip()]


def split_uris(value: str) -> list[str]:
    return [u for u in (parse_uri(p) for p in split_names(value)) if u]


def normalize_header(header: str) -> str:
    return (
        header.strip()
        .lower()
        .replace(" ", "_")
        .replace("(", "")
        .replace(")", "")
    )


AUDIO_FEATURE_KEYS = (
    "danceability",
    "energy",
    "key",
    "loudness",
    "mode",
    "speechiness",
    "acousticness",
    "instrumentalness",
    "liveness",
    "valence",
    "tempo",
    "time_signature",
)


def _to_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class CsvImporter:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self._added = 0
        self._playlists = 0

    def run(self, export_dir: Path | str) -> None:
        export_dir = Path(export_dir)
        if not export_dir.is_dir():
            raise FileNotFoundError(f"Каталог выгрузок не найден: {export_dir}")
        files = sorted(export_dir.glob("*.csv"))
        if not files:
            raise FileNotFoundError(f"CSV не найдены в: {export_dir}")
        print(f"== Импорт CSV из {export_dir} ==", flush=True)
        for path in files:
            self._import_file(path)
            print(f"  обработан: {path.name}", flush=True)
        self.store.save_all()
        print(
            f"Готово: плейлистов {self._playlists}, "
            f"добавлено/обновлено треков {self._added}",
            flush=True,
        )

    def _import_file(self, path: Path) -> None:
        file_name = path.stem
        liked = is_liked_file(file_name)
        name = LIKED_SONGS_NAME if liked else file_name
        playlist_id = playlist_csv_key(name)

        with path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                return
            mapping = {normalize_header(h): h for h in reader.fieldnames}
            collected: list[str] = []
            for row in reader:
                tid = self._process_row(
                    row, mapping, liked=liked, playlist_id=playlist_id
                )
                if tid and tid not in collected:
                    collected.append(tid)

        playlist = Playlist(
            spotify_id=playlist_id,
            name=name,
            description="импортировано из CSV (Exportify)",
            tracks=collected,
        )
        self.store.upsert("playlists", playlist_id, playlist.to_dict())
        self._playlists += 1

    def _field(self, row: dict, mapping: dict[str, str], *keys: str) -> str:
        for key in keys:
            original = mapping.get(key)
            if original:
                return (row.get(original) or "").strip()
        return ""

    def _process_row(
        self,
        row: dict,
        mapping: dict[str, str],
        *,
        liked: bool,
        playlist_id: str,
    ) -> str | None:
        f = lambda *keys: self._field(row, mapping, *keys)

        name = f("track_name", "name")
        if not name:
            return None

        artist_names = split_names(f("artist_names", "artist_name_s", "artist"))
        artist_ids = split_uris(f("artist_uris", "artist_uri_s", "artist_uri"))

        album_name = f("album_name")
        album_uri = f("album_uri")
        album_id = parse_uri(album_uri) or local_album_id(album_name, artist_names)
        album_artist_names = split_names(
            f("album_artist_names", "album_artist_name_s")
        ) or artist_names
        album_artist_ids = split_uris(
            f("album_artist_uris", "album_artist_uri_s")
        ) or artist_ids

        image_url = f("album_image_url")
        duration_ms = int(f("track_duration_ms", "track_duration") or 0)
        isrc = f("isrc")

        tid = parse_uri(f("track_uri"))
        if tid:
            is_local = False
        else:
            tid = local_track_id(name, artist_names, duration_ms)
            is_local = True

        genres = split_names(f("artist_genres"))
        for aid, aname in zip(artist_ids, artist_names):
            existing = self.store.get("artists", aid)
            if existing:
                if genres and not existing.get("genres"):
                    existing["genres"] = genres
                    self.store.upsert("artists", aid, existing)
                continue
            self.store.upsert(
                "artists",
                aid,
                Artist(spotify_id=aid, name=aname, genres=genres).to_dict(),
            )
        if not artist_ids:
            for aname in artist_names:
                aid = "local-artist:" + hashlib.sha1(aname.encode("utf-8")).hexdigest()[:16]
                artist_ids.append(aid)
                self.store.upsert(
                    "artists",
                    aid,
                    Artist(spotify_id=aid, name=aname, genres=genres).to_dict(),
                )

        album = Album(
            spotify_id=album_id,
            name=album_name,
            artist_ids=album_artist_ids,
            artist_names=album_artist_names,
            album_type=f("album_type") or (
                "single" if album_name.strip().lower() == name.strip().lower() else ""
            ),
            release_date=f("album_release_date"),
            total_tracks=int(f("album_total_tracks") or 0),
            images=[Image(url=image_url)] if image_url else [],
            label=f("label"),
            genres=split_names(f("album_genres")),
            copyrights=f("copyrights"),
        )

        existing = self.store.get("tracks", tid)
        if existing:
            rec = Track.from_dict(existing)
            if liked and not rec.liked:
                rec.liked = True
            if playlist_id not in rec.playlists:
                rec.playlists.append(playlist_id)
            if not rec.isrc and isrc:
                rec.isrc = isrc
            if not rec.audio_features:
                rec.audio_features = self._audio_features(f)
            self.store.upsert("tracks", tid, rec.to_dict())
            return tid

        album_existing = self.store.get("albums", album_id)
        if album_existing:
            merged = Album.from_dict(album_existing)
            for field_name, value in (("label", album.label), ("copyrights", album.copyrights)):
                if value and not getattr(merged, field_name):
                    setattr(merged, field_name, value)
            if not merged.genres and album.genres:
                merged.genres = album.genres
            if tid not in merged.track_ids:
                merged.track_ids.append(tid)
            self.store.upsert("albums", album_id, merged.to_dict())
        else:
            album.track_ids.append(tid)
            self.store.upsert("albums", album_id, album.to_dict())

        explicit_raw = f("track_explicit", "explicit")
        rec = Track(
            spotify_id=tid,
            name=name,
            artist_ids=artist_ids,
            artist_names=artist_names,
            album_id=album_id,
            album_name=album_name,
            duration_ms=duration_ms,
            disc_number=int(f("disc_number") or 0),
            track_number=int(f("track_number") or 0),
            explicit=explicit_raw.lower() in ("true", "1", "yes", "да"),
            isrc=isrc or None,
            is_local=is_local,
            popularity=int(f("popularity", "track_popularity") or 0),
            liked=liked,
            playlists=[playlist_id] if playlist_id else [],
            added_at=f("added_at") or None,
            audio_features=self._audio_features(f),
        )
        self.store.upsert("tracks", tid, rec.to_dict())
        self._added += 1
        return tid

    @staticmethod
    def _audio_features(f) -> dict:
        features: dict = {}
        for key in AUDIO_FEATURE_KEYS:
            value = f(key)
            if value:
                parsed = _to_float(value)
                if parsed is not None:
                    features[key] = parsed
        return features
