"""Объединение медиатеки с C: в E: — «умный перенос» вместо копировки винды.

Трек/файл с C: переезжает на E: только если его там нет (по точному пути
или по названию в папке альбома); копии, чей трек/файл уже лежит на E:,
вырезаются с C:. Ничего не заменяется: улучшение качества E:-файлов —
отдельная задача.

Категории файла с C: (относительный путь внутри Library/):
  move       — на E: нет ни точного, ни похожего файла → перенос (+ обновить
               file в базе на /mnt/e/...)
  dup        — на E: уже есть тот же трек (точный путь или единственный
               похожий по названию в папке альбома) → вырезать с C:
  ambiguous  — на E: несколько похожих кандидатов → не трогать
  unknown    — файла нет в базе → переносить как есть (со структурой папок);
               если на E: уже лежит точная копия — просто вырезать с C:

Dry-run по умолчанию; --apply выполняет перенос/вырезание и обновляет базу.
Операция идемпотентна: повторный apply после обрыва продолжает с места
обрыва (проверка состояния дисков). Вызывать после завершения прогона
скачивания (downloader пишет чекпоинты и мог бы затёрtь правки).

Использование: orpheus library merge [--apply] [--artist "Имя"]
"""

from __future__ import annotations

import json
import shutil
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .config import Config
from .library_check import (
    LibraryCheck,
    E_LIBRARY_DEFAULT,
    _NON_WORD,
    _NUM_SEP,
    _rel_of_file_field,
)
from .store import Store

E_FILE_PREFIX = "/mnt/e/Library/"


def _split_stem(name: str) -> tuple[str, str | None]:
    """Имя файла на «основу» и суффикс-версию: '04. Песня (2)' → ('песня', '2')."""
    stem = Path(name).stem
    stem = _NUM_SEP.sub("", stem)
    if " (" in stem:
        base, _, suffix = stem.rpartition(" (")
        suffix = suffix[:-1] if suffix.endswith(")") else suffix
    else:
        base, suffix = stem, None
    return _NON_WORD.sub("", base.lower()), suffix


def _is_safe_dup(name_c: str, name_e: str) -> bool:
    """Вырезание C-файла безопасно только для копий с числовыми суффиксами.

    '02. Пожалуйста (2)' → '02. Пожалуйста' — мусор старого reorganize, можно
    резать. '04. Вечная' → '04. Вечная (live)' может быть ДРУГИМ треком —
    не режем (ambiguous).
    """
    base_c, suffix_c = _split_stem(name_c)
    base_e, suffix_e = _split_stem(name_e)
    if base_c != base_e:
        return False
    if suffix_c is not None and not suffix_c.isdigit():
        return False
    if suffix_e is not None and not suffix_e.isdigit():
        return False
    return True


