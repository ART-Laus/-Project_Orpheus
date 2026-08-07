"""Resolver: получение файлов для треков/альбомов из источников.

Проходит по источникам в порядке приоритета (config.yaml: sources:),
собирает кандидатов, применяет общую политику качества и скачивает
лучший вариант; при неудаче переходит к следующему кандидату/источнику.

Два режима:
  - resolve_track: per-track источники (slskd, yandex);
  - resolve_album: альбомный режим (rutracker) — скачивает релиз целиком
    и сопоставляет файлы с треками базы.
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import Config
from .models import Album, Track
from .quality import QualityPolicy, filter_and_rank, verify_file
from .sources.base import Candidate, MusicSource, SourceNotSupported

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".ape"}


class Resolver:
    def __init__(self, sources: list[MusicSource], policy: QualityPolicy | None = None):
        self.sources = sources
        self.policy = policy or QualityPolicy()
        self._avail: dict[str, bool] = {}

    def _available(self, src: MusicSource) -> bool:
        """Доступность источника с кешем: проверяем один раз за прогон
        (available() может висеть на сетевом таймауте)."""
        if src.name not in self._avail:
            try:
                self._avail[src.name] = bool(src.available())
            except Exception:
                self._avail[src.name] = False
        return self._avail[src.name]

    # --- per-track ---------------------------------------------------------

    def resolve_track(self, track: Track, staging: Path) -> Path | None:
        """Лучший скачанный и проверенный файл для трека, или None."""
        want_ms = track.duration_ms
        for src in self.sources:
            if not self._available(src):
                continue
            try:
                cands = src.search_track(track)
            except SourceNotSupported:
                continue
            except Exception:
                continue
            for cand in filter_and_rank(cands, self.policy, want_ms)[: self.policy.max_candidates]:
                try:
                    path = src.download_track(cand, staging)
                except Exception:
                    continue
                if path and path.exists() and verify_file(path, track, self.policy):
                    return path
        return None

    # --- per-album ---------------------------------------------------------

    def resolve_album(
        self, album: Album, tracks: list[Track], staging: Path
    ) -> dict[str, Path]:
        """Скачать релиз и сопоставить файлы с треками: {spotify_id: path}."""
        for src in self.sources:
            if not self._available(src):
                continue
            try:
                cands = src.search_album(album)
            except SourceNotSupported:
                continue
            except Exception:
                continue
            for cand in filter_album_candidates(cands)[: self.policy.max_candidates]:
                try:
                    folder = src.download_album(cand, staging)
                except Exception:
                    continue
                if not folder or not folder.exists():
                    continue
                matched = match_files_to_tracks(folder, tracks, self.policy)
                if matched:
                    return matched
        return {}


def filter_album_candidates(cands: list[Candidate]) -> list[Candidate]:
    """Ранжирование альбомных кандидатов: FLAC > 320 > остальное, потом размер."""
    def key(c: Candidate) -> tuple[int, int]:
        from .quality import format_rank

        fmt = format_rank(c.extension)
        if fmt >= 3:
            quality = 2 if fmt == 4 else 1
        else:
            quality = 0
        return (quality, c.size)

    out = [c for c in cands if c.extension in AUDIO_EXTS]
    out.sort(key=key, reverse=True)
    return out


_NUM_PREFIX = re.compile(r"^(\d{1,3})\s*[-._\s]*(.*)$")
_NON_WORD = re.compile(r"[^a-zа-яё0-9]+")


def _norm_title(text: str) -> str:
    """Нормализация названия для сравнения: без номера дорожки, нижний регистр."""
    text = text or ""
    m = _NUM_PREFIX.match(text.strip())
    if m:
        text = m.group(2)
    return _NON_WORD.sub("", text.lower())


def match_files_to_tracks(
    folder: Path, tracks: list[Track], policy: QualityPolicy | None = None
) -> dict[str, Path]:
    """Сопоставление файлов скачанного релиза с треками базы.

    Сначала по номеру дорожки в имени файла ("01. Песня"), затем по
    нормализованному названию. Файл засчитывается после проверки качества.
    """
    policy = policy or QualityPolicy()
    files = [
        p
        for p in folder.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS
    ]
    by_number: dict[int, list[Path]] = {}
    by_name: dict[str, Path] = {}
    for p in files:
        stem = p.stem
        m = _NUM_PREFIX.match(stem.strip())
        if m and m.group(1).isdigit():
            by_number.setdefault(int(m.group(1)), []).append(p)
        by_name.setdefault(_norm_title(stem), p)

    result: dict[str, Path] = {}
    used: set[Path] = set()
    for track in tracks:
        cand_files: list[Path] = []
        if track.track_number:
            cand_files = [p for p in by_number.get(track.track_number, []) if p not in used]
        if not cand_files:
            p = by_name.get(_norm_title(track.name))
            if p and p not in used:
                cand_files = [p]
        for p in cand_files:
            if verify_file(p, track, policy):
                result[track.spotify_id] = p
                used.add(p)
                break
    return result


def build_sources(cfg: Config) -> list[MusicSource]:
    """Создание источников по config.yaml (sources:) в порядке приоритета."""
    from .qbit_client import QbitClient
    from .slskd_client import SlskdClient
    from .sources.rutracker import RutrackerClient, RutrackerSource
    from .sources.slskd import SlskdSource

    sources: list[MusicSource] = []
    for section in cfg.sources_config:
        name = section.get("name", "")
        if not section.get("enabled", True):
            continue
        if name == "slskd":
            sources.append(
                SlskdSource(
                    base_url=section.get("base_url", cfg.slskd_base_url),
                    api_key=SlskdClient.api_key_from_config(cfg.slskd_config),
                    downloads_dirs=[cfg.data_dir / "downloads"],
                )
            )
        elif name == "rutracker":
            qbit = section.get("qbit", {})
            sources.append(
                RutrackerSource(
                    client=RutrackerClient(
                        base_url=section.get("base_url", "https://rutracker.org/forum"),
                        cache_dir=cfg.data_dir / "cache",
                        timeout_s=10,
                    ),
                    qbit=QbitClient(
                        base_url=qbit.get("url", "http://localhost:8080"),
                        username=qbit.get("username", "admin"),
                        password=qbit.get("password", ""),
                    ),
                    torrents_dir=cfg.data_dir / "torrents",
                    min_quality=section.get("min_quality", "flac"),
                )
            )
        elif name == "yandex":
            from .sources.yandex import YandexSource

            sources.append(
                YandexSource(
                    token=section.get("token", ""),
                    token_file=cfg.data_dir / "cache" / "yandex_token.txt",
                )
            )
        else:
            print(f"предупреждение: источник {name!r} неизвестен, пропущен")
    return sources
