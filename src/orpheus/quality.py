"""Общая политика качества: фильтрация и ранжирование кандидатов.

Единое место для правил «FLAC > нативный AAC/M4A > MP3 >= 320»:
и Resolver, и источники используют его, чтобы поведение не зависело
от конкретного источника.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Track
from .sources.base import Candidate

# Приоритет форматов: выше — лучше (файл сохраняем как есть, без пережима)
FORMAT_RANK = {
    ".flac": 4,
    ".wav": 4,
    ".ape": 4,
    ".m4a": 3,
    ".aac": 3,
    ".mp3": 2,
    ".ogg": 1,
    ".opus": 1,
}


@dataclass
class QualityPolicy:
    """Пороги качества и допуски при скачивании."""

    min_mp3_bitrate: int = 320
    min_aac_bitrate: int = 256
    duration_tolerance_s: int = 5
    verify_tolerance_s: int = 10
    max_candidates: int = 5


def format_rank(extension: str) -> int:
    return FORMAT_RANK.get((extension or "").lower(), 0)


def estimate_bitrate(size: int, want_ms: int) -> int:
    """Оценка битрейта по размеру файла и ожидаемой длительности."""
    if want_ms <= 0 or not size:
        return 0
    return int(size * 8 / (want_ms / 1000))


def candidate_bitrate(cand: Candidate, want_ms: int) -> int:
    return cand.bitrate or estimate_bitrate(cand.size, want_ms)


def ok_duration(cand: Candidate, want_ms: int, tolerance_s: int) -> bool:
    """Длительность совпадает с ожидаемой (пустое значение — пропускаем)."""
    if not cand.length_s:
        return True
    return abs(cand.length_s * 1000 - want_ms) <= tolerance_s * 1000


def ok_bitrate(cand: Candidate, policy: QualityPolicy, want_ms: int) -> bool:
    """Формат/битрейт допустимы: FLAC любой, AAC >= мин, MP3 >= 320."""
    ext = (cand.extension or "").lower()
    if ext == ".flac":
        return True
    if ext in (".m4a", ".aac"):
        return candidate_bitrate(cand, want_ms) >= policy.min_aac_bitrate
    if ext == ".mp3":
        return candidate_bitrate(cand, want_ms) >= policy.min_mp3_bitrate
    return False


def quality_score(cand: Candidate, want_ms: int, album_name: str = "") -> tuple[int, int, int]:
    """Сортировочный кортеж (формат, битрейт, штраф): больше — лучше."""
    fmt = format_rank(cand.extension)
    penalty = 1 if album_name and album_name.lower() in cand.filename.lower() else 0
    return (fmt, candidate_bitrate(cand, want_ms), penalty)


def filter_and_rank(
    cands: list[Candidate],
    policy: QualityPolicy,
    want_ms: int,
    album_name: str = "",
) -> list[Candidate]:
    """Общий фильтр (длительность + качество) и сортировка по убыванию качества."""
    out = []
    for c in cands:
        if not ok_duration(c, want_ms, policy.duration_tolerance_s):
            continue
        if not ok_bitrate(c, policy, want_ms):
            continue
        out.append(c)
    out.sort(key=lambda c: quality_score(c, want_ms, album_name), reverse=True)
    return out


def verify_file(path, track: Track, policy: QualityPolicy) -> dict | None:
    """Проверка скачанного файла mutagen'ом: читается, длительность совпадает,
    битрейт не ниже порога. Возвращает инфо или None при несоответствии."""
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(path, easy=False)
        if audio is None:
            return None
    except Exception:
        return None
    length_s = getattr(audio.info, "length", 0)
    if abs(length_s - track.duration_ms / 1000) > policy.verify_tolerance_s:
        return None
    info = {
        "length_s": length_s,
        "bitrate": int(getattr(audio.info, "bitrate", 0)),
        "format": path.suffix.lower().lstrip("."),
    }
    if path.suffix.lower() == ".mp3" and info["bitrate"] < policy.min_mp3_bitrate - 32:
        return None
    return info
