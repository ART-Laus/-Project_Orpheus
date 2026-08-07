"""Тесты Resolver: приоритет источников, фолбэк, альбомный режим, маппинг."""

import shutil
from pathlib import Path

from orpheus.models import Album, Track
from orpheus.quality import QualityPolicy
from orpheus.resolver import Resolver, match_files_to_tracks
from orpheus.sources.base import Candidate, MusicSource

ID1 = "a" * 22
ID2 = "b" * 22
ALBUM = "c" * 22


def make_mp3(path, duration_s=2.0, bitrate=320):
    rate = 44100
    frame_bytes = 144 * bitrate * 1000 // rate
    frames = int(duration_s * rate / 1152)
    data = bytearray()
    for i in range(frames):
        br_idx = 14 if bitrate >= 320 else (13 if bitrate >= 256 else 9)
        data += bytes([0xFF, 0xFB, (br_idx << 4) | 0x00, 0x00])
        data += b"\x00" * (frame_bytes - 4)
    path.write_bytes(bytes(data))


def _track(spotify_id, name, number=1, duration_ms=2000, album_id=ALBUM):
    return Track(
        spotify_id=spotify_id,
        name=name,
        artist_names=["Исполнитель"],
        album_id=album_id,
        album_name="Альбом",
        duration_ms=duration_ms,
        track_number=number,
        statuses=["manual_review"],
    )


def _album(name="Альбом"):
    return Album(spotify_id=ALBUM, name=name, artist_names=["Исполнитель"], total_tracks=2)


def _cand(filename, size=11_000_000, bitrate=320, length_s=2):
    return Candidate(
        source="src",
        filename=filename,
        size=size,
        bitrate=bitrate,
        length_s=length_s,
        extension=Path(filename).suffix.lower(),
    )


class StubSource(MusicSource):
    """Настраиваемая заглушка: кандидаты, результат скачивания, режимы."""

    name = "stub"

    def __init__(
        self,
        candidates=None,
        download_result=None,
        available=True,
        supports_album=False,
        album_candidates=None,
        album_folder=None,
    ):
        self.candidates = candidates or []
        self.download_result = download_result
        self._available = available
        self.supports_album = supports_album
        self.album_candidates = album_candidates or []
        self.album_folder = album_folder
        self.download_calls = []

    def available(self):
        return self._available

    def search_track(self, track):
        return list(self.candidates)

    def download_track(self, cand, dest_dir):
        self.download_calls.append(cand)
        return self.download_result

    def search_album(self, album):
        return list(self.album_candidates)

    def download_album(self, cand, dest_dir):
        return self.album_folder


def test_unavailable_source_is_skipped(tmp_path):
    src = StubSource(available=False)
    resolver = Resolver([src])
    assert resolver.resolve_track(_track(ID1, "Песня"), tmp_path) is None
    assert src.download_calls == []


def test_uses_first_source_with_result(tmp_path):
    fixture = tmp_path / "a.mp3"
    make_mp3(fixture)
    first = StubSource(candidates=[_cand("/x/a.mp3")], download_result=fixture)
    second = StubSource(candidates=[_cand("/x/b.mp3")], download_result=fixture)
    resolver = Resolver([first, second])
    path = resolver.resolve_track(_track(ID1, "Песня"), tmp_path)
    assert path == fixture
    assert second.download_calls == []


def test_fallback_to_next_source_on_failed_verify(tmp_path):
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"not an mp3 at all")
    good = tmp_path / "good.mp3"
    make_mp3(good, duration_s=2.0, bitrate=320)
    first = StubSource(candidates=[_cand("/x/bad.mp3")], download_result=bad)
    second = StubSource(candidates=[_cand("/x/good.mp3")], download_result=good)
    resolver = Resolver([first, second])
    path = resolver.resolve_track(_track(ID1, "Песня"), tmp_path)
    assert path == good
    assert len(first.download_calls) == 1


def test_best_candidate_selected(tmp_path):
    low = tmp_path / "low.mp3"
    make_mp3(low, duration_s=2.0, bitrate=128)
    high = tmp_path / "high.flac"
    make_mp3(high, duration_s=2.0, bitrate=320)
    src = StubSource(
        candidates=[
            _cand("/x/low.mp3", 4_000_000, 128),
            _cand("/x/high.flac", 30_000_000, 0),
        ],
        download_result=low,
    )
    resolver = Resolver([src])
    assert resolver.resolve_track(_track(ID1, "Песня"), tmp_path) == low


def test_resolve_album_maps_files_by_number(tmp_path):
    folder = tmp_path / "release"
    folder.mkdir()
    t1 = _track(ID1, "Первая", number=1)
    t2 = _track(ID2, "Вторая", number=2)
    make_mp3(folder / "01 - Первая.mp3", duration_s=2.0)
    make_mp3(folder / "02 - Вторая.mp3", duration_s=2.0)
    src = StubSource(
        supports_album=True,
        album_candidates=[_cand("Album [FLAC].flac", size=90_000_000)],
        album_folder=folder,
    )
    resolver = Resolver([src])
    matched = resolver.resolve_album(_album(), [t1, t2], tmp_path)
    assert matched[ID1] == folder / "01 - Первая.mp3"
    assert matched[ID2] == folder / "02 - Вторая.mp3"


def test_match_files_by_name_fallback(tmp_path):
    folder = tmp_path / "release"
    folder.mkdir()
    t1 = _track(ID1, "Первая песня", number=0)
    make_mp3(folder / "Intro.mp3", duration_s=2.0)
    make_mp3(folder / "Первая песня.mp3", duration_s=2.0)
    matched = match_files_to_tracks(folder, [t1], QualityPolicy())
    assert matched[ID1] == folder / "Первая песня.mp3"


def test_match_skips_wrong_duration(tmp_path):
    folder = tmp_path / "release"
    folder.mkdir()
    t1 = _track(ID1, "Первая", number=1, duration_ms=2000)
    make_mp3(folder / "01 - Первая.mp3", duration_s=99.0)
    matched = match_files_to_tracks(folder, [t1], QualityPolicy(verify_tolerance_s=10))
    assert ID1 not in matched
