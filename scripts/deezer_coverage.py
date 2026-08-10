"""Проверка покрытия Deezer: для каждого нескачанного трека из базы выясняем,
есть ли он на Deezer (публичный поиск + допуск длительности, как в
DeezerSource.search_track). Публичный API работает БЕЗ Premium-аккаунта и VPN.

Результат — data/reports/deezer-coverage-state.json (чекпоинт, возобновляемо)
+ итоговый отчёт markdown. Только публичный поиск: полные файлы проверятся
при скачивании (аккаунт может быть регион-ограничен даже при наличии трека).

Классификация трека (как в yandex_coverage.py):
  H (hit)  — есть подходящий результат (длительность в допуске)
  M (miss) — на Deezer ничего похожего нет
  D (mismatch) — результаты есть, но ни один по длительности не подошёл
  E (error) — сетевая/API ошибка (повтор при следующем запуске)

Использование: python scripts/deezer_coverage.py
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orpheus.config import Config
from orpheus.models import Track
from orpheus.statuses import TrackStatus, has_status
from orpheus.store import Store

VERDICT_OK = "H"
VERDICT_MISS = "M"
VERDICT_MISMATCH = "D"
VERDICT_ERROR = "E"
TOLERANCE_S = 5
PAUSE_S = 0.35


def search_deezer(query: str) -> list[dict]:
    from orpheus.deezer_client import DeezerClient

    cfg = Config()
    return DeezerClient(arl_file=cfg.data_dir / "cache" / "deezer_arl.txt").search(query)


def verdict_for(results: list[dict], track: Track) -> str:
    for r in results:
        duration_s = r.get("duration") or 0
        if duration_s and abs(duration_s * 1000 - track.duration_ms) > TOLERANCE_S * 1000:
            continue
        if not str(r.get("id") or ""):
            continue
        return VERDICT_OK
    if results:
        return VERDICT_MISMATCH
    return VERDICT_MISS


def main() -> None:
    cfg = Config()
    store = Store(cfg.db_dir).load()

    pending = []
    for rec in store.tracks.values():
        t = Track.from_dict(rec)
        if has_status(t.statuses, TrackStatus.DOWNLOADED):
            continue
        if has_status(rec.get("statuses", []), TrackStatus.CANONICAL_VERSION):
            f = rec.get("file")
            if f and (cfg.root / f).exists():
                continue
        pending.append(t)
    pending.sort(key=lambda t: t.spotify_id)

    state_path = cfg.data_dir / "reports" / "deezer-coverage-state.json"
    state = {"checked": {}}
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))

    todo = [t for t in pending if state["checked"].get(t.spotify_id) not in (VERDICT_OK, VERDICT_MISS)]
    print(
        f"всего pending: {len(pending)}, уже проверено: {len(state['checked'])}, "
        f"осталось: {len(todo)}",
        flush=True,
    )

    for i, t in enumerate(todo, 1):
        query = " ".join(t.artist_names + [t.name]).strip()
        try:
            v = verdict_for(search_deezer(query), t)
        except Exception as exc:
            v = VERDICT_ERROR
            print(f"[{i}/{len(todo)}] ERROR {t.spotify_id} {query}: {exc}", flush=True)
        state["checked"][t.spotify_id] = v
        if i % 25 == 0:
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        if i % 50 == 0:
            c = Counter(state["checked"].values())
            print(
                f"[{i}/{len(todo)}] ok={c[VERDICT_OK]} miss={c[VERDICT_MISS]} "
                f"mism={c[VERDICT_MISMATCH]} err={c[VERDICT_ERROR]}",
                flush=True,
            )
        time.sleep(PAUSE_S)

    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    c = Counter(state["checked"].values())
    report_path = cfg.data_dir / "reports" / f"deezer-coverage-{datetime.now():%Y%m%d-%H%M%S}.md"
    lines = [
        "# Покрытие Deezer по нескачанным трекам",
        "",
        f"Всего ожидает скачивания: {len(pending)}",
        f"- есть на Deezer (подходит по длительности): {c[VERDICT_OK]}",
        f"- **НЕТ на Deezer: {c[VERDICT_MISS]}**",
        f"- есть, но другой вариант (длительность не совпала): {c[VERDICT_MISMATCH]}",
        f"- сбои API (повтор при следующем запуске): {c[VERDICT_ERROR]}",
        "",
        "## Треки, которых нет на Deezer (пойдут через другие источники)",
    ]
    for rec in store.tracks.values():
        t = Track.from_dict(rec)
        if state["checked"].get(t.spotify_id) != VERDICT_MISS:
            continue
        lines.append(f"- {', '.join(t.artist_names)} — {t.name}")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print()
    print(f"Записано: {report_path}")
    print()
    print("Сводка:")
    print(f"  H (есть на Deezer):        {c[VERDICT_OK]}")
    print(f"  M (нет на Deezer):         {c[VERDICT_MISS]}")
    print(f"  D (другая версия):         {c[VERDICT_MISMATCH]}")
    print(f"  E (ошибки, повтор):        {c[VERDICT_ERROR]}")


if __name__ == "__main__":
    main()