class LibraryMerge:
    """Умный перенос C:\\Library → E:\\Library (объединение)."""

    def __init__(
        self,
        cfg: Config,
        store: Store,
        lib_c: Path | None = None,
        lib_e: Path | None = None,
        check: LibraryCheck | None = None,
    ):
        self.cfg = cfg
        self.store = store
        self.lib_c = lib_c if lib_c is not None else cfg.library_dir
        secondary = cfg.library_secondary_dirs or []
        self.lib_e = lib_e if lib_e is not None else (
            secondary[0] if secondary else E_LIBRARY_DEFAULT
        )
        self._check = check or LibraryCheck(cfg, store, lib_c=self.lib_c, lib_e=self.lib_e)
        try:
            self.e_mounted = self.lib_e.exists()
        except OSError:
            self.e_mounted = False

    def _index(self, root: Path) -> dict[str, tuple[int, int]]:
        return self._check._index(root)

    def _fuzzy_find(self, files: dict[str, tuple[int, int]], rel: str) -> list[str]:
        return self._check._fuzzy_find(files, rel)

    # --- классификация -----------------------------------------------------

    def plan(self, artist_filter: str = "") -> dict[str, Any]:
        """Построить план переноса (read-only: диски и база не меняются)."""
        if not self.e_mounted:
            raise RuntimeError(
                f"E: не смонтирован ({self.lib_e}) — перенос невозможен"
            )
        files_c = self._index(self.lib_c)
        files_e = self._index(self.lib_e)

        # Владельцы: относительный путь → треки, чьё file-поле на него смотрит
        owners: dict[str, list[dict]] = {}
        for t in self.store.tracks.values():
            rel = _rel_of_file_field(t.get("file") or "")
            if rel:
                owners.setdefault(rel, []).append(t)

        move: dict[str, dict] = {}
        dup: dict[str, dict] = {}
        ambiguous: dict[str, dict] = {}
        unknown_move: dict[str, dict] = {}
        unknown_dup: dict[str, dict] = {}

        for rel, (size_c, mtime_c) in sorted(files_c.items()):
            if artist_filter and not rel.startswith(artist_filter + "/"):
                continue
            rec = {"rel": rel, "size_c": size_c, "mtime_c": mtime_c}
            track_list = owners.get(rel, [])
            if rel in files_e:
                # на E: точная копия — вырезать с C:
                rec["size_e"] = files_e[rel][0]
                rec["different"] = size_c != files_e[rel][0]
                rec["tracks"] = [self._describe(t) for t in track_list]
                if track_list:
                    dup[rel] = rec
                else:
                    unknown_dup[rel] = rec
                continue
            hits = self._fuzzy_find(files_e, rel) if track_list else []
            if track_list and hits:
                if len(hits) == 1 and _is_safe_dup(rel, hits[0]):
                    rec["size_e"] = files_e[hits[0]][0]
                    rec["fuzzy_e"] = hits[0]
                    rec["different"] = size_c != files_e[hits[0]][0]
                    rec["tracks"] = [self._describe(t) for t in track_list]
                    dup[rel] = rec
                else:
                    rec["candidates_e"] = [(h, files_e[h][0]) for h in hits]
                    rec["tracks"] = [self._describe(t) for t in track_list]
                    ambiguous[rel] = rec
                continue
            if track_list:
                rec["tracks"] = [self._describe(t) for t in track_list]
                move[rel] = rec
            else:
                unknown_move[rel] = rec

        result = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "mode": "plan",
            "e_mounted": True,
            "counts": {
                "files_c": len(files_c),
                "files_e": len(files_e),
                "move": len(move),
                "dup": len(dup),
                "ambiguous": len(ambiguous),
                "unknown_move": len(unknown_move),
                "unknown_dup": len(unknown_dup),
            },
            "move": move,
            "dup": dup,
            "ambiguous": ambiguous,
            "unknown_move": unknown_move,
            "unknown_dup": unknown_dup,
            "errors": [],
        }
        result["counts"]["freed_bytes_c"] = sum(
            r["size_c"] for r in list(move.values()) + list(dup.values())
            + list(unknown_move.values()) + list(unknown_dup.values())
        )
        return result

    def _describe(self, t: dict) -> dict:
        return {
            "id": t.get("spotify_id"),
            "name": t.get("name", ""),
            "track_number": t.get("track_number"),
        }

    # --- исполнение --------------------------------------------------------

    def apply(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Выполнить план: перенос C: → E:, вырезание дублей, обновление базы."""
        if not self.e_mounted:
            raise RuntimeError(
                f"E: не смонтирован ({self.lib_e}) — перенос невозможен"
            )
        errors: list[str] = []
        moved = plan["move"]
        dup = plan["dup"]
        unknown_move = plan["unknown_move"]
        unknown_dup = plan["unknown_dup"]

        total = len(moved) + len(dup) + len(unknown_move) + len(unknown_dup)
        done = 0
        freed = 0

        # 1. перенос отсутствующих (по базе)
        for rel, rec in list(moved.items()):
            rc = self._apply_move(rel, rec, errors)
            freed += rc[1]
            done += 1
            if done % 500 == 0 or done == total:
                print(f"  {done}/{total}", flush=True)
        # 2. неизвестные файлы без треков
        for rel, rec in list(unknown_move.items()):
            rc = self._apply_move(rel, rec, errors)
            freed += rc[1]
            done += 1
            if done % 500 == 0 or done == total:
                print(f"  {done}/{total}", flush=True)
        # 3. вырезание дублей
        for rel, rec in list(dup.items()) + list(unknown_dup.items()):
            rc = self._apply_cut(rel, rec, errors)
            freed += rc[1]
            done += 1
            if done % 500 == 0 or done == total:
                print(f"  {done}/{total}", flush=True)

        self._prune_empty(plan)
        result = dict(plan)
        result["mode"] = "apply"
        result["generated_at"] = datetime.now().isoformat(timespec="seconds")
        result["counts"]["freed_bytes_c"] = freed
        result["errors"] = errors
        return result

    def _apply_move(self, rel: str, rec: dict, errors: list[str]) -> tuple[bool, int]:
        src = self.lib_c / rel
        dst = self.lib_e / rel
        if not src.exists():
            if not dst.exists():
                errors.append(f"move {rel}: нет ни на C:, ни на E: — пропущено")
                return False, 0
        elif dst.exists():
            if src.stat().st_size == dst.stat().st_size:
                self._cut_file(src, rec, errors)
            else:
                errors.append(f"move {rel}: файл уже есть на E:, но другого размера")
                return False, 0
        else:
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            except OSError as exc:
                errors.append(f"move {rel}: {exc}")
                return False, 0
            if src.stat().st_size != dst.stat().st_size:
                errors.append(f"move {rel}: размер E: не совпал после копирования")
                dst.unlink(missing_ok=True)
                return False, 0
            self._cut_file(src, rec, errors)
        for t in rec.get("tracks", []):
            tid = t.get("id")
            if tid in self.store.tracks:
                self.store.tracks[tid]["file"] = E_FILE_PREFIX + rel
        return True, rec["size_c"]

    def _apply_cut(self, rel: str, rec: dict, errors: list[str]) -> tuple[bool, int]:
        src = self.lib_c / rel
        if not src.exists():
            return True, 0
        new_rel = rec.get("fuzzy_e") or rel
        if not (self.lib_e / new_rel).exists():
            # со времени плана E:-копия исчезла — резать нельзя (потеря файла)
            errors.append(f"cut {rel}: E:-копии больше нет ({new_rel}) — не тронуто")
            return False, 0
        self._cut_file(src, rec, errors)
        for t in rec.get("tracks", []):
            tid = t.get("id")
            if tid in self.store.tracks:
                self.store.tracks[tid]["file"] = E_FILE_PREFIX + new_rel
        return True, rec["size_c"]

    def _cut_file(self, src: Path, rec: dict, errors: list[str]) -> None:
        try:
            src.unlink()
        except OSError as exc:
            errors.append(f"cut {rec['rel']}: {exc}")

    def _prune_empty(self, plan: dict[str, Any]) -> None:
        """Удалить опустевшие папки на C: (от альбома к исполнителю)."""
        rels = (
            list(plan.get("move", {}))
            + list(plan.get("dup", {}))
            + list(plan.get("unknown_move", {}))
            + list(plan.get("unknown_dup", {}))
        )
        for rel in rels:
            cur = (self.lib_c / rel).parent
            while cur != self.lib_c and cur != cur.parent:
                try:
                    cur.rmdir()
                except OSError:
                    break
                cur = cur.parent

    def write_report(self, result: dict[str, Any]) -> Path:
        reports_dir = self.cfg.data_dir / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        stamp = date.today().isoformat()
        json_path = reports_dir / f"merge-{stamp}.json"
        md_path = reports_dir / f"merge-{stamp}.md"
        json_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        md_path.write_text(self._render_md(result), encoding="utf-8")
        return md_path

    def _render_md(self, result: dict[str, Any]) -> str:
        c = result["counts"]
        mode = "ПЛАН (ничего не изменено)" if result["mode"] == "plan" else "ВЫПОЛНЕНО"
        freed_mb = c["freed_bytes_c"] / 1024 / 1024
        lines = [
            "# Объединение Library/ C: → E:",
            f"Сформировано: {result['generated_at']}",
            f"Режим: **{mode}**",
            "",
            "## Сводка",
            f"- Файлов на C:: {c['files_c']}, на E:: {c['files_e']}",
            f"- **Перенести** (нет на E:): {c['move']}",
            f"- **Вырезать дублей** (есть на E:): {c['dup']}",
            f"- **Неоднозначно** (не тронуто): {c['ambiguous']}",
            f"- **Без трека в базе — перенести как есть**: {c['unknown_move']}",
            f"- **Без трека в базе — вырезать (есть на E:)**: {c['unknown_dup']}",
            f"- Освободится на C:: **{freed_mb:.1f} МБ**",
            "",
        ]
        for key, title in (
            ("move", "Перенести: нет на E:"),
            ("unknown_move", "Без трека в базе — перенести как есть:"),
            ("dup", "Вырезать дубли: трек уже есть на E:"),
            ("unknown_dup", "Вырезать (без трека, есть на E:):"),
        ):
            items = result[key]
            if not items:
                continue
            lines.append(f"## {title} {len(items)}\n")
            for rel, rec in sorted(items.items()):
                note = ""
                if rec.get("size_e") and rec.get("different"):
                    note = f"  (разный размер: C {rec['size_c']} / E {rec['size_e']} Б)"
                tracks = ", ".join(
                    f"{t.get('track_number', '?')}. {t['name']}" for t in rec.get("tracks", [])
                )
                lines.append(f"- `{rel}`{note}")
                if tracks:
                    lines.append(f"  {tracks}")
            lines.append("")
        amb = result["ambiguous"]
        if amb:
            lines.append(f"## Неоднозначно (не тронуто): {len(amb)}\n")
            for rel, rec in sorted(amb.items()):
                cands = ", ".join(f"`{p}` ({s} Б)" for p, s in rec["candidates_e"])
                lines.append(f"- `{rel}`\n  кандидаты на E:: {cands}")
            lines.append("")
        if result["errors"]:
            lines.append(f"## Ошибки: {len(result['errors'])}\n")
            for e in result["errors"]:
                lines.append(f"- {e}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"