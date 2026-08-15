"""Тесты загрузчика: ранжирование, скачивание, теги, раскладка, статусы."""

import json
import shutil
from pathlib import Path

import pytest

from orpheus.config import Config
from orpheus.downloader import Downloader, DownloadOptions
from orpheus.models import Album, Track
from orpheus.quality import QualityPolicy, filter_and_rank
from orpheus.resolver import Resolver
from orpheus.sources.base import Candidate, MusicSource
from orpheus.statuses import TrackStatus
from orpheus.store import Store

ID = "a" * 22
ALBUM = "b" * 22
ARTIST = "c" * 22


def make_mp3(path, duration_s=2.0, bitrate=320):
    """Генерация валидного тихого MP3 (MPEG1 Layer3) без внешних инструментов."""
    rate = 44100
    frame_bytes = 144 * bitrate * 1000 // rate
    frames = int(duration_s * rate / 1152)
    data = bytearray()
    for i in range(frames):
        br_idx = 14 if bitrate >= 320 else (13 if bitrate >= 256 else 9)  # 320/256/128
        header = bytes([0xFF, 0xFB, (br_idx << 4) | 0x00, 0x00])
        data += header
        data += b"\x00" * (frame_bytes - 4)
    path.write_bytes(bytes(data))


def _track(**overrides):
    base = dict(
        spotify_id=ID,
        name="Песня",
        artist_ids=[ARTIST],
        artist_names=["Исполнитель"],
        album_id=ALBUM,
        album_name="Альбом",
        duration_ms=2000,
        track_number=1,
        liked=True,
        statuses=["manual_review"],
    )
    base.update(overrides)
    return Track(**base)


def _album(**overrides):
    base = dict(spotify_id=ALBUM, name="Альбом", release_date="2020-05-01", images=[])
    base.update(overrides)
    return Album(**base)


def _cfg(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "paths:\n  data_dir: data\nlibrary:\n  dir: Library\n", encoding="utf-8"
    )
    return Config(project_root=tmp_path)


def _store(tmp_path):
    store = Store(tmp_path / "db")
    store.upsert("tracks", ID, _track().to_dict())
    store.upsert("albums", ALBUM, _album().to_dict())
    store.upsert("artists", ARTIST, {"spotify_id": ARTIST, "name": "Исполнитель", "genres": []})
    store.upsert(
        "playlists", "liked", {"spotify_id": "liked", "name": "Liked Songs", "tracks": [ID]}
    )
    return store


def _cand(filename, size, bitrate=320, length_s=2, username="peer"):
    return Candidate(
        source="fake",
        filename=filename,
        size=size,
        bitrate=bitrate,
        length_s=length_s,
        extension=Path(filename).suffix.lower(),
        extra={"username": username},
    )


def _policy(**overrides):
    base = dict(min_mp3_bitrate=320, min_aac_bitrate=256, duration_tolerance_s=5)
    base.update(overrides)
    return QualityPolicy(**base)


def _resolver(sources, policy=None):
    return Resolver(sources, policy or _policy())


class FakeSource(MusicSource):
    """Источник-заглушка: кандидаты задаются вручную, «скачивание» — копия файла."""

    name = "fake"

    def __init__(self, candidates, download_fixture=None, downloads_dir=None, available=True):
        self.candidates = list(candidates)
        self.download_fixture = download_fixture
        self.downloads_dir = downloads_dir
        self.enqueued = []
        self._available = available

    def available(self):
        return self._available

    def search_track(self, track):
        return list(self.candidates)

    def download_track(self, cand, dest_dir):
        self.enqueued.append((cand.extra.get("username", ""), cand.filename, cand.size))
        if self.download_fixture is None:
            return None
        dest = self.downloads_dir / Path(cand.filename).name
        shutil.copy2(self.download_fixture, dest)
        return dest


def test_ranking_prefers_flac_then_320():
    cands = [
        _cand("/a/Песня.mp3", 11_000_000, bitrate=320),
        _cand("/a/Песня.flac", 30_000_000, bitrate=0),
        _cand("/a/Песня.mp3", 4_000_000, bitrate=128),
    ]
    ranked = filter_and_rank(cands, _policy(), want_ms=2000)
    assert [c.extension for c in ranked] == [".flac", ".mp3"]
    assert [c.bitrate for c in ranked] == [0, 320]


def test_ranking_discards_wrong_duration_and_low_bitrate():
    cands = [
        _cand("/a/Песня.mp3", 11_000_000, bitrate=320, length_s=999),
        _cand("/a/Песня.mp3", 4_000_000, bitrate=128),
    ]
    assert filter_and_rank(cands, _policy(), want_ms=2000) == []


