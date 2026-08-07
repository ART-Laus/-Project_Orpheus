"""Реорганизация Library/ по структуре: у каждого артиста папки альбомов
и одна папка «Синглы и EP» для синглов и EP.

Задачи:
  1. слияние вариантов написания артиста (карта синонимов):
     PlayingTheAngel / PlayingTheAngel [#каждаябарбистерва] /
     PlayingTheAngel [6континент] / #каждаябарбистерва /
     3амолчи! [6континент] / BLAZER BOYZ / Гноев Ковчег -> playingtheangel;
  2. перенос файлов: альбомы остаются своими папками, синглы и EP
     собираются в папку «Синглы и EP» со сквозной нумерацией
     (порядок: дата релиза, затем название);
  3. обновление полей file/artist_names в базе, тегов аудиофайлов.

Папки, которые нельзя однозначно классифицировать (альбом скачан
частично, total_tracks неизвестен) не трогаются и попадают в отчёт.
"""

from __future__ import annotations

import shutil
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .downloader import safe_name
from .models import Track
from .store import Store

SINGLES_FOLDER = "Синглы и EP"
ALBUM_MIN_TRACKS = 5
AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".ape"}

# Варианты написания playingtheangel + баттл-группы -> канон
ARTIST_MERGE: dict[str, str] = {
    "playingtheangel": "playingtheangel",
    "PlayingTheAngel": "playingtheangel",
    "PlayingTheAngel [#каждаябарбистерва]": "playingtheangel",
    "PlayingTheAngel [6континент]": "playingtheangel",
    "#каждаябарбистерва": "playingtheangel",
    "3амолчи! [6континент]": "playingtheangel",
    "BLAZER BOYZ": "playingtheangel",
    "Гноев Ковчег": "playingtheangel",
}

CANON_ARTIST_ID = "5LrzvQVGJUmt227QAUfR5x"  # playingtheangel (Spotify)


@dataclass
class Move:
    src: Path
    dst: Path
    track_id: str
    artist_changed: bool = False


