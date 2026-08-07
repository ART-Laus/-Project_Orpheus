"""CLI: orpheus import | status | stats."""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter

from .config import Config, ConfigError
from .csv_importer import CsvImporter
from .dedup import DedupAnalyzer, DedupApplier
from .importer import Importer
from .models import Album, Track
from .spotify_client import SpotifyClient
from .store import Store
from .statuses import TrackStatus


def _load(cfg: Config) -> Store:
    return Store(cfg.db_dir).load()


def cmd_import(args: argparse.Namespace) -> None:
    cfg = Config()
    cfg.validate_credentials()
    if args.no_raw:
        cfg.raw_enabled = False
    store = Store(cfg.db_dir).load()
    client = SpotifyClient(cfg)
    Importer(cfg, store, client).run()


def cmd_import_csv(args: argparse.Namespace) -> None:
    cfg = Config()
    store = Store(cfg.db_dir).load()
    CsvImporter(cfg, store).run(args.directory)


def cmd_dedup(args: argparse.Namespace) -> None:
    cfg = Config()
    store = _load(cfg)
    analysis = DedupAnalyzer(cfg, store).analyze()
    path = DedupAnalyzer(cfg, store).write_report(analysis)
    print(f"Точных групп (ISRC):    {len(analysis['exact_groups'])}")
    print(f"Кандидатов по имени:    {len(analysis['candidate_groups'])}")
    print(
        f"Один трек в нескольких плейлистах (не дубли): "
        f"{analysis['cross_playlist_count']}"
    )
    print(f"Отчёт: {path}")


def cmd_dedup_apply(args: argparse.Namespace) -> None:
    cfg = Config()
    store = _load(cfg)
    stats = DedupApplier(cfg, store).apply()
    print(f"Объединено групп:   {stats['merged_groups']}")
    print(f"Удалено треков:     {stats['removed_tracks']}")
    print(f"Перелинковано ссылок: {stats['remapped_refs']}")


def cmd_status(args: argparse.Namespace) -> None:
    cfg = Config()
    store = _load(cfg)
    counts: Counter[str] = Counter()
    for rec in store.tracks.values():
        for flag in rec.get("statuses", []):
            counts[flag] += 1
    print(f"Треков всего: {len(store.tracks)}")
    for status in TrackStatus:
        print(f"  {status.value:<22} {counts.get(status.value, 0):>6}")
    notes = sum(1 for t in store.tracks.values() if t.get("notes"))
    print(f"  с заметками (notes): {notes:>6}")
    imp = store.meta.get("import", {})
    print("\nПрогресс импорта:")
    print(f"  статус:        {imp.get('status', 'не запускался')}")
    print(f"  лайков:        {imp.get('liked_offset', 0)}/{imp.get('liked_total', '?')}")
    print(
        "  плейлистов:    "
        f"{len(imp.get('playlists_done', []))} обработано"
    )
    print(f"  жанры:         {'обогащены' if imp.get('status') == 'complete' else imp.get('artists_enriched', False)}")


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", "", name.lower())


def cmd_stats(args: argparse.Namespace) -> None:
    cfg = Config()
    store = _load(cfg)
    tracks = store.tracks
    albums = store.albums

    total = len(tracks)
    local = sum(1 for t in tracks.values() if t.get("is_local"))
    with_isrc = sum(1 for t in tracks.values() if t.get("isrc"))
    liked = sum(1 for t in tracks.values() if t.get("liked"))

    by_isrc: dict[str, list[str]] = {}
    for tid, t in tracks.items():
        isrc = t.get("isrc")
        if isrc:
            by_isrc.setdefault(isrc, []).append(tid)
    isrc_dups = {k: v for k, v in by_isrc.items() if len(v) > 1}

    by_key: dict[tuple[str, str], list[str]] = {}
    for tid, t in tracks.items():
        key = (
            _norm_name(t.get("name", "")),
            _norm_name((t.get("artist_names") or [""])[0]),
        )
        by_key.setdefault(key, []).append(tid)
    name_dups = {k: v for k, v in by_key.items() if len(v) > 1}

    album_types = Counter(
        a.get("album_type", "unknown") for a in albums.values()
    )

    orphans = 0
    for pl in store.playlists.values():
        for tid in pl.get("tracks", []):
            if tid not in tracks:
                orphans += 1

    print(f"Треков всего:            {total}")
    print(f"  лайкнутых:             {liked}")
    print(f"  локальных (без ID):    {local}")
    print(f"  с ISRC:                {with_isrc} "
          f"({100 * with_isrc / max(total, 1):.1f}%)")
    print(f"Альбомов:                {len(albums)}")
    print(f"  по типам:              {dict(album_types)}")
    print(f"Исполнителей:            {len(store.artists)}")
    print(f"Плейлистов:              {len(store.playlists)}")
    print(f"  битых ссылок в плейлистах: {orphans}")

    if args.duplicates:
        print("\nДубли по ISRC:")
        for isrc, ids in sorted(isrc_dups.items()):
            names = " | ".join(tracks[i]["name"] for i in ids)
            print(f"  {isrc} -> {names}")
        print("\nКандидаты по имени (имя + первый исполнитель):")
        for key, ids in sorted(name_dups.items()):
            names = " | ".join(tracks[i]["name"] for i in ids)
            print(f"  {key[1]} / {key[0]} -> {names}")
    else:
        print(f"\nДублей по ISRC:           {len(isrc_dups)}")
        print(f"Кандидатов по имени:      {len(name_dups)}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="orpheus", description="Project Orpheus — ядро музыкальной библиотеки"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="Импорт лайков и плейлистов из Spotify")
    p_import.add_argument("--no-raw", action="store_true", help="не сохранять сырые ответы API")
    p_import.set_defaults(func=cmd_import)

    p_csv = sub.add_parser(
        "import-csv", help="Импорт выгрузок Exportify (CSV-файлы) из каталога"
    )
    p_csv.add_argument("directory", help="каталог с *.csv выгрузками Exportify")
    p_csv.set_defaults(func=cmd_import_csv)

    p_dedup = sub.add_parser(
        "dedup", help="Анализ дубликатов и отчёт (без изменений базы)"
    )
    p_dedup.set_defaults(func=cmd_dedup)
    dedup_sub = p_dedup.add_subparsers(dest="dedup_command")

    p_dedup_apply = dedup_sub.add_parser(
        "apply",
        help="Перелинковка плейлистов и удаление неканонических версий",
    )
    p_dedup_apply.set_defaults(func=cmd_dedup_apply)

    p_status = sub.add_parser("status", help="Сводка по статусам треков")
    p_status.set_defaults(func=cmd_status)

    p_stats = sub.add_parser("stats", help="Статистика и контроль качества базы")
    p_stats.add_argument("--duplicates", action="store_true", help="показать списки дублей")
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nПрервано. Прогресс сохранён — повторный запуск продолжит импорт.",
              file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
