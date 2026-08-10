"""Импорт локальной папки с аудиофайлами (например, торрент-дискографии).

Файлы из папки считаются каноном: сматченные с базой треки обновляются
(название, длительность), файлы приводятся к структуре Library/
Исполнитель/Альбом/NN. Название.ext и записываются в теги; не сматченные
файлы добавляются как локальные треки (is_local=True) с локальными
альбомами/исполнителями.

Матчинг каскадом:
  1. название (тег) + длительность (±10 с);
  2. название (тег) — только если кандидат ровно один;
  3. альбом (имя папки) + номер дорожки + длительность (±10 с) —
     ловит зацензуренные/переименованные названия в базе.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .downloader import cover_data, safe_name, unique_dest
from .models import Album, Track, local_album_id, local_track_id
from .statuses import TrackStatus, add_status, has_status
from .store import Store

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".ape"}
COVER_NAMES = ("cover", "folder", "front", "album")
COVER_EXTS = (".jpg", ".jpeg", ".png")

_NON_WORD = re.compile(r"[^a-zа-яё0-9]+")
_NUM_PREFIX = re.compile(r"^\s*(\d{1,3})\s*[-._\s]*(.*)$")
_FEAT = re.compile(r"\s*(?:\(?\s*(?:feat|ft|featuring)\.?[^)]*\)?)?$", re.I)
_ARTIST_FROM_FOLDER = re.compile(r"^(.*?)\s*[-–—]\s*(.+)$")
_YEAR_TAG = re.compile(r"\s*\(\s*(\d{4}(?:\.\d{2}(?:\.\d{2})?)?)\s*\)\s*$")
_EP_TAG = re.compile(r"\s*\(\s*EP\s*\)\s*$")


def _norm(text: str) -> str:
    return _NON_WORD.sub("", (text or "").lower())


def clean_title(text: str) -> str:
    """Название из файла: без хвостов '[...]' и 'feat. X'."""
    text = text or ""
    text = re.sub(r"\s*\[[^\]]*\]\s*$", "", text)
    return _FEAT.sub("", text).strip()


def split_artists(text: str) -> list[str]:
    """'A & B, C' -> [A, B, C]; состав группы в скобках — один артист."""
    text = (text or "").strip()
    if not text:
        return []
    if re.search(r"\([^)]*,", text):
        text = text.split("(", 1)[0]
    parts = re.split(r"\s*(?:&|,|feat\.?|ft\.?)\s*", text, flags=re.I)
    return [p.strip().strip(".") for p in parts if p.strip()]


@dataclass
class FileInfo:
    path: Path
    title: str
    artists: list[str]
    album_tag: str
    track_number: int
    duration_ms: int
    cover: tuple[bytes, str] | None = None


@dataclass
class ImportStats:
    files: int = 0
    matched: int = 0
    matched_by: Counter = field(default_factory=Counter)
    canonicalized: int = 0
    replaced: int = 0
    local_added: int = 0
    local_skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class LocalImporter:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        folder: Path,
        duration_tolerance_s: int = 10,
    ):
        self.cfg = cfg
        self.store = store
        self.folder = Path(folder)
        self.tolerance_ms = duration_tolerance_s * 1000
        self.stats = ImportStats()
        self._by_title: dict[str, list[Track]] = defaultdict(list)
        self._by_album_pos: dict[str, dict[int, list[Track]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self._used_tracks: set[str] = set()
        self._used_files: set[Path] = set()

    # --- подготовка -------------------------------------------------------

    def _index(self) -> None:
        tracks = [Track.from_dict(r) for r in self.store.tracks.values()]
        albums = self.store.albums
        for t in tracks:
            self._by_title[_norm(t.name)].append(t)
            album = albums.get(t.album_id, {})
            name = album.get("name")
            if name and t.track_number:
                self._by_album_pos[_norm(name)][t.track_number].append(t)

    def _scan(self) -> list[FileInfo]:
        files = sorted(
            p for p in self.folder.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS
        )
        result = []
        for p in files:
            info = self._read(p)
            if info:
                result.append(info)
        return result

    def _read(self, path: Path) -> FileInfo | None:
        from mutagen import File as MutagenFile

        try:
            f = MutagenFile(path)
            if f is None:
                return None
        except Exception:
            return None
        duration = int(getattr(f.info, "length", 0) * 1000)
        tags = getattr(f, "tags", None) or {}

        def get(*keys):
            for k in keys:
                v = tags.get(k)
                if isinstance(v, list):
                    v = v[0] if v else None
                if v:
                    return str(v).strip()
            return None

        title = get("TIT2", "title")
        artist = get("TPE1", "artist")
        album_tag = get("TALB", "album")
        trck = get("TRCK", "tracknumber")

        # из имени файла, если теги пустые
        stem = path.stem
        m = _NUM_PREFIX.match(stem.strip())
        number = 0
        if m and m.group(1).isdigit():
            number = int(m.group(1))
            stem = m.group(2).strip()
        if not title:
            parts = _ARTIST_FROM_FOLDER.match(stem)
            title = parts.group(2).strip() if parts else stem
        if not artist:
            parts = _ARTIST_FROM_FOLDER.match(stem)
            artist = parts.group(1).strip() if parts else ""
        if trck and not number:
            m2 = re.match(r"(\d+)", trck)
            if m2:
                number = int(m2.group(1))

        cover = self._cover_from_folder(path.parent)
        return FileInfo(
            path=path,
            title=title,
            artists=split_artists(artist),
            album_tag=album_tag or "",
            track_number=number,
            duration_ms=duration,
            cover=cover,
        )

    def _cover_from_folder(self, folder: Path) -> tuple[bytes, str] | None:
        for name in COVER_NAMES:
            for ext in COVER_EXTS:
                p = folder / f"{name}{ext}"
                if p.exists():
                    mime = "image/png" if ext == ".png" else "image/jpeg"
                    return p.read_bytes(), mime
        return None

    # --- матчинг ----------------------------------------------------------

    def _match(self, info: FileInfo, folder_name: str) -> tuple[Track | None, str]:
        want = _norm(clean_title(info.title))

        cands = [t for t in self._by_title.get(want, []) if t.spotify_id not in self._used_tracks]
        for t in cands:
            if t.duration_ms and abs(t.duration_ms - info.duration_ms) <= self.tolerance_ms:
                return t, "title+duration"
        if len(cands) == 1:
            return cands[0], "title"

        # альбом + номер дорожки
        album_key = folder_album_key(folder_name)
        if album_key and info.track_number:
            pos_cands = [
                t
                for t in self._by_album_pos.get(album_key, {}).get(info.track_number, [])
                if t.spotify_id not in self._used_tracks
            ]
            for t in pos_cands:
                if t.duration_ms and abs(t.duration_ms - info.duration_ms) <= self.tolerance_ms:
                    return t, "album+pos+duration"
            # позиция в совпавшем альбоме — сильный сигнал, но без проверки
            # длительности легко попасть на другой трек (альбом-сингл с тем же
            # именем, EP против сингла). Матчим, только если расхождение
            # в пределах разумного: max(допуск, 30% длительности трека).
            if len(pos_cands) == 1:
                t = pos_cands[0]
                if t.duration_ms and info.duration_ms:
                    d = abs(t.duration_ms - info.duration_ms)
                    limit = max(self.tolerance_ms, int(t.duration_ms * 0.30))
                    if d > limit:
                        return None, ""
                    if d > self.tolerance_ms:
                        self.stats.warnings.append(
                            f"{info.path.name}: расхождение длительности {d // 1000} с "
                            f"('{info.title}' vs '{t.name}')"
                        )
                return t, "album+pos"
        return None, ""

    # --- основной проход ---------------------------------------------------

    def run(self) -> ImportStats:
        self.folder = self.folder.resolve()
        if not self.folder.exists():
            raise FileNotFoundError(self.folder)
        self._index()
        infos = self._scan()
        self.stats.files = len(infos)
        for info in infos:
            try:
                self._process_file(info)
            except Exception as exc:  # noqa: BLE001
                self.stats.errors.append(f"{info.path}: {exc}")
        return self.stats

    def _process_file(self, info: FileInfo) -> None:
        track, how = self._match(info, info.path.parent.name)
        if track:
            self._import_matched(info, track, how)
        else:
            self._import_local(info)

    def _file_path(self, dest: Path) -> str:
        """Путь для поля file: относительно root, если Library внутри него."""
        try:
            return str(dest.relative_to(self.cfg.root))
        except ValueError:
            return str(dest)

    # --- сматченный трек ---------------------------------------------------

    def _import_matched(self, info: FileInfo, track: Track, how: str) -> None:
        self.stats.matched += 1
        self.stats.matched_by[how] += 1
        self._used_tracks.add(track.spotify_id)
        self._used_files.add(info.path)

        album = self.store.albums.get(track.album_id, {})
        canonical = clean_title(info.title) or track.name
        old_name = track.name

        # канонизация: папка — истина, зацензуренные имена исправляем
        renamed = canonical != track.name
        if renamed:
            note = f"канон из {self.folder.name}; было: {old_name}"
            track.notes = f"{note}\n{track.notes}".strip()
            track.name = canonical
        if info.duration_ms:
            track.duration_ms = info.duration_ms
        if renamed:
            self.stats.canonicalized += 1

        old_file = self.store.tracks.get(track.spotify_id, {}).get("file")
        dest = self._place_file(info, track, album)
        if not dest:
            if renamed:
                track.name = old_name
                track.notes = track.notes.split("\n", 1)[-1]
            self.stats.errors.append(f"{info.path}: не удалось разложить файл")
            return

        # старый (зацензуренный) файл, если путь изменился
        if old_file and self._file_path(dest) != old_file:
            old_path = self.cfg.root / old_file
            if old_path.exists() and old_path != dest:
                old_path.unlink(missing_ok=True)
                self.stats.replaced += 1

        statuses = list(track.statuses)
        statuses = add_status(statuses, TrackStatus.DOWNLOADED)
        statuses = add_status(statuses, TrackStatus.METADATA_VERIFIED)
        statuses = add_status(statuses, TrackStatus.CANONICAL_VERSION)
        if info.cover or self._db_cover_ok(album):
            statuses = add_status(statuses, TrackStatus.COVER_VERIFIED)
        rec = track.to_dict()
        rec["statuses"] = statuses
        rec["file"] = self._file_path(dest)
        self.store.tracks[track.spotify_id] = rec

    def _db_cover_ok(self, album: dict) -> bool:
        from .downloader import _image_size_from_url

        for img in album.get("images", []):
            width = img.get("width") or _image_size_from_url(img.get("url")) or 0
            if width >= self.cfg.cover_min_size:
                return True
        return False

    # --- локальный (не сматченный) трек ------------------------------------

    def _import_local(self, info: FileInfo) -> None:
        title = clean_title(info.title)
        if not title:
            self.stats.local_skipped += 1
            return
        artists = info.artists or ["Неизвестный исполнитель"]
        folder_name = info.path.parent.name
        album_name, album_type, release_date = self._album_meta(info, folder_name, title)

        tid = local_track_id(title, artists, info.duration_ms)
        existing = self.store.tracks.get(tid)
        if existing and has_status(existing.get("statuses", []), TrackStatus.DOWNLOADED):
            self.stats.local_skipped += 1
            return

        album_id = local_album_id(album_name, artists)
        album_rec = self.store.albums.get(album_id)
        if album_rec is None:
            album_rec = Album(
                spotify_id=album_id,
                name=album_name,
                artist_names=artists,
                album_type=album_type,
                release_date=release_date,
                total_tracks=0,
                images=[],
                track_ids=[],
            ).to_dict()
        if tid not in album_rec["track_ids"]:
            album_rec["track_ids"].append(tid)
        if album_rec.get("total_tracks"):
            album_rec["total_tracks"] = max(
                album_rec["total_tracks"], len(album_rec["track_ids"])
            )
        else:
            album_rec["total_tracks"] = len(album_rec["track_ids"])
        self.store.albums[album_id] = album_rec

        for i, aname in enumerate(artists):
            aid = "local-artist:" + hashlib.sha1(aname.encode("utf-8")).hexdigest()[:16]
            if aid not in self.store.artists:
                self.store.artists[aid] = {"spotify_id": aid, "name": aname, "genres": []}

        track = Track(
            spotify_id=tid,
            name=title,
            artist_ids=[f"local-artist:{aname}" for aname in artists],
            artist_names=artists,
            album_id=album_id,
            album_name=album_name,
            duration_ms=info.duration_ms,
            track_number=info.track_number,
            is_local=True,
            notes=f"локальный импорт из {self.folder.name}",
        )
        dest = self._place_file(info, track, album_rec)
        if not dest:
            self.stats.errors.append(f"{info.path}: не удалось разложить файл")
            return
        statuses = default_local_statuses(info.cover)
        rec = track.to_dict()
        rec["statuses"] = statuses
        rec["file"] = self._file_path(dest)
        self.store.tracks[tid] = rec
        self.stats.local_added += 1

    def _album_meta(
        self, info: FileInfo, folder_name: str, title: str
    ) -> tuple[str, str, str]:
        """Альбом для локального трека: из структуры папки."""
        parent = info.path.parent.name or ""
        year = _YEAR_TAG.search(folder_name)
        date = year.group(1).replace(".", "-") if year else ""

        # Синглы/(ГГГГ.ММ.ДД) Артист - Название
        date_pref = re.match(r"^\((\d{4}(?:\.\d{2}(?:\.\d{2})?)?)\)\s*(.*)$", folder_name)
        if date_pref:
            date = date_pref.group(1).replace(".", "-")
            return title, "single", date

        # Трекография/NN. Артист - Название
        if "трекографи" in folder_name.lower() or "трекографи" in parent.lower():
            return title, "single", ""

        # Альбомы/Артист - Название (ГГГГ)
        name = _strip_suffix_tags(folder_name)
        m = _ARTIST_FROM_FOLDER.match(name)
        if m:
            name = m.group(2).strip()
        return name or title, "album", date

    # --- раскладка файла ---------------------------------------------------

    def _place_file(self, info: FileInfo, track: Track, album: dict) -> Path | None:
        artist = safe_name(track.artist_names[0]) if track.artist_names else "Неизвестный исполнитель"
        album_name = safe_name(album.get("name") or track.album_name or "Сингл")
        number = track.track_number or info.track_number or 0
        dest_dir = self.cfg.library_dir / artist / album_name
        dest_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{number:02d}. {safe_name(track.name)}" if number else safe_name(track.name)
        dest = unique_dest(dest_dir, stem, info.path.suffix.lower())
        try:
            shutil.copy2(info.path, dest)
            self._apply_tags(dest, track, album, info)
            if info.cover:
                self._write_cover(dest_dir, info.cover)
        except Exception as exc:  # noqa: BLE001
            dest.unlink(missing_ok=True)
            self.stats.errors.append(f"{info.path}: {exc}")
            return None
        return dest

    def _apply_tags(
        self, path: Path, track: Track, album: dict, info: FileInfo
    ) -> None:
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

        cover = info.cover or (album.get("images") and cover_data(album, self.cfg.cover_min_size))
        if not cover:
            return
        data, mime = cover
        try:
            if ext == ".mp3":
                id3 = ID3(path)
                id3.add(
                    APIC(
                        encoding=3,
                        mime=mime,
                        type=PictureType.COVER_FRONT,
                        desc="Cover",
                        data=data,
                    )
                )
                id3.save(path)
            elif isinstance(tags, FLAC):
                tags.clear_pictures()
                pic = FLACPicture()
                pic.type = PictureType.COVER_FRONT
                pic.mime = mime
                pic.data = data
                tags.add_picture(pic)
                tags.save()
        except Exception:
            pass

    def _write_cover(self, dest_dir: Path, cover: tuple[bytes, str]) -> None:
        data, mime = cover
        ext = ".png" if mime == "image/png" else ".jpg"
        target = dest_dir / f"cover{ext}"
        if not target.exists():
            target.write_bytes(data)


def default_local_statuses(has_cover: bool) -> list[str]:
    statuses = default_statuses()
    statuses = add_status(statuses, TrackStatus.DOWNLOADED)
    statuses = add_status(statuses, TrackStatus.METADATA_VERIFIED)
    if has_cover:
        statuses = add_status(statuses, TrackStatus.COVER_VERIFIED)
    return statuses


def default_statuses() -> list[str]:
    from .statuses import default_statuses as _default

    return _default()


def _strip_suffix_tags(name: str) -> str:
    """Убирает хвосты ' (EP)' и ' (ГГГГ...)' повторно (могут идти подряд)."""
    while True:
        new = _EP_TAG.sub("", name)
        new = _YEAR_TAG.sub("", new)
        if new == name:
            return name
        name = new


def folder_album_key(folder_name: str) -> str:
    """Нормализованное имя альбома из имени папки 'Артист - Альбом (Год)'."""
    name = _strip_suffix_tags(folder_name or "")
    m = _ARTIST_FROM_FOLDER.match(name)
    if m:
        name = m.group(2)
    return _norm(name)
