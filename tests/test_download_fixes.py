"""Тесты исправлений механизма скачивания и структурирования.

Покрывают: порядок очереди по вердиктам чекера Яндекса (H -> M),
счётчик пропущенных, collision-safe имена, отсутствие префикса "00.",
изоляцию staging по альбомам, корректные MP4-атомы, чистку неудачных
кандидатов из staging, sha1-save_path торрентов, отказ per-track
у торрентов.
"""

import hashlib
import json
import shutil
import struct
from pathlib import Path

import pytest

from mutagen.mp4 import MP4

from orpheus.config import Config
from orpheus.downloader import Downloader, DownloadOptions
from orpheus.resolver import Resolver
from orpheus.sources.base import Candidate, MusicSource, SourceNotSupported
from orpheus.sources.torrents import TorrentSource, build_specs
from orpheus.store import Store
from orpheus.statuses import TrackStatus

ID = "a" * 22
ALBUM = "b" * 22
ARTIST = "c" * 22


def make_mp3(path, duration_s=2.0, bitrate=320):
    rate = 44100
    frame_bytes = 144 * bitrate * 1000 // rate
    frames = int(duration_s * rate / 1152)
    data = bytearray()
    for _ in range(frames):
        br_idx = 14 if bitrate >= 320 else (13 if bitrate >= 256 else 9)
        data += bytes([0xFF, 0xFB, (br_idx << 4) | 0x00, 0x00])
        data += b"\x00" * (frame_bytes - 4)
    path.write_bytes(bytes(data))


def _track(**overrides):
    from orpheus.models import Track

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
    from orpheus.models import Album

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
    return store


def _cand(filename, size, bitrate=320, length_s=2):
    return Candidate(
        source="fake",
        filename=filename,
        size=size,
        bitrate=bitrate,
        length_s=length_s,
        extension=Path(filename).suffix.lower(),
    )


def _resolver(sources):
    from orpheus.quality import QualityPolicy

    return Resolver(sources, QualityPolicy(min_mp3_bitrate=320, min_aac_bitrate=256, duration_tolerance_s=5))


class FakeSource(MusicSource):
    """Per-track источник-заглушка: «скачивание» — копия fixture в staging.

    match: если задано, источник отвечает только на треки с этой подстрокой
    в названии (чтобы несколько источников обслуживали разные треки).
    """

    name = "fake"

    def __init__(self, candidates, download_fixture=None, downloads_dir=None, match=None):
        self.candidates = list(candidates)
        self.download_fixture = download_fixture
        self.downloads_dir = downloads_dir
        self.match = match

    def available(self):
        return True

    def search_track(self, track):
        if self.match and self.match.lower() not in track.name.lower():
            return []
        return list(self.candidates)

    def download_track(self, cand, dest_dir):
        if self.download_fixture is None:
            return None
        dest = self.downloads_dir / Path(cand.filename).name
        shutil.copy2(self.download_fixture, dest)
        return dest


class FakeAlbumSource(MusicSource):
    """Альбомный источник, пишущий файлы прямо в переданный staging (как yandex)."""

    name = "fake-album"
    album_capable = True

    def __init__(self):
        self.searched = 0

    def available(self):
        return True

    def search_album(self, album):
        self.searched += 1
        return [
            Candidate(
                source=self.name, filename="Release [FLAC].flac", size=90_000_000, extension=".flac"
            )
        ]

    def download_album(self, cand, dest_dir):
        make_mp3(dest_dir / "01 - Песня.mp3", duration_s=2.0, bitrate=320)
        make_mp3(dest_dir / "99 - Лишний трек.mp3", duration_s=2.0, bitrate=320)
        return dest_dir


class QbitFake:
    """Заглушка qBittorrent: логирует add_torrent и «мгновенно качает»."""

    def __init__(self):
        self.adds = []

    def add_torrent(self, magnet, save_path):
        self.adds.append((magnet, str(save_path)))
        return "h" * 40

    def wait_complete(self, torrent_hash):
        return {"hash": torrent_hash, "save_path": self.adds[-1][1]}

    def content_dir(self, torrent):
        return torrent["save_path"]


