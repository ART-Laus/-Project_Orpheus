"""Базовые типы слоя источников: кандидат на скачивание и протокол MusicSource.

Источник — адаптер над внешним сервисом (Soulseek/slskd, RuTracker,
Яндекс.Музыка, ...). Реализация сама знает, как искать и качать; политику
качества (фильтр/ранжирование) применяет Resolver из orpheus.quality.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import Album, Track


class SourceError(RuntimeError):
    """Ошибка источника: недоступен, отказ сервиса, сетевые проблемы."""


class SourceNotSupported(SourceError):
    """Режим (по трекам / по альбомам) не поддерживается этим источником."""


@dataclass
class Candidate:
    """Один найденный вариант скачивания: трек или целый альбом-релиз.

    Дополнительные данные, нужные источнику для скачивания
    (username, id темы, magnet и т.п.), складываются в extra.
    """

    source: str
    filename: str = ""
    size: int = 0
    bitrate: int = 0
    length_s: int = 0
    extension: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class MusicSource(ABC):
    """Абстрактный источник музыки.

    Минимальная реализация: name, available(), search_track(), download_track().
    Альбомный режим (search_album/download_album) — опционально: включите
    album_capable = True и реализуйте методы.
    """

    name: str = ""
    album_capable: bool = False

    def available(self) -> bool:
        """Быстрая проверка доступности (демон жив, VPN поднят, токен есть)."""
        return True

    def search_track(self, track: Track) -> list[Candidate]:
        """Поиск вариантов скачивания трека."""
        raise SourceNotSupported(f"источник {self.name}: поиск по трекам не поддерживается")

    def download_track(self, cand: Candidate, dest_dir: Path) -> Path | None:
        """Скачать трек; вернуть путь к готовому файлу или None при неудаче."""
        raise SourceNotSupported(f"источник {self.name}: скачивание треков не поддерживается")

    def search_album(self, album: Album) -> list[Candidate]:
        """Поиск вариантов скачивания целого альбома (релиза)."""
        raise SourceNotSupported(f"источник {self.name}: поиск по альбомам не поддерживается")

    def download_album(self, cand: Candidate, dest_dir: Path) -> Path | None:
        """Скачать альбом; вернуть путь к папке с файлами или None при неудаче."""
        raise SourceNotSupported(f"источник {self.name}: скачивание альбомов не поддерживается")
