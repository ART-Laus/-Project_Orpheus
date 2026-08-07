"""Тесты импорта локальной папки (LocalImporter): матчинг, канонизация,
добавление локальных треков."""

from pathlib import Path

import pytest

from orpheus.config import Config
from orpheus.local_import import (
    LocalImporter,
    clean_title,
    folder_album_key,
    split_artists,
)
from orpheus.models import Album, Track
from orpheus.statuses import TrackStatus
from orpheus.store import Store


def _cfg(tmp_path) -> Config:
    (tmp_path / "config.yaml").write_text(
        "paths:\n  data_dir: data\nlibrary:\n  dir: Library\n  cover_min_size: 640\n",
        encoding="utf-8",
    )
    return Config(project_root=tmp_path)


def _store(tmp_path) -> Store:
    store = Store(tmp_path / "data" / "db").load()
    album = Album(
        spotify_id="alb1",
        name="CARNIVAL DRAGON",
        artist_names=["playingtheangel"],
        album_type="album",
        total_tracks=10,
        images=[
            {
                "url": "https://i.scdn.co/image/ab67616d0000b273deadbeef",
                "width": 640,
                "height": 640,
            }
        ],
        track_ids=[],
    )
    store.albums["alb1"] = album.to_dict()
    tracks = [
        Track(
            spotify_id="t1",
            name="Карнавальный дракон",
            artist_names=["playingtheangel"],
            album_id="alb1",
            album_name="CARNIVAL DRAGON",
            duration_ms=180000,
            track_number=1,
        ),
        Track(  # зацензуренное название — канон должен исправить
            spotify_id="t2",
            name="Space",
            artist_names=["playingtheangel"],
            album_id="alb1",
            album_name="CARNIVAL DRAGON",
            duration_ms=205000,
            track_number=6,
        ),
    ]
    for t in tracks:
        store.tracks[t.spotify_id] = t.to_dict()
    return store


def make_mp3(path: Path, title: str, artist: str = "playingtheangel", seconds: int = 200):
    """Валидный MP3 (кадры + ID3-теги + встроенная обложка)."""
    from mutagen.easyid3 import EasyID3
    from mutagen.id3 import APIC, ID3, PictureType

    path.parent.mkdir(parents=True, exist_ok=True)
    rate = 44100
    frame_bytes = 144 * 320 * 1000 // rate
    frames = int(seconds * rate / 1152)
    data = bytearray()
    for _ in range(frames):
        header = bytes([0xFF, 0xFB, 0xE0, 0x00])
        data += header + b"\x00" * (frame_bytes - 4)
    path.write_bytes(bytes(data))
    tags = EasyID3()
    tags["title"] = title
    tags["artist"] = artist
    tags.save(path)
    id3 = ID3(path)
    id3.add(
        APIC(
            encoding=3,
            mime="image/jpeg",
            type=PictureType.COVER_FRONT,
            desc="Cover",
            data=b"\xff\xd8\xff\xe0fakecover",
        )
    )
    id3.save(path)


def test_clean_title_and_artists():
    assert clean_title("Спиритический сеанс (Дешёвка)") == "Спиритический сеанс (Дешёвка)"
    assert clean_title("Заводи мотор feat. МУККА") == "Заводи мотор"
    assert clean_title("Пир стерв [tag]") == "Пир стерв"
    assert split_artists("A & B, C") == ["A", "B", "C"]
    assert split_artists("Гноев Ковчег (hawaiian, sted.d)") == ["Гноев Ковчег"]


def test_folder_album_key():
    assert folder_album_key("playingtheangel - CARNIVAL DRAGON (2018)") == "carnivaldragon"
    assert (
        folder_album_key("playingtheangel - Пропорция уязвимости (EP) (2020)")
        == "пропорцияуязвимости"
    )


def test_matched_track_canonicalized(tmp_path: Path):
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)

    src = tmp_path / "52201" / "Альбомы" / "playingtheangel - CARNIVAL DRAGON (2018)"
    make_mp3(src / "01. Карнавальный дракон.mp3", "Карнавальный дракон")
    make_mp3(src / "06. Space Cake.mp3", "Space Cake")
    (src / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0")

    importer = LocalImporter(cfg, store, src.parent.parent)
    stats = importer.run()

    assert stats.files == 2
    assert stats.matched == 2
    assert stats.local_added == 0
    # t2 переименован в канон
    assert store.tracks["t2"]["name"] == "Space Cake"
    assert "канон" in store.tracks["t2"]["notes"]
    assert "downloaded" in store.tracks["t2"]["statuses"]
    dest = tmp_path / "Library" / "playingtheangel" / "CARNIVAL DRAGON" / "06. Space Cake.mp3"
    assert dest.exists()


def test_unmatched_becomes_local(tmp_path: Path):
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)

    src = tmp_path / "52201" / "Трекография"
    make_mp3(src / "01. playingtheangel - 100HP.mp3", "100HP")

    importer = LocalImporter(cfg, store, src.parent)
    stats = importer.run()

    assert stats.local_added == 1
    local = [t for t in store.tracks.values() if t.get("is_local")]
    assert len(local) == 1
    track = Track.from_dict(local[0])
    assert track.name == "100HP"
    assert track.artist_names == ["playingtheangel"]
    assert track.album_name == "100HP"
    assert track.is_local
    assert "downloaded" in track.statuses
    assert track.spotify_id.startswith("local:")
    assert store.albums[track.album_id]["album_type"] == "single"


