"""Проверка медиатеки: категории присутствия файлов на C:⇄E:, сравнение
вариантов (дублей) и список треков для докачки другими источниками.

Категории трека (по относительному пути внутри Library/):
  only_c     — файл есть только на C:
  only_e     — файл есть только на E:
  both_same  — на обоих дисках, содержимое идентично
  both_diff  — на обоих дисках, файлы разные → варианты: сравнение качества
               (формат > битрейт > размер) и выбор канона
  nowhere    — файла нет ни на одном диске; сначала фаззи-поиск по названию
               (имена вида "01 - Название.mp3"), оставшиеся — true missing

Дополнительные списки:
  download — треки без статуса downloaded и без файла нигде (ни по пути,
             ни по фаззи) → кандидаты на докачку другими источниками
  phantom  — треки со статусом downloaded, но файла нет нигде (фантомы)

Read-only по базе: читает диски и data/db/*.json, пишет только отчёт в
data/reports/. Применение решений — отдельной командой после завершения
прогона скачивания (иначе чекпоинты downloader перезапишут правки).

Использование: orpheus library check
"""

from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date
from pathlib import Path
from typing import Any

from .config import Config
from .quality import FORMAT_RANK
from .statuses import TrackStatus, has_status
from .store import Store

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".ape"}
SAMPLE = 8192
E_LIBRARY_DEFAULT = Path("/mnt/e/Library")

_NON_WORD = re.compile(r"[^a-zа-яё0-9]+")
_NUM_SEP = re.compile(r"^\d{1,3}\s*[.\-–_]\s*")


def _norm_stem(name: str) -> str:
    """Нормализация имени файла: без номера дорожки и суффиксов (N)."""
    stem = Path(name).stem
    stem = _NUM_SEP.sub("", stem)
    stem = stem.split(" (", 1)[0]
    return _NON_WORD.sub("", stem.lower())


def _rel_of_file_field(f: str) -> str | None:
    """Относительный путь внутри Library для записи базы."""
    if f.startswith("Library/"):
        return f[len("Library/"):]
    if f.startswith("/mnt/e/Library/"):
        return f[len("/mnt/e/Library/"):]
    return None


def _sample_hash(path: Path) -> str:
    """Хеш первых и последних SAMPLE байт — дешёвая проверка идентичности."""
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            head = f.read(SAMPLE)
            h.update(head)
            f.seek(max(0, f.seek(0, 2) - SAMPLE))
            tail = f.read(SAMPLE)
            h.update(tail)
    except OSError:
        return ""
    return h.hexdigest()


def _probe(path: Path) -> dict:
    """Лёгкий проб файла: формат, битрейт, длительность, размер.

    mutagen не обязателен: при недоступности тегов сравниваем по формату
    (суффиксу) и размеру — этого достаточно для выбора канона.
    """
    info = {
        "format": path.suffix.lower().lstrip("."),
        "bitrate": 0,
        "length_s": 0,
        "size": 0,
    }
    try:
        info["size"] = path.stat().st_size
    except OSError:
        return info
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(path, easy=False)
        if audio is not None and audio.info is not None:
            info["bitrate"] = int(getattr(audio.info, "bitrate", 0) or 0)
            info["length_s"] = float(getattr(audio.info, "length", 0) or 0)
    except Exception:
        pass
    return info


def _quality_key(probe: dict) -> tuple:
    """Сортировочный ключ качества файла: больше — лучше."""
    fmt = FORMAT_RANK.get(f".{probe['format']}", 0)
    return (fmt, probe["bitrate"], probe["size"])


def _describe(db_track: dict) -> dict:
    return {
        "id": db_track.get("spotify_id"),
        "name": db_track.get("name", ""),
        "artist": ", ".join(db_track.get("artist_names") or []),
        "album": db_track.get("album_name", ""),
        "duration_ms": db_track.get("duration_ms", 0),
        "isrc": db_track.get("isrc"),
        "downloaded": has_status(db_track.get("statuses", []), TrackStatus.DOWNLOADED),
    }


