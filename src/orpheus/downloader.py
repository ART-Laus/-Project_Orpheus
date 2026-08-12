"""Загрузчик музыки: получение файлов через Resolver и раскладка в Library/.

Для каждого трека из базы:
  Resolver (источники по порядку приоритета) -> проверка качества
  -> теги + обложка -> раскладка в Library/Исполнитель/Альбом/NN. Название.ext
  -> статусы downloaded / metadata_verified / cover_verified.

База не хранит URL источников: Resolver сам решает, откуда взять файл.
"""

from __future__ import annotations

import json
import re
import shutil
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import Config
from .models import Track
from .quality import QualityPolicy, verify_file
from .resolver import Resolver
from .statuses import TrackStatus, add_status
from .store import Store

_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


@dataclass
class DownloadOptions:
    library_dir: Path
    cover_min_size: int = 1000


def safe_name(text: str, max_len: int = 160) -> str:
    name = _ILLEGAL.sub("_", text).strip().strip(".")
    return name[:max_len] or "Без названия"


def unique_dest(directory: Path, stem: str, ext: str) -> Path:
    """Путь без коллизий: при существующем файле добавляем ' (2)', ' (3)'..."""
    dest = directory / f"{stem}{ext}"
    n = 2
    while dest.exists():
        dest = directory / f"{stem} ({n}){ext}"
        n += 1
    return dest


def _load_yandex_coverage(cfg: Config) -> dict[str, str]:
    """Вердикты чекера Яндекс.Музыки: {track_id: H|M|D|E}."""
    path = cfg.data_dir / "reports" / "yandex-coverage-state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        checked = state.get("checked", {})
        return {k: v for k, v in checked.items() if v in ("H", "M", "D")}
    except Exception:
        return {}


def _image_size_from_url(url: str) -> int | None:
    """Размер Spotify-обложки по коду в URL: i.scdn.co/image/ab67616d0000<code>.
    (в базе width может отсутствовать — импорт не сохранял размеры)"""
    m = re.search(r"ab67616d0000([0-9a-f]{6})", url or "")
    if not m:
        return None
    code = m.group(1)
    if code.startswith("b273"):
        return 640
    if code.startswith("01e02"):
        return 300
    if code.startswith("04851"):
        return 64
    return None


def cover_data(album: dict, cover_min_size: int = 640, timeout: float = 20) -> dict | None:
    """Данные обложки альбома (data, mime) или None.

    Вынесено в модульную функцию, чтобы её могли использовать и Downloader,
    и импортёр локальных файлов. i.scdn.co заблокирован в РФ — пробуем
    зеркала Spotify CDN.
    """
    images = album.get("images", [])
    if not images:
        return None

    def _size(img: dict) -> int:
        return img.get("width") or _image_size_from_url(img.get("url")) or 0

    images = [i for i in images if i.get("url")]
    images.sort(key=_size, reverse=True)
    best = images[0]
    if _size(best) < cover_min_size:
        return None
    for host in ("image-cdn-fa.spotifycdn.com", "image-cdn-ak.spotifycdn.com", "i.scdn.co"):
        candidate = best["url"].replace("i.scdn.co", host)
        try:
            with urllib.request.urlopen(candidate, timeout=timeout) as resp:
                data = resp.read()
        except Exception:
            continue
        mime = "image/png" if candidate.lower().endswith(".png") else "image/jpeg"
        return {"data": data, "mime": mime}
    return None


