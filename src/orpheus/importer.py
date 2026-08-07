"""Импортёр Spotify: лайки, плейлисты, обогащение жанров.

Особенности:
- инкрементальное сохранение (resume после прерывания);
- идемпотентность: повторный запуск не дублирует данные;
- сырые ответы API складываются в data/raw/ для аудита;
- плейлисты хранят только ссылки на Spotify ID треков.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .models import (
    Album,
    Artist,
    Image,
    Playlist,
    Track,
    local_album_id,
    local_track_id,
)
from .spotify_client import SpotifyClient
from .store import Store
from .statuses import add_status, has_status, TrackStatus

PAGE_SIZE = 50


class Importer:
    def __init__(self, cfg: Config, store: Store, client: SpotifyClient):
        self.cfg = cfg
        self.store = store
        self.client = client
        self.imp: dict = store.meta.setdefault("import", {})
        self._last_save = time.monotonic()
        self._raw_enabled = cfg.raw_enabled
        self._added = 0

    # --- сохранение -------------------------------------------------------

    def _maybe_save(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or (now - self._last_save >= self.cfg.save_interval_s):
            self.store.save_all()
            self._last_save = now

    def _save_raw(self, kind: str, key: str, payload: dict) -> None:
        if not self._raw_enabled:
            return
        path: Path = self.cfg.raw_dir / kind / f"{key}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    # --- построение записей ------------------------------------------------

    def _process_artists(self, artists_raw: list[dict]) -> None:
        for a in artists_raw:
            if not a or not a.get("id"):
                continue
            self.store.upsert(
                "artists",
                a["id"],
                Artist(spotify_id=a["id"], name=a.get("name", "")).to_dict(),
            )

    def _process_track(
        self,
        item: dict,
        *,
        liked: bool,
        playlist_id: str | None,
    ) -> None:
        track = item.get("track") or {}
        added_at = item.get("added_at")
        if not track:
            return

        if track.get("is_local") or not track.get("id"):
            tid = local_track_id(
                track.get("name", ""),
                [a.get("name", "") for a in track.get("artists", [])],
                track.get("duration_ms", 0),
            )
            is_local = True
        else:
            tid = track["id"]
            is_local = False

        artist_ids = [a["id"] for a in track.get("artists", []) if a.get("id")]
        artist_names = [a.get("name", "") for a in track.get("artists", [])]

        album_raw = track.get("album") or {}
        if album_raw.get("id"):
            aid = album_raw["id"]
        else:
            aid = local_album_id(album_raw.get("name", ""), artist_names)

        existing = self.store.get("tracks", tid)
        if existing:
            rec = Track.from_dict(existing)
            changed = False
            if liked and not rec.liked:
                rec.liked = True
                changed = True
            if playlist_id and playlist_id not in rec.playlists:
                rec.playlists.append(playlist_id)
                changed = True
            if changed:
                self.store.upsert("tracks", tid, rec.to_dict())
            return

        album_artists = album_raw.get("artists", [])
        album_artist_ids = [
            a["id"] for a in album_artists if a.get("id")
        ] or artist_ids
        album_artist_names = [
            a.get("name", "") for a in album_artists
        ] or artist_names

        self._process_artists(track.get("artists", []))
        self._process_artists(album_artists)

        album_existing = self.store.get("albums", aid)
        if album_existing:
            album = Album.from_dict(album_existing)
        else:
            album = Album(
                spotify_id=aid,
                name=album_raw.get("name", ""),
                artist_ids=album_artist_ids,
                artist_names=album_artist_names,
                album_type=album_raw.get("album_type", ""),
                release_date=album_raw.get("release_date", ""),
                release_date_precision=album_raw.get("release_date_precision", ""),
                total_tracks=album_raw.get("total_tracks", 0),
                images=[
                    Image.from_dict(img)
                    for img in album_raw.get("images", [])
                    if img.get("url")
                ],
            )
        if tid not in album.track_ids:
            album.track_ids.append(tid)
        self.store.upsert("albums", aid, album.to_dict())

        external_ids = track.get("external_ids") or {}
        isrc = external_ids.get("isrc")
        playlists = [playlist_id] if playlist_id else []

        rec = Track(
            spotify_id=tid,
            name=track.get("name", ""),
            artist_ids=artist_ids,
            artist_names=artist_names,
            album_id=aid,
            album_name=album_raw.get("name", ""),
            duration_ms=track.get("duration_ms", 0),
            disc_number=track.get("disc_number", 0),
            track_number=track.get("track_number", 0),
            explicit=bool(track.get("explicit")),
            isrc=isrc,
            is_local=is_local,
            popularity=track.get("popularity", 0),
            liked=liked,
            playlists=playlists,
            added_at=added_at,
        )
        self.store.upsert("tracks", tid, rec.to_dict())
        self._added += 1

    # --- этапы импорта -----------------------------------------------------

    def import_liked(self) -> int:
        total = self.imp.get("liked_total")
        if total is None:
            probe = self.client._call(
                lambda: self.client.sp.current_user_saved_tracks(limit=1)
            )
            total = probe.get("total", 0)
            self.imp["liked_total"] = total

        offset = self.imp.get("liked_offset", 0)
        done = offset
        for page_offset, page in self.client.iter_saved_tracks():
            if page_offset < offset:
                continue
            for item in page.get("items", []):
                self._process_track(item, liked=True, playlist_id=None)
            done = page_offset + len(page.get("items", []))
            self.imp["liked_offset"] = done
            self._save_raw("likes", f"page_{page_offset}", page)
            self._maybe_save()
            print(f"  лайки: {done}/{total}", flush=True)
            if done >= total:
                break
        return done

    def import_playlists(self) -> None:
        done_ids = set(self.imp.get("playlists_done", []))
        for _, page in self.client.iter_playlists():
            for pl_raw in page.get("items", []):
                if not pl_raw or not pl_raw.get("id"):
                    continue
                pl_id = pl_raw["id"]
                if pl_id in done_ids:
                    continue
                self._import_playlist(pl_raw, pl_id)
                done_ids.add(pl_id)
                self.imp["playlists_done"] = sorted(done_ids)
                self._maybe_save()
                print(f"  плейлист обработан: {pl_raw.get('name')!r}", flush=True)

    def _import_playlist(self, pl_raw: dict, pl_id: str) -> None:
        owner = (pl_raw.get("owner") or {}).get("display_name", "")
        existing = self.store.get("playlists", pl_id)
        if existing:
            playlist = Playlist.from_dict(existing)
        else:
            playlist = Playlist(
                spotify_id=pl_id,
                name=pl_raw.get("name", ""),
                description=pl_raw.get("description", "") or "",
                owner=owner,
                snapshot_id=pl_raw.get("snapshot_id", ""),
            )

        collected: list[str] = []
        for _, page in self.client.iter_playlist_items(pl_id):
            for item in page.get("items", []):
                self._process_track(item, liked=False, playlist_id=pl_id)
                track = item.get("track") or {}
                tid = track.get("id")
                if not tid and track.get("is_local"):
                    tid = local_track_id(
                        track.get("name", ""),
                        [a.get("name", "") for a in track.get("artists", [])],
                        track.get("duration_ms", 0),
                    )
                if tid and tid not in collected:
                    collected.append(tid)
            self._save_raw("playlists", f"{pl_id}_page_{page.get('offset', 0)}", page)
            self._maybe_save()

        playlist.snapshot_id = pl_raw.get("snapshot_id", playlist.snapshot_id)
        playlist.tracks = collected
        self.store.upsert("playlists", pl_id, playlist.to_dict())

    def enrich_artists(self) -> int:
        """Жанры исполнителей пакетами по 50."""
        pending = [
            aid
            for aid, a in self.store.artists.items()
            if not a.get("genres")
        ]
        if not pending:
            return 0
        print(f"  обогащение жанров: {len(pending)} исполнителей", flush=True)
        for i in range(0, len(pending), 50):
            batch = pending[i : i + 50]
            artists = self.client.get_artists(batch)
            for raw in artists:
                if not raw or not raw.get("id"):
                    continue
                rec = Artist.from_dict(self.store.artists[raw["id"]])
                rec.genres = raw.get("genres", [])
                self.store.upsert("artists", raw["id"], rec.to_dict())
            self._maybe_save()
            print(f"  жанры: {min(i + 50, len(pending))}/{len(pending)}", flush=True)
        return len(pending)

    # --- запуск ------------------------------------------------------------

    def run(self) -> None:
        started = datetime.now(timezone.utc).isoformat()
        print("== Импорт из Spotify ==", flush=True)

        liked = self.import_liked()
        self.import_playlists()
        enriched = self.enrich_artists()

        self.imp["last_import"] = started
        self.imp["status"] = "complete"
        self.store.meta["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.store.save_all()

        print(
            f"Готово: лайков {liked}, добавлено/обновлено треков {self._added}, "
            f"обогащено исполнителей {enriched}",
            flush=True,
        )
