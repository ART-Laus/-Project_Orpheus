"""Источники музыки: адаптеры над внешними сервисами.

Каждый источник реализует общий интерфейс MusicSource; Resolver ходит в
источники только через этот интерфейс, поэтому источник можно заменить
или добавить без изменения остального кода (база не хранит URL источников).
"""

from .base import Candidate, MusicSource, SourceError, SourceNotSupported

__all__ = ["Candidate", "MusicSource", "SourceError", "SourceNotSupported"]