class Downloader:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        resolver: Resolver,
        opts: DownloadOptions | None = None,
    ):
        self.cfg = cfg
        self.store = store
        self.resolver = resolver
        self.opts = opts or DownloadOptions(library_dir=cfg.library_dir)
        self.policy: QualityPolicy = resolver.policy
        self.staging = cfg.data_dir / "downloads"
        self.stats = {"found": 0, "downloaded": 0, "skipped": 0, "failed": 0}
        self.missing: list[dict] = []
        self._coverage: dict[str, str] = {}

    # --- главный цикл ------------------------------------------------------

    def run(self, limit: int = 0, name_filter: str = "") -> dict:
        self.opts.library_dir.mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(parents=True, exist_ok=True)
        self._coverage = _load_yandex_coverage(self.cfg)
        pending = self._pending_tracks(name_filter)
        self.total = len(pending)
        self.processed = 0
        if self._has_album_sources() and pending:
            self._run_albums(pending, limit)
        if limit and self.stats["downloaded"] + self.stats["failed"] >= limit:
            self._write_missing_report()
            return self.stats
        for track in pending:
            current = self.store.tracks.get(track.spotify_id)
            if not current:
                continue
            track = Track.from_dict(current)
            if has_status(track.statuses, TrackStatus.DOWNLOADED):
                continue
            if self._is_canonical_with_file(current):
                continue
            if limit and self.stats["downloaded"] + self.stats["failed"] >= limit:
                break
            self.processed += 1
            self._progress(f"трек: {', '.join(track.artist_names)} — {track.name}")
            self._process(track)
            if self.processed % 25 == 0:
                # чекпоинт: ночной прогон не должен терять статусы при сбое
                self.store.save_all()
        self._write_missing_report()
        return self.stats

    def _progress(self, label: str) -> None:
        print(f"[{self.processed}/{self.total}] {label}", flush=True)

    def _has_album_sources(self) -> bool:
        return any(getattr(src, "album_capable", False) for src in self.resolver.sources)

    def _run_albums(self, pending: list[Track], limit: int) -> None:
        """Альбомный режим: группируем треки по альбомам и качаем релизы целиком."""
        from .models import Album

        by_album: dict[str, list[Track]] = {}
        for track in pending:
            by_album.setdefault(track.album_id, []).append(track)
        attempts = 0
        for album_id, tracks in by_album.items():
            if limit and self.stats["downloaded"] + self.stats["failed"] >= limit:
                break
            # лимит считается по попыткам: недоступный источник не должен
            # заставлять перебирать все альбомы базы
            if limit and attempts >= limit:
                break
            attempts += 1
            rec = self.store.albums.get(album_id)
            if not rec:
                continue
            album = Album.from_dict(rec)
            self.processed += len(tracks)
            artist = ", ".join(album.artist_names) or "Неизвестный исполнитель"
            self._progress(f"альбом: {artist} — {album.name}")
            # свой staging на альбом: файлы разных релизов не смешиваются,
            # остатки предыдущего альбома не попадают в сопоставление следующего
            own = self.staging / f"album-{album_id[:24]}"
            own.mkdir(parents=True, exist_ok=True)
            try:
                matched = self.resolver.resolve_album(album, tracks, own)
            except Exception:
                matched = {}
            for track in tracks:
                src = matched.get(track.spotify_id)
                if not src:
                    continue
                self.stats["found"] += 1
                verified = verify_file(src, track, self.policy)
                if not verified:
                    continue
                if self._finalize(src, verified, track):
                    self.stats["downloaded"] += 1
            # остатки (несматченные файлы) не должны попасть в следующий альбом
            shutil.rmtree(own, ignore_errors=True)
            if attempts % 10 == 0:
                # чекпоинт: ночной прогон не должен терять статусы при сбое
                self.store.save_all()

    def _pending_tracks(self, name_filter: str) -> list[Track]:
        """Очередь: вердикты чекера Яндекса первыми (H -> D -> M -> прочие),
        внутри — лайкнутые раньше нелайкнутых."""
        verdict_rank = {"H": 0, "D": 1, "M": 2}
        if not self._coverage:
            self._coverage = _load_yandex_coverage(self.cfg)
        items = list(self.store.tracks.values())
        items.sort(
            key=lambda t: (
                verdict_rank.get(self._coverage.get(t.get("spotify_id", ""), ""), 3),
                not t.get("liked", False),
                t.get("spotify_id", ""),
            )
        )
        result = []
        for rec in items:
            track = Track.from_dict(rec)
            if has_status(track.statuses, TrackStatus.DOWNLOADED):
                self.stats["skipped"] += 1
                continue
            if self._is_canonical_with_file(rec):
                # канон уже лежит в Library — не перекачиваем с источников
                self.stats["skipped"] += 1
                continue
            if name_filter and name_filter.lower() not in (
                track.name + " " + " ".join(track.artist_names)
            ).lower():
                continue
            result.append(track)
        return result

    def _file_exists(self, rec: dict) -> bool:
        """Файл трека реально существует на диске (Library или внешняя ФС).

        Статус downloaded без файла (например, старая библиотека на забытом
        внешнем диске) не освобождает трек от перекачки.
        """
        f = rec.get("file")
        if not f:
            return False
        p = Path(f)
        if not p.is_absolute():
            if f.startswith("Library/"):
                p = self.cfg.library_dir / f[len("Library/"):]
            else:
                p = self.cfg.root / p
        try:
            return p.exists()
        except OSError:
            return False

    def _is_canonical_with_file(self, rec: dict) -> bool:
        """Канонический трек, чей файл реально существует в Library."""
        if not has_status(rec.get("statuses", []), TrackStatus.CANONICAL_VERSION):
            return False
        return self._file_exists(rec)

    def _process(self, track: Track) -> None:
        try:
            path = self.resolver.resolve_track(track, self.staging)
        except Exception as exc:
            self.stats["failed"] += 1
            self.missing.append({"id": track.spotify_id, "name": track.name, "reason": str(exc)})
            return
        if path is None:
            self.stats["failed"] += 1
            self.missing.append(
                {"id": track.spotify_id, "name": track.name, "reason": "нет подходящих результатов"}
            )
            return
        self.stats["found"] += 1
        verified = verify_file(path, track, self.policy)
        if not verified:
            path.unlink(missing_ok=True)
            self.stats["failed"] += 1
            self.missing.append(
                {"id": track.spotify_id, "name": track.name, "reason": "файл не прошёл проверку"}
            )
            return
        if self._finalize(path, verified, track):
            self.stats["downloaded"] += 1
        else:
            path.unlink(missing_ok=True)
            self.stats["failed"] += 1
            self.missing.append(
                {"id": track.spotify_id, "name": track.name, "reason": "не удалось перенести в Library"}
            )

    # --- раскладка в библиотеку -------------------------------------------

    def _finalize(self, src: Path, verified: dict, track: Track) -> bool:
        album = self.store.albums.get(track.album_id, {})
        artist = safe_name(track.artist_names[0]) if track.artist_names else "Неизвестный исполнитель"
        album_name = safe_name(album.get("name") or track.album_name or "Сингл")
        number = track.track_number or 0
        title = safe_name(track.name)
        dest_dir = self.opts.library_dir / artist / album_name
        dest_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{number:02d}. {title}" if number else title
        dest = unique_dest(dest_dir, stem, src.suffix.lower())
        try:
            shutil.copy2(src, dest)
            self._apply_tags(dest, track, album, verified)
            src.unlink(missing_ok=True)
        except Exception:
            dest.unlink(missing_ok=True)
            return False

        statuses = list(track.statuses)
        statuses = add_status(statuses, TrackStatus.DOWNLOADED)
        statuses = add_status(statuses, TrackStatus.METADATA_VERIFIED)
        cover_ok = self._cover_ok(album)
        if cover_ok:
            statuses = add_status(statuses, TrackStatus.COVER_VERIFIED)
        self.store.tracks[track.spotify_id]["statuses"] = statuses
        self.store.tracks[track.spotify_id]["file"] = str(dest.relative_to(self.cfg.root))
        return True

    def _apply_tags(self, path: Path, track: Track, album: dict, verified: dict) -> None:
        from mutagen import File as MutagenFile
        from mutagen.easyid3 import EasyID3
        from mutagen.flac import FLAC, Picture as FLACPicture
        from mutagen.id3 import APIC, ID3, PictureType

        ext = path.suffix.lower()
        album_name = album.get("name") or track.album_name
        year = (album.get("release_date") or "")[:4]
        artist0 = track.artist_names[0] if track.artist_names else ""
        try:
            if ext == ".mp3":
                tags = EasyID3()
                try:
                    tags.load(path)
                except Exception:
                    pass
                tags["title"] = track.name
                tags["artist"] = track.artist_names
                tags["albumartist"] = artist0
                tags["album"] = album_name
                if year:
                    tags["date"] = year
                if track.track_number:
                    tags["tracknumber"] = [str(track.track_number)]
                if track.disc_number:
                    tags["discnumber"] = [str(track.disc_number)]
                tags.save()
            elif ext in (".m4a", ".aac", ".mp4", ".m4b", ".m4p"):
                # MP4-теги пишутся штатными атомами — иначе mutagen обрезает
                # ключ до 4 байт и получаются мусорные атомы titl/arti/albu
                from mutagen.mp4 import MP4

                m4a = MP4(path)
                m4a["\xa9nam"] = [track.name]
                m4a["\xa9ART"] = list(track.artist_names)
                m4a["aART"] = [artist0]
                m4a["\xa9alb"] = [album_name]
                if year:
                    m4a["\xa9day"] = [year]
                if track.track_number:
                    m4a["trkn"] = [(track.track_number, 0)]
                if track.disc_number:
                    m4a["disk"] = [(track.disc_number, 0)]
                m4a.save()
                tags = m4a
            else:
                tags = MutagenFile(path, easy=False)
                if tags is None:
                    return
                tags["title"] = track.name
                tags["artist"] = track.artist_names
                tags["albumartist"] = artist0
                tags["album"] = album_name
                if year:
                    tags["date"] = year
                if track.track_number:
                    tags["tracknumber"] = [str(track.track_number)]
                if track.disc_number:
                    tags["discnumber"] = [str(track.disc_number)]
                tags.save()
        except Exception:
            return

        cover = self._cover_data(album)
        if not cover:
            return
        try:
            if ext == ".mp3":
                id3 = ID3(path)
                id3.add(
                    APIC(
                        encoding=3,
                        mime=cover["mime"],
                        type=PictureType.COVER_FRONT,
                        desc="Cover",
                        data=cover["data"],
                    )
                )
                id3.save(path)
            elif isinstance(tags, FLAC):
                tags.clear_pictures()
                pic = FLACPicture()
                pic.type = PictureType.COVER_FRONT
                pic.mime = cover["mime"]
                pic.data = cover["data"]
                tags.add_picture(pic)
                tags.save()
        except Exception:
            pass

    def _cover_data(self, album: dict) -> dict | None:
        return cover_data(album, self.opts.cover_min_size)

    def _cover_ok(self, album: dict) -> bool:
        images = album.get("images", [])
        for img in images:
            width = img.get("width") or _image_size_from_url(img.get("url")) or 0
            if width >= self.opts.cover_min_size:
                return True
        return False

    def _write_missing_report(self) -> None:
        if not self.missing:
            return
        reports = self.cfg.data_dir / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        (reports / f"download-missing-{stamp}.json").write_text(
            json.dumps(self.missing, ensure_ascii=False, indent=1), encoding="utf-8"
        )


def has_status(statuses: list[str], status: TrackStatus) -> bool:
    return status.value in statuses