# --- порядок очереди и счётчики -------------------------------------------


def test_pending_orders_by_coverage_verdict(tmp_path):
    """Очередь продолжает работу чекера Яндекса: H (есть) -> M (нет) -> прочие."""
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)
    tid2 = "b" * 22
    album2 = "d" * 22
    store.upsert("tracks", tid2, _track(spotify_id=tid2, name="Вторая", album_id=album2).to_dict())
    store.upsert("albums", album2, _album(name="Второй").to_dict())
    reports = tmp_path / "data" / "reports"
    reports.mkdir(parents=True)
    (reports / "yandex-coverage-state.json").write_text(
        json.dumps({"checked": {tid2: "H", ID: "M"}}), encoding="utf-8"
    )
    dl = Downloader(cfg, store, _resolver([]), DownloadOptions(library_dir=cfg.library_dir))
    pending = dl._pending_tracks("")
    assert [t.spotify_id for t in pending] == [tid2, ID]


def test_downloaded_skipped_counts(tmp_path):
    """Уже скачанные треки (статус downloaded + файл на диске) не идут в работу и попадают в skipped."""
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)
    dest = cfg.library_dir / "Исполнитель" / "Альбом" / "01. Песня.mp3"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"x" * 100)
    store.tracks[ID]["file"] = str(dest.relative_to(tmp_path))
    src = FakeSource([_cand("/a/x.mp3", 11_000_000, 320, 2)], None, tmp_path)
    dl = Downloader(cfg, store, _resolver([src]), DownloadOptions(library_dir=cfg.library_dir))
    store.tracks[ID]["statuses"] = ["downloaded", "manual_review"]
    stats = dl.run()
    assert stats["downloaded"] == 0
    assert stats["skipped"] == 1


# --- раскладка файлов ------------------------------------------------------


def test_finalize_collision_gets_unique_name(tmp_path):
    """Коллизия имени в Library не должна перезаписывать существующий файл."""
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)
    fixture = tmp_path / "src.mp3"
    make_mp3(fixture, duration_s=2.0, bitrate=320)
    downloads = tmp_path / "data" / "downloads"
    downloads.mkdir(parents=True)
    src = FakeSource([_cand("/peer/01 Песня.mp3", 11_000_000, 320, 2)], fixture, downloads)
    dl = Downloader(cfg, store, _resolver([src]), DownloadOptions(library_dir=cfg.library_dir))
    dest_dir = cfg.library_dir / "Исполнитель" / "Альбом"
    dest_dir.mkdir(parents=True)
    first = dest_dir / "01. Песня.mp3"
    make_mp3(first, duration_s=2.0, bitrate=320)
    before = first.read_bytes()
    stats = dl.run()
    assert stats["downloaded"] == 1
    assert first.read_bytes() == before  # исходный файл не тронут
    assert (dest_dir / "01. Песня (2).mp3").exists()


def test_finalize_no_number_prefix_when_unknown(tmp_path):
    """Без номера трека имя файла не получает префикс '00.'."""
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)
    store.upsert("tracks", ID, _track(track_number=0).to_dict())
    fixture = tmp_path / "src.mp3"
    make_mp3(fixture, duration_s=2.0, bitrate=320)
    downloads = tmp_path / "data" / "downloads"
    downloads.mkdir(parents=True)
    src = FakeSource([_cand("/peer/Песня.mp3", 11_000_000, 320, 2)], fixture, downloads)
    dl = Downloader(cfg, store, _resolver([src]), DownloadOptions(library_dir=cfg.library_dir))
    stats = dl.run()
    assert stats["downloaded"] == 1
    album_dir = cfg.library_dir / "Исполнитель" / "Альбом"
    assert (album_dir / "Песня.mp3").exists()
    assert not list(album_dir.glob("0*.mp3"))


