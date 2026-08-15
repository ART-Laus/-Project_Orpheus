r"""Сверка «База ⇄ C:\Library ⇄ E:\Library»: качественный отчёт по медиатеке.

Категории (по относительному пути внутри Library/):
  only_c   — файл есть только на C: (скачано и не перенесено)
  only_e   — файл есть только на E: (фантомный статус подтверждён файлом)
  dup_same — на обоих дисках, размер совпадает → при ручном переносе можно
             пропустить (SKIP, Explorer переспросит впустую)
  dup_diff — на обоих, размер разный → Explorer спросит (ASK): файлы разные
  missing  — файла нет ни на одном диске (истинный пропуск)
  orphan_* — аудиофайлы на диске без записи в базе
  queue_hit — трек без статуса downloaded, но файл существует (докачка не нужна)

Read-only: диски только читает, пишет отчёт и JSON. Отчёт:
  data/reports/reconcile-<дата>.md, data/reports/reconcile-<дата>.json

Использование: python scripts/reconcile_library.py
"""

from __future__ import annotations

import json
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LIBRARY = ROOT / "Library"
E_LIBRARY = Path("/mnt/e/Library")

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".ape"}
SAMPLE = 8192


def _load(name: str) -> dict:
    path = DATA / "db" / name
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and name.replace(".json", "") in data:
        return data[name.replace(".json", "")]
    return data


def _rel_of_file_field(f: str) -> str | None:
    """Относительный путь внутри Library для записи базы."""
    if f.startswith("Library/"):
        return f[len("Library/"):]
    if f.startswith("/mnt/e/Library/"):
        return f[len("/mnt/e/Library/"):]
    return None


def _index_files(root: Path) -> dict[str, tuple[int, int]]:
    """{rel_path: (size, mtime)} для всех аудиофайлов под root."""
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


_NON_WORD = __import__("re").compile(r"[^a-zа-яё0-9]+")
_NUM_SEP = __import__("re").compile(r"^\d{1,3}\s*[.\-–_]\s*")


def _norm_stem(name: str) -> str:
    """Нормализация имени файла: без номера дорожки и суффиксов (N)."""
    stem = Path(name).stem
    stem = _NUM_SEP.sub("", stem)
    stem = stem.split(" (", 1)[0]
    return _NON_WORD.sub("", stem.lower())


def _fuzzy_find(files: dict[str, tuple[int, int]], rel: str) -> list[str]:
    """Поиск на диске по альбомной папке и нормализованному названию."""
    parent, name = rel.rsplit("/", 1)
    norm = _norm_stem(name)
    hits = []
    for p in files:
        if p.rsplit("/", 1)[0] == parent and _norm_stem(p.rsplit("/", 1)[1]) == norm:
            hits.append(p)
    return hits