def test_already_downloaded_track_is_replaced(tmp_path: Path):
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)

    # старый зацензуренный файл уже скачан
    old_dir = tmp_path / "Library" / "playingtheangel" / "CARNIVAL DRAGON"
    old_dir.mkdir(parents=True)
    old_file = old_dir / "06. Space.mp3"
    old_file.write_bytes(b"old-censored")
    store.tracks["t2"]["file"] = str(old_file.relative_to(tmp_path))
    store.tracks["t2"]["statuses"] = [
        TrackStatus.DOWNLOADED.value,
        TrackStatus.METADATA_VERIFIED.value,
        TrackStatus.MANUAL_REVIEW.value,
    ]

    src = tmp_path / "52201" / "Альбомы" / "playingtheangel - CARNIVAL DRAGON (2018)"
    make_mp3(src / "06. Space Cake.mp3", "Space Cake")

    importer = LocalImporter(cfg, store, src.parent.parent)
    stats = importer.run()

    assert stats.replaced == 1
    assert not old_file.exists()
    dest = tmp_path / "Library" / "playingtheangel" / "CARNIVAL DRAGON" / "06. Space Cake.mp3"
    assert dest.exists()
    assert store.tracks["t2"]["file"].endswith("06. Space Cake.mp3")


def test_album_pos_matches_moderate_duration_gap(tmp_path: Path):
    """Позиция в совпавшем альбоме матчится при умеренном расхождении
    длительности (цензурная подмена), но с предупреждением."""
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)

    src = tmp_path / "52201" / "Альбомы" / "playingtheangel - CARNIVAL DRAGON (2018)"
    make_mp3(src / "06. Тестостерон.mp3", "Тестостерон", seconds=210)  # vs Space 205 с

    importer = LocalImporter(cfg, store, src.parent.parent)
    stats = importer.run()

    assert stats.matched == 1
    assert stats.matched_by["album+pos+duration"] == 1
    assert store.tracks["t2"]["name"] == "Тестостерон"
    assert stats.local_added == 0


def test_album_pos_matches_moderate_gap_with_warning(tmp_path: Path):
    """Умеренное расхождение (в пределах 30%) — цензурная подмена."""
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)

    src = tmp_path / "52201" / "Альбомы" / "playingtheangel - CARNIVAL DRAGON (2018)"
    make_mp3(src / "06. Тестостерон.mp3", "Тестостерон", seconds=225)  # vs Space 205 с (10%)

    importer = LocalImporter(cfg, store, src.parent.parent)
    stats = importer.run()

    assert stats.matched == 1
    assert stats.matched_by["album+pos"] == 1
    assert store.tracks["t2"]["name"] == "Тестостерон"
    assert any("расхождение длительности" in w for w in stats.warnings)


def test_album_pos_skips_large_duration_gap(tmp_path: Path):
    """Расхождение больше 30% — другой трек (например, сингл с тем же
    именем альбома): файл не матчится, становится локальным."""
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)

    src = tmp_path / "52201" / "Альбомы" / "playingtheangel - CARNIVAL DRAGON (2018)"
    make_mp3(src / "06. Технотроника.mp3", "Технотроника", seconds=300)  # vs Space 205 с (46%)

    importer = LocalImporter(cfg, store, src.parent.parent)
    stats = importer.run()

    assert stats.matched == 0
    assert stats.local_added == 1


def test_unknown_album_becomes_local(tmp_path: Path):
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)

    src = tmp_path / "52201" / "Альбомы" / "playingtheangel - ЧУЖОЙ АЛЬБОМ (2020)"
    make_mp3(src / "06. Совсем другая песня.mp3", "Совсем другая песня")

    importer = LocalImporter(cfg, store, src.parent.parent)
    stats = importer.run()

    assert stats.matched == 0
    assert stats.local_added == 1
