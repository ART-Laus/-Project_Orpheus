"""Модели данных базы проекта.

Ключи всех коллекций — Spotify ID (для локальных треков — синтетический ключ).
Плейлисты содержат только ссылки на треки, никогда не копии файлов.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field

from .statuses import default_statuses


def _norm(value, default):
    return default if value is None else value


@dataclass
class Image:
    url: str = ""
    width: int | None = None
    height: int | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Image":
        return cls(url=d.get("url", ""), width=d.get("width"), height=d.get("height"))

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Track:
    """Главная сущность: одна композиция = один трек."""

    spotify_id: str
    name: str
    artist_ids: list[str] = field(default_factory=list)
    artist_names: list[str] = field(default_factory=list)
    album_id: str = ""
    album_name: str = ""
    duration_ms: int = 0
    disc_number: int = 0
    track_number: int = 0
    explicit: bool = False
    isrc: str | None = None
    is_local: bool = False
    popularity: int = 0
    statuses: list[str] = field(default_factory=default_statuses)
    notes: str = ""
    liked: bool = False
    playlists: list[str] = field(default_factory=list)
    added_at: str | None = None
    audio_features: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "Track":
        return cls(
            spotify_id=d["spotify_id"],
            name=_norm(d.get("name"), ""),
            artist_ids=_norm(d.get("artist_ids"), []),
            artist_names=_norm(d.get("artist_names"), []),
            album_id=_norm(d.get("album_id"), ""),
            album_name=_norm(d.get("album_name"), ""),
            duration_ms=_norm(d.get("duration_ms"), 0),
            disc_number=_norm(d.get("disc_number"), 0),
            track_number=_norm(d.get("track_number"), 0),
            explicit=_norm(d.get("explicit"), False),
            isrc=d.get("isrc"),
            is_local=_norm(d.get("is_local"), False),
            popularity=_norm(d.get("popularity"), 0),
            statuses=_norm(d.get("statuses"), default_statuses()),
            notes=_norm(d.get("notes"), ""),
            liked=_norm(d.get("liked"), False),
            playlists=_norm(d.get("playlists"), []),
            added_at=d.get("added_at"),
            audio_features=_norm(d.get("audio_features"), {}),
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Album:
    spotify_id: str
    name: str
    artist_ids: list[str] = field(default_factory=list)
    artist_names: list[str] = field(default_factory=list)
    album_type: str = ""  # album / single / compilation
    release_date: str = ""
    release_date_precision: str = ""
    total_tracks: int = 0
    images: list[Image] = field(default_factory=list)
    track_ids: list[str] = field(default_factory=list)
    label: str = ""
    genres: list[str] = field(default_factory=list)
    copyrights: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Album":
        return cls(
            spotify_id=d["spotify_id"],
            name=_norm(d.get("name"), ""),
            artist_ids=_norm(d.get("artist_ids"), []),
            artist_names=_norm(d.get("artist_names"), []),
            album_type=_norm(d.get("album_type"), ""),
            release_date=_norm(d.get("release_date"), ""),
            release_date_precision=_norm(d.get("release_date_precision"), ""),
            total_tracks=_norm(d.get("total_tracks"), 0),
            images=[Image.from_dict(i) for i in _norm(d.get("images"), [])],
            track_ids=_norm(d.get("track_ids"), []),
            label=_norm(d.get("label"), ""),
            genres=_norm(d.get("genres"), []),
            copyrights=_norm(d.get("copyrights"), ""),
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Artist:
    spotify_id: str
    name: str
    genres: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Artist":
        return cls(
            spotify_id=d["spotify_id"],
            name=_norm(d.get("name"), ""),
            genres=_norm(d.get("genres"), []),
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Playlist:
    """Плейлист — только ссылки на Spotify ID треков, без копий."""

    spotify_id: str
    name: str
    description: str = ""
    owner: str = ""
    snapshot_id: str = ""
    tracks: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Playlist":
        return cls(
            spotify_id=d["spotify_id"],
            name=_norm(d.get("name"), ""),
            description=_norm(d.get("description"), ""),
            owner=_norm(d.get("owner"), ""),
            snapshot_id=_norm(d.get("snapshot_id"), ""),
            tracks=_norm(d.get("tracks"), []),
        )

    def to_dict(self) -> dict:
        return asdict(self)


def local_track_id(name: str, artist_names: list[str], duration_ms: int) -> str:
    """Синтетический ключ для локальных треков без Spotify ID."""
    raw = f"{name}|{','.join(artist_names)}|{duration_ms}"
    return "local:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def local_album_id(name: str, artist_names: list[str]) -> str:
    raw = f"{name}|{','.join(artist_names)}"
    return "local-album:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