def _min_m4a_bytes() -> bytes:
    """Минимальный валидный MP4-контейнер (без треков) для тестов тегов."""

    def box(name, payload):
        return struct.pack(">I", 8 + len(payload)) + name + payload

    ftyp = box(b"ftyp", b"isom\x00\x00\x00\x00isomiso2")
    meta = box(b"meta", b"\x00\x00\x00\x00" + box(b"ilst", b""))
    moov = box(b"moov", box(b"udta", meta))
    return ftyp + box(b"mdat", b"") + moov


def test_mp4_tags_written_with_proper_atoms(tmp_path):
    """M4A-теги должны писаться штатными атомами, а не мусорными titl/arti/albu."""
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)
    track = _track(track_number=3, disc_number=2)
    path = tmp_path / "song.m4a"
    path.write_bytes(_min_m4a_bytes())
    dl = Downloader(cfg, store, _resolver([]), DownloadOptions(library_dir=cfg.library_dir))
    dl._apply_tags(path, track, store.albums[ALBUM], {})
    tags = MP4(path).tags
    assert tags["\xa9nam"] == ["Песня"]
    assert tags["\xa9ART"] == ["Исполнитель"]
    assert tags["aART"] == ["Исполнитель"]
    assert tags["\xa9alb"] == ["Альбом"]
    assert tags["\xa9day"] == ["2020"]
    assert tags["trkn"] == [(3, 0)]
    assert tags["disk"] == [(2, 0)]


# --- изоляция staging ------------------------------------------------------


def test_album_staging_isolated_and_cleaned(tmp_path):
    """Файлы релиза качаются в свой каталог staging и удаляются после матчинга."""
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)
    album_src = FakeAlbumSource()
    dl = Downloader(
        cfg, store, _resolver([album_src]), DownloadOptions(library_dir=cfg.library_dir)
    )
    stats = dl.run()
    assert stats["downloaded"] == 1
    assert album_src.searched == 1
    dest = cfg.library_dir / "Исполнитель" / "Альбом" / "01. Песня.mp3"
    assert dest.exists()
    staging = tmp_path / "data" / "downloads"
    # staging очищен полностью (и не-матченный "99 - Лишний трек.mp3", и каталог)
    assert list(staging.glob("album-*")) == []
    rec = store.tracks[ID]
    assert TrackStatus.DOWNLOADED.value in rec["statuses"]
    assert TrackStatus.METADATA_VERIFIED.value in rec["statuses"]


# --- resolver: чистка мусора -----------------------------------------------


def test_resolver_cleans_failed_candidate(tmp_path):
    """Файл кандидата, не прошедший проверку, удаляется из staging."""
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"\x00\x01\x02 not audio")
    good = tmp_path / "good.mp3"
    make_mp3(good, duration_s=2.0, bitrate=320)
    downloads = tmp_path / "data" / "downloads"
    downloads.mkdir(parents=True)
    src_bad = FakeSource([_cand("/x/bad.mp3", 60, 128, 2)], bad, downloads)
    src_good = FakeSource([_cand("/x/good.mp3", 11_000_000, 320, 2)], good, downloads)
    dl = Downloader(
        cfg, store, _resolver([src_bad, src_good]), DownloadOptions(library_dir=cfg.library_dir)
    )
    stats = dl.run()
    assert stats["downloaded"] == 1
    assert not (downloads / "bad.mp3").exists()  # мусор удалён из staging
    assert (cfg.library_dir / "Исполнитель" / "Альбом" / "01. Песня.mp3").exists()


# --- структура библиотеки и метаданные --------------------------------------


def _id3_text(id3, key) -> str:
    frames = id3.getall(key)
    return " / ".join(str(t) for t in (frames[0].text if frames else []))


