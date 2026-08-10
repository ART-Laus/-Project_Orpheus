"""Отчёт «Альбомы на докачку с торрентов»: нескачанные треки из базы,
сгруппированные по артистам и альбомам, с вердиктами чекера Яндекса.

Результат — data/reports/albums-to-download-<дата>.md.
Синглы и EP (менее 5 треков в альбоме) не включаются — как в reorganize.py.

Использование: python scripts/albums_to_download.py
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

VERDICTS = ("H", "M", "D")


def _load(name: str) -> dict:
    path = DATA / "db" / name
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and name.replace(".json", "") in data:
        return data[name.replace(".json", "")]
    return data


def _is_album(aid: str, albums: dict, pending: list[dict]) -> bool:
    """Альбом ли это? По конвенции reorganize._is_album: total_tracks >= 5,
    либо тип 'album', либо номера дорожек >= 5."""
    a = albums.get(aid, {})
    totals = a.get("total_tracks") or 0
    if totals >= 5:
        return True
    if (a.get("album_type") or "").lower() == "album":
        return True
    nums = [t.get("track_number") or 0 for t in pending]
    return bool(nums) and max(nums) >= 5


def main() -> None:
    tracks = _load("tracks.json")
    albums = _load("albums.json")
    coverage = json.loads(
        (DATA / "reports" / "yandex-coverage-state.json").read_text(encoding="utf-8")
    ).get("checked", {})

    pending = [t for t in tracks.values() if "downloaded" not in t.get("statuses", [])]

    by_album: dict[str, list[dict]] = defaultdict(list)
    for t in pending:
        by_album[t["album_id"]].append(t)

    album_ids = sorted(
        aid for aid, group in by_album.items() if _is_album(aid, albums, group)
    )

    by_artist: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for aid in album_ids:
        a = albums.get(aid, {})
        artist = ", ".join(a.get("artist_names") or ["Неизвестный исполнитель"])
        by_artist[artist][aid] = sorted(
            by_album[aid],
            key=lambda t: ((t.get("disc_number") or 1), (t.get("track_number") or 0),
                           t.get("name", "")),
        )

    lines: list[str] = []
    n_tracks = 0
    verdict_counts: dict[str, int] = defaultdict(int)
    for artist in sorted(by_artist, key=str.lower):
        lines.append(f"## {artist}")
        for i, (aid, group) in enumerate(sorted(
            by_artist[artist].items(),
            key=lambda kv: albums.get(kv[0], {}).get("release_date") or "9999-99-99",
        ), 1):
            a = albums.get(aid, {})
            total = a.get("total_tracks") or len(group)
            year = (a.get("release_date") or "")[:4]
            lines.append(
                f"{i}. {a.get('name', 'Без названия')} "
                f"({year}, осталось {len(group)}/{total})"
            )
            for t in group:
                verdicts = coverage.get(t["spotify_id"], "?")
                v = verdicts if verdicts in VERDICTS else "?"
                verdict_counts[v] += 1
                n_tracks += 1
                no = t.get("track_number") or 0
                prefix = f"{no:02d}." if no else "-"
                lines.append(f"   {prefix} {t.get('name', '')} [{v}]")
        lines.append("")

    stamp = date.today().isoformat()
    out = DATA / "reports" / f"albums-to-download-{stamp}.md"
    title = (
        f"# Альбомы на докачку с торрентов (чекер Яндекса) — {stamp}\n\n"
        f"{len(album_ids)} альбомов / {n_tracks} треков"
        f" (H: {verdict_counts.get('H', 0)}, "
        f"M: {verdict_counts.get('M', 0)}, D: {verdict_counts.get('D', 0)})\n\n"
    )
    out.write_text(title + "\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"Записано: {out}")
    print(f"Альбомов: {len(album_ids)}, треков: {n_tracks}")
    print("По вердиктам:", dict(verdict_counts))


if __name__ == "__main__":
    main()