"""Статусы треков — жизненный цикл композиции в библиотеке."""

from __future__ import annotations

from enum import Enum


class TrackStatus(str, Enum):
    """Флаги состояния трека.

    Один трек может нести сразу несколько флагов одновременно.

    downloaded             — файл скачан в медиатеку
    metadata_verified      — теги проверены и приведены к канону
    cover_verified         — встроенная обложка проверена (>= 1000x1000)
    canonical_version      — хранится каноническая (альбомная) версия
    manual_review          — требует ручного решения (по умолчанию для всех)
    replaced_with_original — заменён на оригинальную версию вручную
    """

    DOWNLOADED = "downloaded"
    METADATA_VERIFIED = "metadata_verified"
    COVER_VERIFIED = "cover_verified"
    CANONICAL_VERSION = "canonical_version"
    MANUAL_REVIEW = "manual_review"
    REPLACED_WITH_ORIGINAL = "replaced_with_original"


DEFAULT_STATUSES: list[str] = [TrackStatus.MANUAL_REVIEW.value]


def default_statuses() -> list[str]:
    """Список статусов по умолчанию для нового трека (копия)."""
    return list(DEFAULT_STATUSES)


def has_status(statuses: list[str], status: TrackStatus) -> bool:
    return status.value in statuses


def add_status(statuses: list[str], status: TrackStatus) -> list[str]:
    if status.value not in statuses:
        statuses.append(status.value)
    return statuses