def test_library_structure_and_metadata(tmp_path, monkeypatch):
    """Скачанные песни раскладываются так же, как в текущей Library/:
    Library/<исполнитель>/<альбом>/<NN. Название>.mp3 для альбомов и
    Library/<исполнитель>/«Синглы и EP»/<NN. Название>.mp3 (сквозная
    нумерация по дате релиза) для синглов и EP; метаданные на месте."""
    from mutagen.id3 import ID3
    from orpheus.reorganize import LibraryReorganizer

    cfg = _cfg(tmp_path)
    store = _store(tmp_path)
    # альбом с крупной обложкой -> покрывается тегом cover_verified
    store.upsert(
        "albums",
        ALBUM,
        _album(
            total_tracks=10,
            album_type="album",
            images=[{"url": "http://i.scdn.co/image/ab67616d0000b273abc123", "width": 1400}],
        ).to_dict(),
    )
    store.upsert("tracks", ID, _track(track_number=2).to_dict())
    # сингл и EP: по структуре Library/ уходят в папку «Синглы и EP»
    tid2, album2 = "b" * 22, "e" * 22
    tid3, album3 = "c" * 22, "f" * 22
    store.upsert(
        "albums",
        album2,
        _album(spotify_id=album2, name="Сингл", total_tracks=1, album_type="single").to_dict(),
    )
    store.upsert(
        "albums",
        album3,
        _album(spotify_id=album3, name="Мой EP", total_tracks=3, album_type="ep").to_dict(),
    )
    store.upsert(
        "tracks",
        tid2,
        _track(
            spotify_id=tid2, name="Хит", album_id=album2, album_name="Сингл",
            track_number=0, liked=False,
        ).to_dict(),
    )
    store.upsert(
        "tracks",
        tid3,
        _track(
            spotify_id=tid3, name="Эпик", album_id=album3, album_name="Мой EP",
            track_number=4, liked=False,
        ).to_dict(),
    )

    downloads = tmp_path / "data" / "downloads"
    downloads.mkdir(parents=True)
    fixture1 = tmp_path / "src1.mp3"
    fixture2 = tmp_path / "src2.mp3"
    fixture3 = tmp_path / "src3.mp3"
    make_mp3(fixture1, duration_s=2.0, bitrate=320)
    make_mp3(fixture2, duration_s=2.0, bitrate=320)
    make_mp3(fixture3, duration_s=2.0, bitrate=320)
    srcs = [
        FakeSource([_cand("/p/02 Песня.mp3", 11_000_000, 320, 2)], fixture1, downloads, match="Песня"),
        FakeSource([_cand("/p/Хит.mp3", 8_000_000, 320, 2)], fixture2, downloads, match="Хит"),
        FakeSource([_cand("/p/01 Эпик.mp3", 9_000_000, 320, 2)], fixture3, downloads, match="Эпик"),
    ]
    monkeypatch.setattr(
        "orpheus.downloader.cover_data",
        lambda album, cover_min_size: {"data": b"\xff\xd8\xff\xe0 fake cover", "mime": "image/jpeg"},
    )
    dl = Downloader(cfg, store, _resolver(srcs), DownloadOptions(library_dir=cfg.library_dir))
    stats = dl.run()
    assert stats["downloaded"] == 3
    assert stats["skipped"] == 0
    # финальную раскладку (синглы и EP -> «Синглы и EP») делает reorganize
    LibraryReorganizer(cfg, store).run()

    root = cfg.library_dir / "Исполнитель"
    album_track = root / "Альбом" / "02. Песня.mp3"
    single_track = root / "Синглы и EP" / "01. Хит.mp3"  # сквозная нумерация
    ep_track = root / "Синглы и EP" / "02. Эпик.mp3"
    assert {p.name for p in root.iterdir()} == {"Альбом", "Синглы и EP"}
    assert album_track.exists() and single_track.exists() and ep_track.exists()

    id3 = ID3(album_track)
    assert _id3_text(id3, "TIT2") == "Песня"
    assert _id3_text(id3, "TPE1") == "Исполнитель"  # artist + albumartist
    assert _id3_text(id3, "TALB") == "Альбом"
    assert _id3_text(id3, "TRCK") == "2"
    year = _id3_text(id3, "TDRC") or _id3_text(id3, "TYER")
    assert year == "2020"
    apic = id3.getall("APIC")
    assert len(apic) == 1
    assert apic[0].data == b"\xff\xd8\xff\xe0 fake cover"
    assert apic[0].mime == "image/jpeg"
    assert _id3_text(ID3(single_track), "TIT2") == "Хит"
    assert _id3_text(ID3(ep_track), "TIT2") == "Эпик"

    for tid, expected in ((ID, "Альбом"), (tid2, "Синглы и EP"), (tid3, "Синглы и EP")):
        rec = store.tracks[tid]
        assert TrackStatus.DOWNLOADED.value in rec["statuses"]
        assert TrackStatus.METADATA_VERIFIED.value in rec["statuses"]
        assert rec["file"] and (cfg.root / rec["file"]).exists()
        assert expected in rec["file"]
    # cover_verified — только у трека с крупной обложкой в базе
    assert TrackStatus.COVER_VERIFIED.value in store.tracks[ID]["statuses"]
    assert TrackStatus.COVER_VERIFIED.value not in store.tracks[tid2]["statuses"]