@dataclass
class ReorgStats:
    albums: int = 0
    singles: int = 0
    moved: int = 0
    artist_merged: int = 0
    artists_removed: int = 0
    untouched: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class LibraryReorganizer:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self.stats = ReorgStats()
        self.library = cfg.library_dir

    # --- классификация -----------------------------------------------------

    def _is_album(self, items: list[tuple[str, Track]]) -> bool:
        """Альбом ли это? Файлов >= ALBUM_MIN_TRACKS или total_tracks >= 5."""
        if len(items) >= ALBUM_MIN_TRACKS:
            return True
        totals = []
        known = False
        for _f, t in items:
            al = self.store.albums.get(t.album_id, {})
            tt = al.get("total_tracks") or 0
            if tt:
                known = True
                totals.append(tt)
        if known:
            return max(totals) >= ALBUM_MIN_TRACKS
        # total_tracks неизвестен — решаем по номерам дорожек
        nums = [t.track_number for _f, t in items if t.track_number]
        if nums and max(nums) >= 4:
            return True
        return False

    def _is_single(self, items: list[tuple[str, Track]]) -> bool:
        """Надёжно ли это сингл/EP? total_tracks известен и мал, единственный
        файл с номером 1, или все треки пришли из канона 52201 (в такой
        папке не бывает частично скачанных альбомов)."""
        if len(items) == 1 and items[0][1].track_number == 1:
            return True
        totals = []
        known = False
        for _f, t in items:
            al = self.store.albums.get(t.album_id, {})
            tt = al.get("total_tracks") or 0
            if tt:
                known = True
                totals.append(tt)
        if known:
            return max(totals) <= ALBUM_MIN_TRACKS - 1
        # total_tracks неизвестен: папки канона 52201 всегда полные
        if items and all("52201" in self.store.tracks[t.spotify_id].get("notes", "") for _f, t in items):
            return True
        return False

    def _canon_artist(self, name: str) -> str:
        return ARTIST_MERGE.get(name, name)

    # --- слияние артистов в базе -------------------------------------------

    def _merge_artists(self) -> None:
        variants = set(ARTIST_MERGE) - {"playingtheangel"}
        track_remap: dict[str, str] = {}
        # собрать все id-варианты артистов (local-записи с именами-вариантами)
        for aid, a in self.store.artists.items():
            if a.get("name") in variants:
                track_remap[aid] = CANON_ARTIST_ID
                self.stats.artists_removed += 1

        canon_local = "local-artist:a85961ec8b19cb7b"  # канонический локальный playingtheangel
        for tid, rec in self.store.tracks.items():
            names = rec.get("artist_names", [])
            if not names:
                continue
            new_names = [self._canon_artist(n) for n in names]
            new_ids = []
            for aid in rec.get("artist_ids", []):
                if aid in track_remap:
                    aid = track_remap[aid]
                elif aid in variants:
                    aid = CANON_ARTIST_ID
                new_ids.append(aid)
            # переносим первый id артиста на канон для локальных треков
            if new_names and rec.get("is_local") and new_ids and new_ids[0] not in (
                CANON_ARTIST_ID,
                canon_local,
            ):
                new_ids[0] = canon_local
            if new_names != names or new_ids != rec.get("artist_ids", []):
                rec["artist_names"] = new_names
                rec["artist_ids"] = new_ids
                self.store.tracks[tid] = rec
                if self._canon_artist(names[0]) != names[0]:
                    self.stats.artist_merged += 1

        for al in self.store.albums.values():
            names = al.get("artist_names", [])
            if not names:
                continue
            new_names = [self._canon_artist(n) for n in names]
            new_ids = [track_remap.get(i, i) for i in al.get("artist_ids", [])]
            if new_names != names or new_ids != al.get("artist_ids", []):
                al["artist_names"] = new_names
                al["artist_ids"] = new_ids

        for aid in list(track_remap):
            self.store.artists.pop(aid, None)

    # --- планирование переносов ---------------------------------------------

    def _track_from(self, tid: str) -> Track:
        return Track.from_dict(self.store.tracks[tid])

    def plan(self) -> list[Move]:
        """План переносов. Не меняет ни файлы, ни базу."""
        # (артист-папка, альбом-папка) -> [(file, Track)]
        groups: dict[tuple[str, str], list[tuple[str, Track]]] = defaultdict(list)
        for tid, rec in self.store.tracks.items():
            f = rec.get("file")
            if not f:
                continue
            parts = Path(f).parts
            if len(parts) < 3:
                continue
            groups[(parts[1], parts[2])].append((f, self._track_from(tid)))

        # слить case-варианты одной физической папки ("Побочки"/"ПОБОЧКИ"):
        # ключ по нижнему регистру, а реальные пути берём из первой записи
        merged: dict[tuple[str, str], list[tuple[str, Track]]] = defaultdict(list)
        for (artist_dir, album_dir), items in groups.items():
            key = (artist_dir.lower(), album_dir.lower())
            merged[key].extend(items)

        # определить, какие группы — альбомы
        album_keys: set[tuple[str, str]] = set()
        singles: dict[tuple[str, str], list[tuple[str, Track]]] = {}
        for (artist_key, album_key), items in merged.items():
            if album_key == SINGLES_FOLDER.lower():
                # уже собранная папка синглов — не трогаем повторно
                continue
            if self._is_album(items):
                album_keys.add((artist_key, album_key))
            elif self._is_single(items):
                singles[(artist_key, album_key)] = items
            else:
                first = items[0][0]
                self.stats.ambiguous.append(f"{Path(first).parts[1]}/{Path(first).parts[2]}")

        moves: list[Move] = []
        singles_moves: list[Move] = []

        for (artist_key, album_key), items in merged.items():
            first = items[0][0]
            artist_dir = Path(first).parts[1]
            album_dir = Path(first).parts[2]
            canon = self._canon_artist(artist_dir)
            artist_canon = safe_name(canon)
            if (artist_key, album_key) in album_keys:
                # альбом — папка сохраняется, при смене артиста переезжает
                for f, t in items:
                    src = self.cfg.root / f
                    dst = self.library / artist_canon / album_dir / Path(f).name
                    # case-вариант одной физической папки — файл не трогаем,
                    # но путь в базе нормализуем
                    if src != dst and src.resolve().as_posix().lower() == dst.resolve().as_posix().lower():
                        dst = src
                    if src != dst:
                        moves.append(Move(src, dst, t.spotify_id, artist_dir != canon))
                self.stats.albums += 1
            elif (artist_key, album_key) in singles:
                for f, t in items:
                    src = self.cfg.root / f
                    singles_moves.append(
                        Move(src, Path(""), t.spotify_id, artist_dir != canon)
                    )
                self.stats.singles += 1
            elif album_key == SINGLES_FOLDER.lower():
                continue
            else:
                self.stats.untouched.append(f"{artist_dir}/{album_dir}")

        # синглы: сквозная нумерация в «Синглы и EP» (по дате, затем названию)
        singles_moves.sort(
            key=lambda m: (
                self._release_date(self.store.tracks[m.track_id]) or "9999",
                safe_name(self.store.tracks[m.track_id].get("name", "")),
            )
        )
        for i, m in enumerate(singles_moves, 1):
            canon = safe_name(
                self._canon_artist(
                    (self.store.tracks[m.track_id].get("artist_names") or ["Неизвестный исполнитель"])[0]
                )
            )
            ext = m.src.suffix.lower()
            name = safe_name(self.store.tracks[m.track_id].get("name", ""))
            m.dst = self.library / canon / SINGLES_FOLDER / f"{i:02d}. {name}{ext}"
            moves.append(m)

        self.stats.moved = len(moves)
        return moves

    def _release_date(self, rec: dict) -> str | None:
        al = self.store.albums.get(rec.get("album_id", ""), {})
        return al.get("release_date") or None

    # --- выполнение ----------------------------------------------------------

    def apply(self, moves: list[Move], dry_run: bool = False) -> None:
        """Переносит файлы и обновляет базу."""
        for m in moves:
            if dry_run:
                continue
            try:
                if m.src != m.dst:
                    m.dst.parent.mkdir(parents=True, exist_ok=True)
                    if m.dst.exists():
                        m.dst.unlink()
                    shutil.copy2(m.src, m.dst)
                    m.src.unlink(missing_ok=True)
                self._retag(m, m.artist_changed)
            except Exception as exc:  # noqa: BLE001
                self.stats.errors.append(f"{m.src} -> {m.dst}: {exc}")
                continue
            rec = self.store.tracks.get(m.track_id)
            if rec is not None:
                rel = self._real_path(m.dst)
                try:
                    rel = str(rel.relative_to(self.cfg.root))
                except ValueError:
                    pass
                rec["file"] = rel
                self.store.tracks[m.track_id] = rec

        # удалить опустевшие папки
        if not dry_run:
            self._cleanup_empty()

    def _real_path(self, p: Path) -> Path:
        """Привести регистр пути к фактическому на диске (case-insensitive ФС)."""
        if not p.exists():
            return p
        out = p.anchor if p.anchor else Path(p.root)
        parts = p.parts if p.anchor else p.parts
        for i, part in enumerate(parts):
            if i == 0:
                continue
            parent = Path(out)
            if parent.exists() and parent.is_dir():
                real = next(
                    (c for c in parent.iterdir() if c.name.casefold() == part.casefold()), None
                )
                if real is not None:
                    out = real
                else:
                    out = parent / part
            else:
                out = Path(out) / part
        return Path(out)

    def _retag(self, m: Move, artist_changed: bool) -> None:
        if not artist_changed:
            return
        from mutagen import File as MutagenFile

        try:
            f = MutagenFile(m.dst)
            if f is None:
                return
            tags = getattr(f, "tags", None)
            if tags is None:
                return
            names = (self.store.tracks.get(m.track_id, {}).get("artist_names") or [""])
            artist = names[0]
            if "TPE1" in tags:
                from mutagen.id3 import TPE1, TPE2

                tags["TPE1"] = TPE1(encoding=3, text=[artist])
                tags["TPE2"] = TPE2(encoding=3, text=[artist])
            elif "\xa9ART" in tags:
                tags["\xa9ART"] = artist
                tags["aART"] = artist
            else:
                tags["artist"] = artist
                tags["albumartist"] = artist
            f.save()
        except Exception:
            pass

    def _cleanup_empty(self) -> None:
        def has_audio(d: Path) -> bool:
            return any(p.suffix.lower() in AUDIO_EXTS for p in d.iterdir() if p.is_file())

        for art in self.library.iterdir():
            if not art.is_dir():
                continue
            for alb in art.iterdir():
                if alb.is_dir() and not has_audio(alb):
                    shutil.rmtree(alb, ignore_errors=True)
            if not any(True for _ in art.iterdir()):
                shutil.rmtree(art, ignore_errors=True)

    def run(self, dry_run: bool = False) -> ReorgStats:
        self._merge_artists()
        moves = self.plan()
        self.apply(moves, dry_run=dry_run)
        if not dry_run:
            self.store.save_all()
        return self.stats
