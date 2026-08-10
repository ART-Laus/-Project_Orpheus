"""Проверка покрытия Яндекс.Музыки: для каждого нескачанного трека из базы
выясняем, есть ли он на Яндексе (поиск + допуск длительности, как в
YandexSource.search_track). Результат — data/reports/yandex-coverage-state.json
(чекпоинт, возобновляемо) + итоговый отчёт markdown.

Классификация трека:
  M (miss) — на Яндексе ничего похожего нет (или всё заблокировано) -> др. источники
  H (hit)  — есть подходящий результат (длительность в допуске) -> можно качать с Яндекса
  D (mismatch) — результаты есть, но ни один по длительности/доступности не подошёл
  E (error) — сетевая/API ошибка (повтор при следующем запуске)
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


def search_yandex_query(query: str) -> list[dict]:
    from orpheus.config import Config
    from orpheus.yandex_client import YandexClient

    cfg = Config()
    return YandexClient(token_file=cfg.data_dir / "cache" / "yandex_token.txt").search(query)


def verdict_for(results: list[dict], track: Track) -> str:
    for r in results:
        duration_ms = r.get("durationMs") or 0
        if duration_ms and abs(duration_ms - track.duration_ms) > TOLERANCE_S * 1000:
            continue
        if r.get("available") is False:
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

    state_path = cfg.data_dir / "reports" / "yandex-coverage-state.json"
    state = {"checked": {}}
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))

    todo = [t for t in pending if state["checked"].get(t.spotify_id) != "H" and state["checked"].get(t.spotify_id) != "M"]
    print(
        f"всего pending: {len(pending)}, уже проверено: {len(state['checked'])}, "
        f"осталось: {len(todo)}",
        flush=True,
    )

    for i, t in enumerate(todo, 1):
        query = " ".join(t.artist_names + [t.name]).strip()
        try:
            v = verdict_for(search_yandex_query(query), t)
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
        time.sleep(0.35)

    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    c = Counter(state["checked"].values())
    report_path = cfg.data_dir / "reports" / f"yandex-coverage-{datetime.now():%Y%m%d-%H%M%S}.md"
    lines = [
        "# Покрытие Яндекс.Музыки по нескачанным трекам",
        "",
        f"Всего ожидает скачивания: {len(pending)}",
        f"- есть на Яндексе (подходит по длительности): {c[VERDICT_OK]}",
        f"- **НЕТ на Яндексе: {c[VERDICT_MISS]}**",
        f"- есть, но другой вариант (длительность/доступность не совпали): {c[VERDICT_MISMATCH]}",
        f"- сбои API (повтор при следующем запуске): {c[VERDICT_ERROR]}",
        "",
        "## Треки, которых нет на Яндекс.Музыке (пойдут через другие источники)",
    ]
    for t in pending:
        if state["checked"].get(t.spotify_id) == VERDICT_MISS:
            album = t.album_name or "?"
            lines.append(
                f"- **{', '.join(t.artist_names)}** — {t.name} ({album}; {t.duration_ms / 1000:.0f} с)"
            )
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"отчёт: {report_path}", flush=True)


if __name__ == "__main__":
    main()