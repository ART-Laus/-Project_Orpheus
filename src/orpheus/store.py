"""Хранилище базы проекта: пять JSON-файлов в data/db.

spotify.json    — метаданные выгрузки (версия, даты, прогресс импорта)
artists.json    — исполнители (ключ: Spotify ID)
albums.json     — альбомы (ключ: Spotify ID)
playlists.json  — плейлисты, только ссылки на треки (ключ: Spotify ID)
tracks.json     — треки + статусы (ключ: Spotify ID)

Все операции идемпотентны: повторная запись или upsert не дублируют данные.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

COLLECTIONS = ("artists", "albums", "playlists", "tracks")
META_FILE = "spotify.json"
DB_VERSION = 1


class Store:
    def __init__(self, db_dir: Path | str):
        self.db_dir = Path(db_dir)
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.meta: dict[str, Any] = {"version": DB_VERSION}
        self.artists: dict[str, dict] = {}
        self.albums: dict[str, dict] = {}
        self.playlists: dict[str, dict] = {}
        self.tracks: dict[str, dict] = {}

    # --- загрузка / сохранение -------------------------------------------

    def load(self) -> "Store":
        meta_path = self.db_dir / META_FILE
        if meta_path.exists():
            self.meta = json.loads(meta_path.read_text(encoding="utf-8"))
        for name in COLLECTIONS:
            path = self.db_dir / f"{name}.json"
            if path.exists():
                setattr(self, name, json.loads(path.read_text(encoding="utf-8")))
        return self

    def save_all(self) -> None:
        self._write(META_FILE, self.meta)
        for name in COLLECTIONS:
            self._write(f"{name}.json", getattr(self, name))

    def save_meta(self) -> None:
        self._write(META_FILE, self.meta)

    def _write(self, filename: str, data: Any) -> None:
        path = self.db_dir / filename
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        tmp.replace(path)

    # --- upsert (идимпотентное добавление) --------------------------------

    def upsert(self, collection: str, key: str, value: dict) -> None:
        assert collection in COLLECTIONS
        getattr(self, collection)[key] = value

    def get(self, collection: str, key: str) -> dict | None:
        return getattr(self, collection, {}).get(key)

    # --- счётчики --------------------------------------------------------

    def counts(self) -> dict[str, int]:
        return {
            "artists": len(self.artists),
            "albums": len(self.albums),
            "playlists": len(self.playlists),
            "tracks": len(self.tracks),
        }
