"""CLI: orpheus import | status | stats."""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from .config import Config, ConfigError
from .csv_importer import CsvImporter
from .dedup import DedupAnalyzer, DedupApplier, normalize_key
from .downloader import DownloadOptions, Downloader
from .importer import Importer
from .models import Album, Track
from .quality import QualityPolicy
from .resolver import Resolver, build_sources
from .sources.rutracker import CAPTCHA_IMAGE, RutrackerClient
from .spotify_client import SpotifyClient
from .yandex_client import AUTHORIZE_URL, YandexClient
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
    return normalize_key(name)


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


def cmd_download(args: argparse.Namespace) -> None:
    cfg = Config()
    store = _load(cfg)
    policy = QualityPolicy(
        min_mp3_bitrate=cfg.min_mp3_bitrate,
        min_aac_bitrate=cfg.min_aac_bitrate,
        duration_tolerance_s=cfg.duration_tolerance_s,
        verify_tolerance_s=cfg.verify_tolerance_s,
        max_candidates=cfg.max_candidates,
    )
    resolver = Resolver(build_sources(cfg), policy)
    opts = DownloadOptions(library_dir=cfg.library_dir, cover_min_size=cfg.cover_min_size)
    if args.dry_run:
        opts.library_dir = cfg.root / "data" / "dry-run-library"
        print(f"DRY-RUN: файлы будут собраны в {opts.library_dir} (не в Library/)")
    stats = Downloader(cfg, store, resolver, opts).run(limit=args.limit, name_filter=args.query)
    print(f"Найдено кандидатов: {stats['found']}")
    print(f"Скачано:            {stats['downloaded']}")
    print(f"Пропущено (скачано ранее): {stats['skipped']}")
    print(f"Не удалось:          {stats['failed']}")
    if not args.dry_run:
        store.save_all()


def cmd_import_local(args: argparse.Namespace) -> None:
    from .local_import import LocalImporter

    cfg = Config()
    store = _load(cfg)
    importer = LocalImporter(cfg, store, args.directory)
    stats = importer.run()
    print(f"Файлов в папке:        {stats.files}")
    print(f"Сматчено с базой:      {stats.matched}")
    for how, count in sorted(stats.matched_by.items()):
        print(f"  {how}: {count}")
    print(f"Канонизировано названий: {stats.canonicalized}")
    print(f"Заменено старых файлов:  {stats.replaced}")
    print(f"Добавлено локальных:   {stats.local_added}")
    print(f"Пропущено локальных:   {stats.local_skipped}")
    if stats.errors:
        print(f"Ошибок: {len(stats.errors)}")
        for err in stats.errors[:20]:
            print(f"  {err}")
    if args.dry_run:
        print("DRY-RUN: база не изменена")
    else:
        store.save_all()
        print("База сохранена.")


def cmd_mark_canonical(args: argparse.Namespace) -> None:
    """Пометить канон из 52201 статусом canonical_version."""
    from .statuses import add_status as add_flag

    cfg = Config()
    store = _load(cfg)
    marked = 0
    for tid, rec in store.tracks.items():
        if rec.get("is_local"):
            continue
        file = rec.get("file", "")
        notes = rec.get("notes") or ""
        artists = rec.get("artist_names") or []
        canon = (
            "канон из 52201" in notes
            or file.startswith("Library/playingtheangel/")
            or any("playingtheangel" in a.lower() for a in artists)
        )
        if not canon:
            continue
        statuses = add_flag(list(rec.get("statuses", [])), TrackStatus.CANONICAL_VERSION)
        if statuses != rec.get("statuses"):
            rec["statuses"] = statuses
            store.tracks[tid] = rec
            marked += 1
    store.save_all()
    print(f"Помечено канонических треков: {marked}. База сохранена.")


def cmd_reorganize(args: argparse.Namespace) -> None:
    from .reorganize import LibraryReorganizer

    cfg = Config()
    store = _load(cfg)
    reorg = LibraryReorganizer(cfg, store)
    stats = reorg.run(dry_run=args.dry_run)
    print(f"Альбомных папок:      {stats.albums}")
    print(f"Синглов/EP (в 'Синглы и EP'): {stats.singles}")
    print(f"Переносов файлов:     {stats.moved}")
    print(f"Слито записей артистов: {stats.artist_merged}")
    print(f"Удалено записей-дублей: {stats.artists_removed}")
    if stats.ambiguous:
        print(f"\nНеоднозначные папки (не тронуты, {len(stats.ambiguous)}):")
        for p in sorted(stats.ambiguous)[:30]:
            print(f"  {p}")
    if stats.untouched:
        print(f"\nОставлено как есть ({len(stats.untouched)}):")
        for p in sorted(stats.untouched)[:20]:
            print(f"  {p}")
    if stats.errors:
        print(f"\nОшибок: {len(stats.errors)}")
        for err in stats.errors[:20]:
            print(f"  {err}")
    if args.dry_run:
        print("\nDRY-RUN: ничего не изменено")
    else:
        print("\nГотово. База сохранена.")