def test_full_download_flow(tmp_path):
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)
    fixture = tmp_path / "src.mp3"
    make_mp3(fixture, duration_s=2.0, bitrate=320)
    downloads = tmp_path / "data" / "downloads"
    downloads.mkdir(parents=True)
    src = FakeSource(
        [_cand("/peer/Исполнитель - Альбом - 01 - Песня.mp3", 11_000_000, 320, 2)],
        fixture,
        downloads,
    )
    dl = Downloader(
        cfg, store, _resolver([src]), DownloadOptions(library_dir=cfg.library_dir)
    )
    stats = dl.run()
    assert stats["downloaded"] == 1
    assert src.enqueued[0][2] == 11_000_000

    dest = cfg.library_dir / "Исполнитель" / "Альбом" / "01. Песня.mp3"
    assert dest.exists()

    from mutagen.easyid3 import EasyID3

    audio = EasyID3(dest)
    assert audio["title"][0] == "Песня"
    assert audio["artist"][0] == "Исполнитель"
    assert audio["album"][0] == "Альбом"
    assert audio["date"][0] == "2020"

    rec = store.tracks[ID]
    assert TrackStatus.DOWNLOADED.value in rec["statuses"]
    assert TrackStatus.METADATA_VERIFIED.value in rec["statuses"]
    assert TrackStatus.COVER_VERIFIED.value not in rec["statuses"]
    assert rec["file"] == str(dest.relative_to(tmp_path))


def test_already_downloaded_with_file_skipped(tmp_path):
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)
    dest = cfg.library_dir / "Исполнитель" / "Альбом" / "01. Песня.mp3"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"x" * 100)
    store.tracks[ID]["file"] = str(dest.relative_to(tmp_path))
    store.tracks[ID]["statuses"] = ["downloaded", "manual_review"]
    src = FakeSource([_cand("/a/x.mp3", 11_000_000, 320, 2)], None, tmp_path)
    dl = Downloader(cfg, store, _resolver([src]), DownloadOptions(library_dir=cfg.library_dir))
    stats = dl.run()
    assert stats["downloaded"] == 0
    assert src.enqueued == []


def test_downloaded_without_file_is_redownloaded(tmp_path):
    """downloaded без файла нигде — не освобождает от перекачки."""
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)
    fixture = tmp_path / "src.mp3"
    make_mp3(fixture, duration_s=2.0, bitrate=320)
    downloads = tmp_path / "data" / "downloads"
    downloads.mkdir(parents=True)
    src = FakeSource(
        [_cand("/peer/Исполнитель - Альбом - 01 - Песня.mp3", 11_000_000, 320, 2)],
        fixture,
        downloads,
    )
    store.tracks[ID]["statuses"] = ["downloaded", "manual_review"]
    dl = Downloader(cfg, store, _resolver([src]), DownloadOptions(library_dir=cfg.library_dir))
    stats = dl.run()
    assert stats["downloaded"] == 1
    assert src.enqueued == [("peer", "/peer/Исполнитель - Альбом - 01 - Песня.mp3", 11_000_000)]


def test_downloaded_with_file_on_secondary_skipped(tmp_path):
    """downloaded + файл на вторичном корне (E:) — пропускается."""
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)
    e_lib = tmp_path / "e_lib"
    rel = "Исполнитель/Альбом/01. Песня.mp3"
    (e_lib / rel).parent.mkdir(parents=True)
    (e_lib / rel).write_bytes(b"x" * 100)
    cfg.library_secondary_dirs = [e_lib]
    store.tracks[ID]["file"] = f"Library/{rel}"
    store.tracks[ID]["statuses"] = ["downloaded", "manual_review"]
    src = FakeSource([_cand("/a/x.mp3", 11_000_000, 320, 2)], None, tmp_path)
    dl = Downloader(cfg, store, _resolver([src]), DownloadOptions(library_dir=cfg.library_dir))
    stats = dl.run()
    assert stats["downloaded"] == 0
    assert stats["skipped"] == 1
    assert src.enqueued == []


def test_cover_verified_with_big_artwork(tmp_path):
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)
    store.upsert(
        "albums",
        ALBUM,
        _album(images=[{"url": "http://img/x.jpg", "width": 1400, "height": 1400}]).to_dict(),
    )
    dl = Downloader(cfg, store, _resolver([]), DownloadOptions(library_dir=cfg.library_dir))
    assert dl._cover_ok(store.albums[ALBUM]) is True


def test_missing_report_written(tmp_path):
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)
    src = FakeSource([_cand("/a/x.mp3", 4_000_000, 128, 2)], None, tmp_path)
    dl = Downloader(cfg, store, _resolver([src]), DownloadOptions(library_dir=cfg.library_dir))
    stats = dl.run()
    assert stats["failed"] == 1
    reports = list((tmp_path / "data" / "reports").glob("download-missing-*.json"))
    assert len(reports) == 1
    data = json.loads(reports[0].read_text(encoding="utf-8"))
    assert data[0]["id"] == ID


class FakeAlbumSource(MusicSource):
    """Альбомный источник: отдаёт папку с файлами релиза."""

    name = "fake-album"
    album_capable = True

    def __init__(self, folder):
        self.folder = folder
        self.searched = 0

    def available(self):
        return True

    def search_album(self, album):
        self.searched += 1
        return [Candidate(source=self.name, filename="Release [FLAC].flac", size=100_000_000, extension=".flac")]

    def download_album(self, cand, dest_dir):
        return self.folder