def main() -> None:
    db = _load("tracks.json")
    e_ok = E_LIBRARY.exists()
    print(f"C:\\Library: {LIBRARY}  {'OK' if LIBRARY.exists() else 'НЕТ'}")
    print(f"E:\\Library: {E_LIBRARY}  {'OK' if e_ok else 'НЕ СМОНТИРОВАН'}")

    files_c = _index_files(LIBRARY)
    files_e = _index_files(E_LIBRARY) if e_ok else {}
    print(f"файлов на C:: {len(files_c)}, на E:: {len(files_e)}")

    rel_c = {rel for rel in files_c}
    rel_e = set(files_e)
    rel_both = rel_c & rel_e

    # --- dup_same / dup_diff: совпадающие относительные пути на обоих дисках
    dup_same: list[dict] = []
    dup_diff: list[dict] = []
    # проверка байт для одинакового размера — параллельно
    def _cmp(rel: str) -> dict | None:
        sc, _ = files_c[rel]
        se, _ = files_e[rel]
        rec = {"rel": rel, "size_c": sc, "size_e": se}
        if sc == se:
            hc = _sample_hash(LIBRARY / rel)
            he = _sample_hash(E_LIBRARY / rel)
            rec["identical"] = bool(hc and hc == he)
            rec["flag"] = "skip" if rec["identical"] else "ask"
            return rec
        rec["flag"] = "ask"
        return rec

    with ThreadPoolExecutor(max_workers=16) as pool:
        for rec in pool.map(_cmp, sorted(rel_both)):
            (dup_same if rec["flag"] == "skip" else dup_diff).append(rec)

    # --- tracks: наличие по записи базы
    by_rel: dict[str, list[str]] = {}
    for tid, t in db.items():
        rel = _rel_of_file_field(t.get("file") or "")
        if rel:
            by_rel.setdefault(rel, []).append(tid)

    only_c, only_e, missing_tracks = [], [], []
    for rel, ids in by_rel.items():
        on_c = rel in files_c
        on_e = rel in files_e
        if on_c and not on_e:
            only_c.append({"rel": rel, "track_id": ids[0]})
        elif on_e and not on_c:
            only_e.append({"rel": rel, "track_id": ids[0]})
        elif not on_c and not on_e:
            missing_tracks.append({"rel": rel, "track_id": ids[0]})

    # --- сироты: файлы на дисках без записи в базе
    orphan_c = sorted(rel for rel in files_c if rel not in by_rel)
    orphan_e = sorted(rel for rel in files_e if rel not in by_rel)

    # --- фаззи-поиск для «не найденных нигде»: имена вида "01 - Название.mp3"
    # На дисках не совпадают по пути, но совпадают по номеру/названию внутри
    # альбомной папки (E: заполнялась локальными импортами с другим форматом).
    fuzzy_c: dict[str, list[str]] = {}
    fuzzy_e: dict[str, list[str]] = {}
    for m in missing_tracks:
        hits_c = _fuzzy_find(files_c, m["rel"])
        hits_e = _fuzzy_find(files_e, m["rel"])
        if hits_c:
            fuzzy_c[m["rel"]] = hits_c
        if hits_e:
            fuzzy_e[m["rel"]] = hits_e
    fuzzy_keys = set(fuzzy_c) | set(fuzzy_e)
    still_missing = [m for m in missing_tracks if m["rel"] not in fuzzy_keys]

    # --- очередь: треки без downloaded, чей файл существует
    queue_hits: list[dict] = []
    for tid, t in db.items():
        if "downloaded" in (t.get("statuses") or []):
            continue
        rel = _rel_of_file_field(t.get("file") or "")
        if rel and (rel in files_c or rel in files_e):
            queue_hits.append({"rel": rel, "track_id": tid})

    # --- отчёт
    stamp = date.today().isoformat()
    lines: list[str] = []
    lines.append(f"# Сверка Library — {stamp}\n")
    lines.append(f"- Треков в базе: {len(db)}")
    lines.append(f"- На C:\\Library: {len(files_c)} файлов; на E:\\Library: {len(files_e)} файлов")
    lines.append(f"- Записей базы с путём: {len(by_rel)}\n")
    lines.append("## Сводка\n")
    lines.append(f"- только на C: **{len(only_c)}** → перенести на E: (без конфликтов)")
    lines.append(f"- только на E: **{len(only_e)}** → статус подтверждён, действий нет")
    lines.append(
        f"- дубли C:=E: (идентичны): **{len(dup_same)}** → флаг `skip` — при переносе пропустить"
    )
    lines.append(
        f"- дубли C:≠E: (разные): **{len(dup_diff)}** → флаг `ask` — Explorer спросит, файлы разные"
    )
    lines.append(f"- нет нигде: **{len(missing_tracks)}** → истинные пропавшие")
    lines.append(f"- из них найдено по названию (C:): **{len(fuzzy_c)}**, (E:): **{len(fuzzy_e)}**")
    lines.append(f"- реально нет нигде: **{len(still_missing)}**")
    lines.append(f"- сироты на C:: **{len(orphan_c)}**, на E:: **{len(orphan_e)}**")
    lines.append(f"- в очереди докачки, но файл уже есть: **{len(queue_hits)}**\n")

    if dup_diff:
        lines.append("## Дубли с разным размером (ASK, файлы разные)\n")
        for r in dup_diff:
            lines.append(f"- `{r['rel']}` (C: {r['size_c']} Б, E: {r['size_e']} Б)")
        lines.append("")
    if queue_hits:
        lines.append("## Очередь, но файл существует (можно пометить downloaded)\n")
        for q in queue_hits:
            lines.append(f"- `{q['rel']}`")
        lines.append("")
    if fuzzy_keys:
        lines.append("## «Пропавшие», найденные по названию (формат имён иной)\n")
        for rel in sorted(fuzzy_keys):
            nc = fuzzy_c.get(rel, [])
            ne = fuzzy_e.get(rel, [])
            where = []
            if nc:
                where.append(f"C: {'; '.join(nc)}")
            if ne:
                where.append(f"E: {'; '.join(ne)}")
            lines.append(f"- `{rel}` → {', '.join(where)}")
        lines.append("")
    if still_missing:
        lines.append("## Не найдено нигде (истинные пропавшие)\n")
        for m in still_missing:
            lines.append(f"- `{m['rel']}`")
        lines.append("")

    out_md = DATA / "reports" / f"reconcile-{stamp}.md"
    out_md.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    payload = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "counts": {
            "db_tracks": len(db),
            "files_c": len(files_c),
            "files_e": len(files_e),
            "only_c": len(only_c),
            "only_e": len(only_e),
            "dup_same": len(dup_same),
            "dup_diff": len(dup_diff),
            "missing": len(missing_tracks),
            "orphan_c": len(orphan_c),
            "orphan_e": len(orphan_e),
            "queue_hits": len(queue_hits),
            "fuzzy_c": len(fuzzy_c),
            "fuzzy_e": len(fuzzy_e),
            "still_missing": len(still_missing),
        },
        "tracks": {
            "only_c": only_c,
            "only_e": only_e,
            "missing": missing_tracks,
            "fuzzy": {rel: {"c": fuzzy_c.get(rel, []), "e": fuzzy_e.get(rel, [])}
                      for rel in sorted(fuzzy_keys)},
            "still_missing": still_missing,
            "queue_hits": queue_hits,
        },
        "duplicates": {"skip": dup_same, "ask": dup_diff},
        "orphans": {"c": orphan_c, "e": orphan_e},
    }
    (DATA / "reports" / f"reconcile-{stamp}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"Отчёт: {out_md}")
    print(
        "only_c=%d only_e=%d dup_same=%d dup_diff=%d missing=%d "
        "(fuzzy_c=%d fuzzy_e=%d still_missing=%d) "
        "orphan_c=%d orphan_e=%d queue_hits=%d"
        % (
            len(only_c), len(only_e), len(dup_same), len(dup_diff),
            len(missing_tracks), len(fuzzy_c), len(fuzzy_e), len(still_missing),
            len(orphan_c), len(orphan_e), len(queue_hits),
        )
    )


if __name__ == "__main__":
    main()