def cmd_sources(args: argparse.Namespace) -> None:
    cfg = Config()
    sources = build_sources(cfg)
    if not sources:
        print("Источников нет (все отключены или не реализованы).")
        return
    print("Источники (в порядке приоритета):")
    for src in sources:
        mode = "альбомы+треки" if getattr(src, "album_capable", False) else "треки"
        try:
            ok = src.available()
        except Exception:
            ok = False
        print(f"  {src.name:<12} {mode:<14} {'доступен' if ok else 'недоступен'}")


def cmd_rutracker_login(args: argparse.Namespace) -> None:
    cfg = Config()
    section = next((s for s in cfg.sources_config if s.get("name") == "rutracker"), None)
    if not section:
        print("Источник rutracker не найден в config.yaml", file=sys.stderr)
        sys.exit(1)
    login = section.get("login") or ""
    password = section.get("password") or ""
    if not login:
        print("Впишите login/password в config.yaml (sources.rutracker)", file=sys.stderr)
        sys.exit(1)
    client = RutrackerClient(
        base_url=section.get("base_url", "https://rutracker.org/forum"),
        cache_dir=cfg.data_dir / "cache",
        proxy=section.get("proxy", ""),
    )
    status = client.login(login, password)
    while status == "captcha":
        print(
            f"Нужна капча: откройте {cfg.data_dir / 'cache' / CAPTCHA_IMAGE} "
            "и введите код с картинки:",
            end=" ",
        )
        code = input().strip()
        status = client.login(login, password, captcha_code=code)
    if status == "ok":
        print("Вход выполнен; сессия сохранена.")
    else:
        print("Вход не удался (проверьте логин/пароль).", file=sys.stderr)
        sys.exit(1)


def cmd_yandex_login(args: argparse.Namespace) -> None:
    cfg = Config()
    print("1. Открой в браузере (тот, где залогинен Яндекс):")
    print("   " + AUTHORIZE_URL)
    print("2. Разреши доступ и скопируй access_token из адресной строки")
    print("   (после #access_token=... до следующего & или конца строки).")
    token = args.token or input("Вставь токен: ").strip()
    if "access_token=" in token:
        token = token.split("access_token=", 1)[1].split("&", 1)[0].strip()
    if not token:
        print("Токен пустой, прерываю.", file=sys.stderr)
        sys.exit(1)
    token_file = cfg.data_dir / "cache" / "yandex_token.txt"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(token, encoding="utf-8")
    client = YandexClient(token=token)
    try:
        status = client.account_status()
    except Exception as exc:
        token_file.unlink(missing_ok=True)
        print(f"Токен не принят: {exc}", file=sys.stderr)
        sys.exit(1)
    account = status.get("account") or {}
    permissions = status.get("permissions") or {}
    high_quality = "high-quality" in (permissions.get("values") or [])
    sub = status.get("subscription") or {}
    has_sub = bool(sub.get("autoRenewable"))
    print(f"Токен сохранён: {token_file}")
    print(f"Аккаунт: {account.get('login', '?')} (uid {account.get('uid', '?')})")
    print(
        "Яндекс Плюс: "
        + (
            f"{'есть' if has_sub else 'нет'}"
            + (", high-quality = 320 kbps доступен" if high_quality else ", 320 kbps недоступен")
        )
    )


def _nicotine_section(cfg: Config) -> dict:
    section = next((s for s in cfg.sources_config if s.get("name") == "nicotine"), {})
    if not section or not section.get("enabled", True):
        print("Источник nicotine не найден или отключён в config.yaml", file=sys.stderr)
        sys.exit(1)
    return section