def test_album_mode_downloads_release(tmp_path):
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)
    store.upsert("tracks", ID, _track(track_number=1).to_dict())
    folder = tmp_path / "release"
    folder.mkdir()
    make_mp3(folder / "01 - Песня.mp3", duration_s=2.0, bitrate=320)
    album_src = FakeAlbumSource(folder)
    dl = Downloader(
        cfg, store, _resolver([album_src]), DownloadOptions(library_dir=cfg.library_dir)
    )
    stats = dl.run()
    assert stats["downloaded"] == 1
    assert album_src.searched == 1
    dest = cfg.library_dir / "Исполнитель" / "Альбом" / "01. Песня.mp3"
    assert dest.exists()
    rec = store.tracks[ID]
    assert TrackStatus.DOWNLOADED.value in rec["statuses"]
    assert rec["file"] == str(dest.relative_to(tmp_path))


class DualSource(FakeSource):
    """Источник с обоими режимами: per-track и album; считает вызовы."""

    name = "dual"
    album_capable = True

    def __init__(self, candidates, download_fixture, downloads_dir, album_folder=None):
        super().__init__(candidates, download_fixture, downloads_dir)
        self.album_folder = album_folder
        self.album_downloaded = 0

    def search_album(self, album):
        return [_cand("Release [MP3].mp3", 100_000_000, 320, 2)]

    def download_album(self, cand, dest_dir):
        self.album_downloaded += 1
        return self.album_folder


def test_thin_album_uses_per_track(tmp_path):
    """Альбом с 1 pending-треком: качаем по-треково, не раздувая CDN-трафик."""
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)
    fixture = tmp_path / "src.mp3"
    make_mp3(fixture, duration_s=2.0, bitrate=320)
    downloads = tmp_path / "data" / "downloads"
    downloads.mkdir(parents=True)
    src = DualSource(
        [_cand("/peer/Исполнитель - Альбом - 01 - Песня.mp3", 11_000_000, 320, 2)],
        fixture,
        downloads,
    )
    dl = Downloader(
        cfg, store, _resolver([src]), DownloadOptions(library_dir=cfg.library_dir)
    )
    stats = dl.run()
    assert stats["downloaded"] == 1
    assert src.album_downloaded == 0  # тонкий альбом не скачивался релизом
    dest = cfg.library_dir / "Исполнитель" / "Альбом" / "01. Песня.mp3"
    assert dest.exists()


def test_thin_album_falls_back_to_release(tmp_path):
    """Если по-треково не нашлось — альбомный режим добирает, количество скачанного не теряется."""
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)
    folder = tmp_path / "release"
    folder.mkdir()
    make_mp3(folder / "01 - Песня.mp3", duration_s=2.0, bitrate=320)
    downloads = tmp_path / "data" / "downloads"
    downloads.mkdir(parents=True)
    src = DualSource([], None, downloads, album_folder=folder)
    dl = Downloader(
        cfg, store, _resolver([src]), DownloadOptions(library_dir=cfg.library_dir)
    )
    stats = dl.run()
    assert stats["downloaded"] == 1
    assert src.album_downloaded == 1  # per-track не сработал — взяли релизом
    dest = cfg.library_dir / "Исполнитель" / "Альбом" / "01. Песня.mp3"
    assert dest.exists()


def test_cover_fetched_once_per_album(tmp_path, monkeypatch):
    """Обложка скачивается 1 раз на альбом, а не на каждый трек."""
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)
    img_url = "http://img/x.jpg"
    store.upsert(
        "albums",
        ALBUM,
        _album(images=[{"url": img_url, "width": 1400, "height": 1400}]).to_dict(),
    )
    id2 = "d" * 22
    store.upsert("tracks", ID, _track(track_number=1).to_dict())
    store.upsert("tracks", id2, _track(spotify_id=id2, track_number=2).to_dict())

    calls = {"n": 0}

    def fake_cover(album, cover_min_size=0, timeout=0):
        calls["n"] += 1
        return {"data": b"cover", "mime": "image/jpeg"}

    monkeypatch.setattr("orpheus.downloader.cover_data", fake_cover)
    fixture = tmp_path / "src.mp3"
    make_mp3(fixture, duration_s=2.0, bitrate=320)
    downloads = tmp_path / "data" / "downloads"
    downloads.mkdir(parents=True)
    src = DualSource(
        [
            _cand("/peer/1.mp3", 8_000_000, 320, 2),
            _cand("/peer/2.mp3", 8_000_000, 320, 2),
        ],
        fixture,
        downloads,
    )
    dl = Downloader(
        cfg, store, _resolver([src]), DownloadOptions(library_dir=cfg.library_dir)
    )
    stats = dl.run()
    # альбом из 2 треков качается по-треково; обложка одна на альбом
    assert stats["downloaded"] == 2
    assert calls["n"] == 1