# --- торренты ---------------------------------------------------------------


def test_torrent_save_path_deterministic(tmp_path):
    """save_path торрента считается sha1 от magnet: один magnet -> один каталог."""
    magnet = "magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01&dn=Album+2020"
    qbit = QbitFake()
    src = TorrentSource(
        spec=build_specs()["rutor"],
        bases=["https://example.test"],
        qbit=qbit,
        torrents_dir=tmp_path / "torrents",
    )
    cand = Candidate(
        source="rutor", filename="Release [FLAC]", size=1, extension=".flac", extra={"magnet": magnet}
    )
    expected = tmp_path / "torrents" / ("rutor-" + hashlib.sha1(magnet.encode("utf-8")).hexdigest()[:24])
    assert src._download(cand, tmp_path) == expected
    assert qbit.adds[0][1] == str(expected)
    src._download(cand, tmp_path)
    assert qbit.adds[1][1] == qbit.adds[0][1]  # повторный magnet -> тот же каталог


def test_torrent_download_track_raises_not_supported(tmp_path):
    """Торренты качают релизы, а не треки: per-track режим должен явно отказать."""
    src = TorrentSource(
        spec=build_specs()["rutor"],
        bases=["https://example.test"],
        qbit=QbitFake(),
        torrents_dir=tmp_path / "torrents",
    )
    cand = Candidate(source="rutor", filename="x", size=1, extension=".mp3")
    with pytest.raises(SourceNotSupported):
        src.download_track(cand, tmp_path)


def test_match_files_handles_feat_and_zero_numbers(tmp_path):
    """Файлы без нумерации и фиты в скобках сопоставляются с треками базы."""
    from orpheus.resolver import match_files_to_tracks

    folder = tmp_path / "album"
    folder.mkdir()
    make_mp3(folder / "00. Lust For Life.mp3", duration_s=2.0, bitrate=320)
    make_mp3(folder / "00. 13 Beaches.mp3", duration_s=2.0, bitrate=320)
    make_mp3(folder / "00. Love.mp3", duration_s=2.0, bitrate=320)
    t1 = _track(spotify_id="a" * 22, name="Lust For Life (with The Weeknd)", track_number=2)
    t2 = _track(spotify_id="b" * 22, name="13 Beaches", track_number=3)
    t3 = _track(spotify_id="c" * 22, name="Love", track_number=1)
    matched = match_files_to_tracks(folder, [t1, t2, t3])
    assert {k: v.name for k, v in matched.items()} == {
        t1.spotify_id: "00. Lust For Life.mp3",
        t2.spotify_id: "00. 13 Beaches.mp3",
        t3.spotify_id: "00. Love.mp3",
    }