def cmd_nicotine_bridge(args: argparse.Namespace) -> None:
    """Запуск моста Nicotine+ (headless Soulseek) в текущем терминале."""
    from .nicotine.bridge import Bridge, BridgeError

    cfg = Config()
    section = _nicotine_section(cfg)
    bridge = Bridge(
        host=section.get("host", "127.0.0.1"),
        port=section.get("port", 5390),
        username=section.get("username", ""),
        password=section.get("password", ""),
        data_dir=str(cfg.data_dir / "nicotine"),
        download_dir=str(cfg.data_dir / "tmp" / "nicotine"),
    )
    try:
        sys.exit(bridge.run())
    except BridgeError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)


def cmd_nicotine_status(args: argparse.Namespace) -> None:
    """Статус моста: запущен ли, подключён ли к Soulseek."""
    from .nicotine_client import NicotineClient

    cfg = Config()
    section = _nicotine_section(cfg)
    client = NicotineClient(
        base_url=f"http://{section.get('host', '127.0.0.1')}:{section.get('port', 5390)}"
    )
    resp = client.ping()
    if resp is None:
        print("Мост Nicotine+ не запущен (запуск: orpheus nicotine bridge, или поднимется сам)")
        return
    state = "подключён к Soulseek" if resp.get("connected") else "запущен, но НЕ подключён"
    print(f"Мост Nicotine+ работает: {state} (аккаунт {resp.get('username', '?')})")


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

    p_download = sub.add_parser(
        "download", help="Скачивание музыки через Resolver (источники из config.yaml) в Library/"
    )
    p_download.add_argument(
        "--limit",
        type=int,
        default=0,
        help="максимум попыток за запуск (0 = без лимита): альбомов в альбомном режиме, треков в поштучном",
    )
    p_download.add_argument(
        "--query", default="", help="скачивать только треки, где имя/исполнитель содержит строку"
    )
    p_download.add_argument(
        "--dry-run", action="store_true", help="скачать без записи статусов и перемещения в Library"
    )
    p_download.set_defaults(func=cmd_download)

    p_local = sub.add_parser(
        "import-local",
        help="Импорт локальной папки с аудио (канон): файлы в Library/, треки матчатся с базой",
    )
    p_local.add_argument(
        "directory", help="папка с аудиофайлами (рекурсивно)"
    )
    p_local.add_argument(
        "--dry-run", action="store_true", help="показать, что будет импортировано, без изменений базы"
    )
    p_local.set_defaults(func=cmd_import_local)

    p_reorg = sub.add_parser(
        "reorganize",
        help="Реорганизация Library/: альбомы папками, синглы и EP в 'Синглы и EP', слияние артистов",
    )
    p_reorg.add_argument(
        "--dry-run", action="store_true", help="показать план без изменений"
    )
    p_reorg.set_defaults(func=cmd_reorganize)

    p_mark = sub.add_parser(
        "mark-canonical",
        help="Пометить канонические треки (из 52201) статусом canonical_version",
    )
    p_mark.set_defaults(func=cmd_mark_canonical)

    p_sources = sub.add_parser("sources", help="Список источников и их доступность")
    p_sources.set_defaults(func=cmd_sources)

    p_rt = sub.add_parser("rutracker", help="Работа с источником RuTracker")
    rt_sub = p_rt.add_subparsers(dest="rutracker_command", required=True)
    p_rt_login = rt_sub.add_parser("login", help="Вход в аккаунт (капча решается один раз)")
    p_rt_login.set_defaults(func=cmd_rutracker_login)

    p_ya = sub.add_parser("yandex", help="Работа с источником Яндекс.Музыки")
    ya_sub = p_ya.add_subparsers(dest="yandex_command", required=True)
    p_ya_login = ya_sub.add_parser(
        "login", help="Получить OAuth-токен и сохранить его в data/cache/yandex_token.txt"
    )
    p_ya_login.add_argument("--token", default="", help="токен (иначе вводится вручную)")
    p_ya_login.set_defaults(func=cmd_yandex_login)

    p_ni = sub.add_parser("nicotine", help="Мост Nicotine+ (headless Soulseek, WSL)")
    ni_sub = p_ni.add_subparsers(dest="nicotine_command", required=True)
    p_ni_bridge = ni_sub.add_parser(
        "bridge", help="Запустить мост в текущем терминале (Ctrl+C — остановка)"
    )
    p_ni_bridge.set_defaults(func=cmd_nicotine_bridge)
    p_ni_status = ni_sub.add_parser("status", help="Статус моста и подключения к Soulseek")
    p_ni_status.set_defaults(func=cmd_nicotine_status)

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