class LibraryCheck:
    """Анализ медиатеки. Только чтение: база и диски не изменяются."""

    def __init__(
        self,
        cfg: Config,
        store: Store,
        lib_c: Path | None = None,
        lib_e: Path | None = None,
    ):
        self.cfg = cfg
        self.store = store
        self.lib_c = lib_c if lib_c is not None else cfg.library_dir
        self.lib_e = lib_e if lib_e is not None else E_LIBRARY_DEFAULT
        try:
            self.e_mounted = self.lib_e.exists()
        except OSError:
            self.e_mounted = False

    # --- индексация дисков ----------------------------------------------

    def _index(self, root: Path) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        if not root.exists():
            return out
        for p in root.rglob("*"):
            if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
                try:
                    st = p.stat()
                except OSError:
                    continue
                rel = str(p.relative_to(root)).replace("\\", "/")
                out[rel] = (st.st_size, int(st.st_mtime))
        return out

    def _fuzzy_find(self, files: dict[str, tuple[int, int]], rel: str) -> list[str]:
        """Поиск на диске по альбомной папке и нормализованному названию."""
        parent, name = rel.rsplit("/", 1)
        norm = _norm_stem(name)
        return [
            p
            for p in files
            if p.rsplit("/", 1)[0] == parent and _norm_stem(p.rsplit("/", 1)[1]) == norm
        ]

    # --- основной анализ ------------------------------------------------

    def analyze(self) -> dict[str, Any]:
        files_c = self._index(self.lib_c)
        files_e = self._index(self.lib_e) if self.e_mounted else {}
        print(
            f"C:\\Library: {self.lib_c}  файлов {len(files_c)};  "
            f"E:\\Library: {self.lib_e}  "
            f"{'файлов ' + str(len(files_e)) if self.e_mounted else 'НЕ СМОНТИРОВАН'}"
        )

        categories = {
            "only_c": [],
            "only_e": [],
            "both_same": [],
            "both_diff": [],
            "nowhere": [],
        }
        variants: list[dict] = []
        download: list[dict] = []
        phantom: list[dict] = []

        for tid, t in self.store.tracks.items():
            rel = _rel_of_file_field(t.get("file") or "")
            on_c = bool(rel and rel in files_c)
            on_e = bool(rel and rel in files_e)
            rec = _describe(t)
            rec["rel"] = rel or ""

            if on_c and on_e:
                same = files_c[rel][0] == files_e[rel][0]
                if same:
                    hc = _sample_hash(self.lib_c / rel)
                    he = _sample_hash(self.lib_e / rel)
                    same = bool(hc and hc == he)
                if same:
                    categories["both_same"].append(rec)
                else:
                    categories["both_diff"].append(rec)
                    variants.append(
                        self._compare_variant(rel, t, self.lib_c / rel, self.lib_e / rel)
                    )
            elif on_c:
                # на E: та же песня с другим именем (фаззи) — вариант
                hits = self._fuzzy_find(files_e, rel) if rel else []
                if hits:
                    categories["both_diff"].append(rec)
                    variants.append(
                        self._compare_variant(
                            rel, t, self.lib_c / rel, self.lib_e / hits[0], hit_e=True
                        )
                    )
                else:
                    categories["only_c"].append(rec)
            elif on_e:
                hits = self._fuzzy_find(files_c, rel) if rel else []
                if hits:
                    categories["both_diff"].append(rec)
                    variants.append(
                        self._compare_variant(
                            rel, t, self.lib_c / hits[0], self.lib_e / rel, hit_c=True
                        )
                    )
                else:
                    categories["only_e"].append(rec)
            else:
                hit_c = self._fuzzy_find(files_c, rel) if rel else []
                hit_e = self._fuzzy_find(files_e, rel) if rel else []
                if hit_c or hit_e:
                    rec["fuzzy"] = {
                        "c": hit_c[:5],
                        "e": hit_e[:5],
                    }
                    categories["nowhere"].append(rec)
                    continue
                categories["nowhere"].append(rec)
                if rec["downloaded"]:
                    phantom.append(rec)
                else:
                    download.append(rec)

        variants.sort(key=lambda v: v["rel"])
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "counts": {
                "db_tracks": len(self.store.tracks),
                "files_c": len(files_c),
                "files_e": len(files_e),
                "e_mounted": self.e_mounted,
                "only_c": len(categories["only_c"]),
                "only_e": len(categories["only_e"]),
                "both_same": len(categories["both_same"]),
                "both_diff": len(categories["both_diff"]),
                "nowhere": len(categories["nowhere"]),
                "download": len(download),
                "phantom": len(phantom),
            },
            "tracks": categories,
            "variants": variants,
            "download": download,
            "phantom": phantom,
        }

    def _compare_variant(
        self,
        rel: str,
        t: dict,
        path_c: Path,
        path_e: Path,
        hit_c: bool = False,
        hit_e: bool = False,
    ) -> dict:
        """Сравнение вариантов на C: и E: — выбор канона.

        path_c/path_e — файлы для сравнения; hit_c/hit_e отмечают, что файл
        найден по названию (фаззи), а не по точному пути из базы.
        Критерий: формат (FLAC > AAC/M4A > MP3) > битрейт > размер.
        При равенстве всех — приоритет C: (туда пишет downloader).
        """
        probe_c = _probe(path_c)
        probe_e = _probe(path_e)
        kc = _quality_key(probe_c)
        ke = _quality_key(probe_e)
        if kc == ke:
            canonical, reason = "c", "формат/битрейт/размер равны — приоритет C:"
        elif kc > ke:
            canonical, reason = "c", f"C: лучше ({kc} > {ke})"
        else:
            canonical, reason = "e", f"E: лучше ({ke} > {kc})"
        return {
            "rel": rel,
            "track": _describe(t),
            "c": probe_c,
            "e": probe_e,
            "c_fuzzy": hit_c,
            "e_fuzzy": hit_e,
            "canonical": canonical,
            "variant": "e" if canonical == "c" else "c",
            "reason": reason,
        }

    # --- фантомы -----------------------------------------------------------

    def find_phantoms(self) -> dict[str, Any]:
        """Фантомы: статус downloaded, но файла нет ни на C:, ни на E:.

        Для каждого — результат фаззи-поиска по названию (похожий файл под
        другим именем/форматом), чтобы отличить «переименованные» от
        «по-настоящему отсутствующих».
        """
        files_c = self._index(self.lib_c)
        files_e = self._index(self.lib_e) if self.e_mounted else {}
        print(
            f"C:\\Library: {self.lib_c}  файлов {len(files_c)};  "
            f"E:\\Library: {self.lib_e}  "
            f"{'файлов ' + str(len(files_e)) if self.e_mounted else 'НЕ СМОНТИРОВАН'}"
        )
        phantoms: list[dict] = []
        for tid, t in self.store.tracks.items():
            if not has_status(t.get("statuses", []), TrackStatus.DOWNLOADED):
                continue
            rel = _rel_of_file_field(t.get("file") or "")
            if not rel:
                continue
            if rel in files_c or rel in files_e:
                continue
            rec = _describe(t)
            rec["rel"] = rel
            rec["fuzzy_c"] = self._fuzzy_find(files_c, rel)
            rec["fuzzy_e"] = self._fuzzy_find(files_e, rel)
            phantoms.append(rec)
        phantoms.sort(
            key=lambda r: (r["artist"].lower(), r["album"].lower(), r["name"].lower())
        )
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "e_mounted": self.e_mounted,
            "count": len(phantoms),
            "with_fuzzy": sum(
                1 for p in phantoms if p["fuzzy_c"] or p["fuzzy_e"]
            ),
            "phantoms": phantoms,
        }

    def write_phantom_report(self, result: dict[str, Any]) -> Path:
        reports_dir = self.cfg.data_dir / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        stamp = date.today().isoformat()
        json_path = reports_dir / f"phantom-{stamp}.json"
        md_path = reports_dir / f"phantom-{stamp}.md"
        json_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        md_path.write_text(self._render_phantom_md(result), encoding="utf-8")
        return md_path

    def _render_phantom_md(self, result: dict[str, Any]) -> str:
        lines = [
            "# Фантомы: downloaded без файла",
            f"Сформировано: {result['generated_at']}",
            "",
            f"- Всего: **{result['count']}**",
            f"- Из них с похожим файлом на диске (другое имя/формат): "
            f"**{result['with_fuzzy']}**",
            "",
        ]
        no_fuzzy = [p for p in result["phantoms"] if not (p["fuzzy_c"] or p["fuzzy_e"])]
        fuzzy = [p for p in result["phantoms"] if p["fuzzy_c"] or p["fuzzy_e"]]
        lines.append(f"## Без файла нигде: {len(no_fuzzy)}\n")
        for p in no_fuzzy:
            lines.append(
                f"- `{p['id']}` — {p['artist']} — «{p['name']}» "
                f"({p['album']}, isrc: {p['isrc'] or '—'})\n  путь: `{p['rel']}`"
            )
        lines.append("")
        lines.append(f"## Есть похожий файл по названию: {len(fuzzy)}\n")
        for p in fuzzy:
            hits = []
            for side in ("c", "e"):
                for h in p[f"fuzzy_{side}"]:
                    hits.append(f"{side}: `{h}`")
            lines.append(
                f"- `{p['id']}` — {p['artist']} — «{p['name']}» "
                f"({p['album']})\n  путь в базе: `{p['rel']}`\n  найдено: {', '.join(hits)}"
            )
        lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    # --- починка путей фантомов -----------------------------------------

    def fix_phantom_paths(self) -> dict[str, Any]:
        """Переписать file-поле фантомов на найденный по названию файл.

        Берёт фантомы из find_phantoms и для тех, чей файл однозначно
        находится на одном из дисков (ровно один кандидат на C: или E:),
        правит путь в базе (Store): файл существует, перекачка не нужна.
        Неоднозначные (несколько кандидатов / на обоих дисках) — в
        ambiguous без изменений. Вызывать после завершения прогона
        скачивания: downloader пишет чекпоинты и затёр бы правки.
        """
        files_c = self._index(self.lib_c)
        files_e = self._index(self.lib_e) if self.e_mounted else {}
        print(
            f"C:\\Library: {self.lib_c}  файлов {len(files_c)};  "
            f"E:\\Library: {self.lib_e}  "
            f"{'файлов ' + str(len(files_e)) if self.e_mounted else 'НЕ СМОНТИРОВАН'}"
        )
        fixed: list[dict] = []
        ambiguous: list[dict] = []
        for tid, t in self.store.tracks.items():
            if not has_status(t.get("statuses", []), TrackStatus.DOWNLOADED):
                continue
            rel = _rel_of_file_field(t.get("file") or "")
            if not rel:
                continue
            if rel in files_c or rel in files_e:
                continue
            hits_c = self._fuzzy_find(files_c, rel)
            hits_e = self._fuzzy_find(files_e, rel)
            if not hits_c and not hits_e:
                continue
            rec = _describe(t)
            rec["rel"] = rel
            both = hits_c and hits_e
            if both and set(hits_c) != set(hits_e):
                # разные файлы на обоих дисках — это варианты, а не копия
                rec["candidates"] = [("c", h) for h in hits_c] + [("e", h) for h in hits_e]
                ambiguous.append(rec)
                continue
            if hits_c:
                side, new_rel = "c", hits_c[0]
            elif len(hits_e) == 1:
                side, new_rel = "e", hits_e[0]
            else:
                rec["candidates"] = [("e", h) for h in hits_e]
                ambiguous.append(rec)
                continue
            rec["fixed_from"] = rel
            rec["fixed_to"] = new_rel
            rec["target"] = side
            self.store.tracks[tid]["file"] = f"Library/{new_rel}"
            fixed.append(rec)
        fixed.sort(key=lambda r: (r["artist"].lower(), r["album"].lower(), r["name"].lower()))
        ambiguous.sort(
            key=lambda r: (r["artist"].lower(), r["album"].lower(), r["name"].lower())
        )
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "e_mounted": self.e_mounted,
            "fixed": fixed,
            "ambiguous": ambiguous,
        }

    def write_fix_report(self, result: dict[str, Any]) -> Path:
        reports_dir = self.cfg.data_dir / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        stamp = date.today().isoformat()
        json_path = reports_dir / f"fix-paths-{stamp}.json"
        md_path = reports_dir / f"fix-paths-{stamp}.md"
        json_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        md_path.write_text(self._render_fix_md(result), encoding="utf-8")
        return md_path

    def _render_fix_md(self, result: dict[str, Any]) -> str:
        fixed, ambiguous = result["fixed"], result["ambiguous"]
        lines = [
            "# Починка путей фантомов",
            f"Сформировано: {result['generated_at']}",
            "",
            f"- Исправлено: **{len(fixed)}**",
            f"- Неоднозначно (не тронуто): **{len(ambiguous)}**",
            "",
            f"## Исправлено: {len(fixed)}\n",
        ]
        for r in fixed:
            lines.append(
                f"- `{r['id']}` — {r['artist']} — «{r['name']}» ({r['album']})\n"
                f"  `{r['fixed_from']}` → `{r['fixed_to']}` (на {r['target'].upper()}:)"
            )
        lines.append("")
        lines.append(f"## Неоднозначно: {len(ambiguous)}\n")
        for r in ambiguous:
            cands = ", ".join(f"{s}: `{p}`" for s, p in r["candidates"])
            lines.append(
                f"- `{r['id']}` — {r['artist']} — «{r['name']}» ({r['album']})\n"
                f"  путь в базе: `{r['rel']}`\n  кандидаты: {cands}"
            )
        lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    # --- отчёт -----------------------------------------------------------

    def write_report(self, result: dict[str, Any]) -> Path:
        reports_dir = self.cfg.data_dir / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        stamp = date.today().isoformat()
        json_path = reports_dir / f"library-check-{stamp}.json"
        md_path = reports_dir / f"library-check-{stamp}.md"

        json_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        md_path.write_text(self._render_md(result), encoding="utf-8")
        return md_path

    def _render_md(self, result: dict[str, Any]) -> str:
        c = result["counts"]
        lines = [
            "# Проверка медиатеки (C: ⇄ E:)",
            f"Сформировано: {result['generated_at']}",
            "",
            f"- Треков в базе: {c['db_tracks']}",
            f"- Файлов на C:: {c['files_c']}, на E:: {c['files_e']}"
            + (" (E: НЕ СМОНТИРОВАН)" if not c["e_mounted"] else ""),
            "",
            "## Сводка",
            "",
            f"- только на C:: **{c['only_c']}** → перенести на E:",
            f"- только на E:: **{c['only_e']}** → действий нет",
            f"- на обоих, идентичны: **{c['both_same']}**",
            f"- на обоих, разные (варианты): **{c['both_diff']}** → см. раздел «Варианты»",
            f"- нет нигде: **{c['nowhere']}** (включая найденные по названию)",
            f"- на докачку другими источниками: **{c['download']}**",
            f"- фантомы (downloaded без файла): **{c['phantom']}**",
            "",
        ]

        variants = result["variants"]
        if variants:
            lines.append(f"## Варианты: {len(variants)} (разные файлы на C: и E:)\n")
            for v in variants:
                mark = "**КАНОН**" if v["canonical"] == "c" else "—"
                emark = "**КАНОН**" if v["canonical"] == "e" else "—"
                tr = v["track"]
                c_note = " (по названию)" if v.get("c_fuzzy") else ""
                e_note = " (по названию)" if v.get("e_fuzzy") else ""
                lines.append(
                    f"- `{v['rel']}` — {tr['artist']} — «{tr['name']}»\n"
                    f"  - C{c_note}: {v['c']['format']} {v['c']['bitrate'] or '?'} кбит/с, "
                    f"{v['c']['size']} Б — {mark}\n"
                    f"  - E{e_note}: {v['e']['format']} {v['e']['bitrate'] or '?'} кбит/с, "
                    f"{v['e']['size']} Б — {emark}\n"
                    f"  - решение: {v['reason']}"
                )
            lines.append("")

        download = result["download"]
        if download:
            lines.append(f"## На докачку другими источниками: {len(download)}\n")
            for d in download:
                lines.append(
                    f"- `{d['id']}` — {d['artist']} — «{d['name']}» "
                    f"({d['album']}, {d['duration_ms']} мс, isrc: {d['isrc'] or '—'})"
                )
            lines.append("")

        phantom = result["phantom"]
        if phantom:
            lines.append(f"## Фантомы (downloaded, файла нет): {len(phantom)}\n")
            for p in phantom:
                lines.append(f"- `{p['id']}` — {p['artist']} — «{p['name']}» ({p['rel']})")
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"
