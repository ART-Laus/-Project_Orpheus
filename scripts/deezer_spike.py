"""Проверка ARL-токена Deezer: аккаунт, поиск, скачивание трека и альбома.

Шаги (каждый печатает результат):
  1. файл data/cache/deezer_arl.txt — есть ли, длина 192;
  2. deezer.getUserData — вход по ARL, пользователь/страна/подписка;
  3. публичный поиск (работает и без Premium) — выбор трека;
  4. track.getData -> CDN -> скачивание одного трека MP3;
  5. альбом найденного трека — скачивание первых 2 треков альбома;
  6. проверка скачанных файлов (битрейт через mutagen, если установлен).

Файлы кладутся в data/tmp/deezer-spike/. Вывод можно показать целиком —
по нему видно, на каком шаге что не так.

Использование: python scripts/deezer_spike.py [запрос]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orpheus.config import Config
from orpheus.deezer_client import DeezerClient
from orpheus.sources.base import SourceError

ARL_FILE_NAME = "deezer_arl.txt"
SPIKE_DIR_NAME = "deezer-spike"


def _bitrate_of(path: Path) -> str:
    try:
        from mutagen.mp3 import MP3

        info = MP3(path).info
        return f"{info.bitrate // 1000} kbps, {info.length:.0f} c"
    except Exception:
        return f"{path.stat().st_size} байт"


def step(n: int, title: str) -> None:
    print(f"\n=== Шаг {n}: {title}")


def main() -> None:
    cfg = Config()
    arl_file = cfg.data_dir / "cache" / ARL_FILE_NAME
    client = DeezerClient(arl_file=arl_file, request_interval=0.5, stream_interval=0.8)
    out_dir = cfg.data_dir / "tmp" / SPIKE_DIR_NAME
    out_dir.mkdir(parents=True, exist_ok=True)

    step(1, f"ARL-файл {arl_file}")
    if not arl_file.exists():
        print("НЕТ файла. После входа на deezer.com достаньте ARL-куку "
              "(DevTools -> Application -> Cookies -> arl) и сохраните её в", arl_file)
        return
    arl = arl_file.read_text(encoding="utf-8").strip()
    print(f"есть, длина {len(arl)} символов (ожидается 192)")

    step(2, "Вход по ARL (deezer.getUserData)")
    try:
        info = client.account_info()
    except SourceError as exc:
        print(f"ОШИБКА: {exc}")
        print("Если gw-light не отвечает вовсе — возможно, geo-блок Deezer "
              "с этого IP: включите VPN и повторите.")
        return
    print("пользователь:", info)

    query = " ".join(sys.argv[1:]) or "Daft Punk Around the World"
    step(3, f"Публичный поиск: {query!r}")
    try:
        results = client.search(query)
    except SourceError as exc:
        print(f"ОШИБКА: {exc}")
        return
    if not results:
        print("НЕТ результатов поиска")
        return
    r = results[0]
    album_id = str((r.get("album") or {}).get("id") or "")
    print("трек:", r.get("title"), "|", (r.get("artist") or {}).get("name"),
          "|", r.get("duration"), "c | id:", r.get("id"), "| альбом id:", album_id)

    step(4, "Скачивание трека (track.getData -> CDN)")
    track_id = str(r.get("id") or "")
    try:
        media = client.track_media(int(track_id))
        if not media:
            print("НЕТ media-записей — аккаунт Free? Premium должен давать MP3_320")
            return
        print("формат:", media.get("format"), "| quality:", media.get("quality"))
        url = client.stream_url(media)
        dest = out_dir / f"track-{track_id}{client.media_extension(media)}"
        client.download(url, dest)
        print("скачано:", dest, "->", _bitrate_of(dest))
    except SourceError as exc:
        print(f"ОШИБКА: {exc}")
        return

    step(5, "Альбом трека: первые 2 трека")
    if not album_id:
        print("у трека нет альбома — пропускаю")
    else:
        try:
            album = client.album(album_id)
            tracks = (album.get("tracks") or {}).get("data") or []
            print("альбом:", album.get("title"), "| треков:", len(tracks))
            for t in tracks[:2]:
                tid = str(t.get("id") or "")
                title = t.get("title") or "?"
                media = client.track_media(int(tid))
                if not media:
                    print(f"  {title}: нет media (регион?)")
                    continue
                dest = out_dir / f"album-{album_id}-{tid}{client.media_extension(media)}"
                client.download(client.stream_url(media), dest)
                print(f"  {title}: {_bitrate_of(dest)}")
        except SourceError as exc:
            print(f"ОШИБКА: {exc}")

    step(6, "Итог")
    files = sorted(out_dir.glob("*.mp3")) + sorted(out_dir.glob("*.m4a"))
    print(f"скачано файлов: {len(files)} в {out_dir}")
    if files:
        print("Премиум-доступ работает. Можно запускать scripts/deezer_coverage.py")


if __name__ == "__main__":
    main()