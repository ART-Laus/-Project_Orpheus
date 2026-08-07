from orpheus.models import Album, Artist, Image, Playlist, Track, local_track_id
from orpheus.statuses import TrackStatus, default_statuses, has_status


def test_track_roundtrip():
    track = Track(
        spotify_id="abc",
        name="Test Song",
        artist_ids=["a1"],
        artist_names=["Artist"],
        album_id="alb1",
        isrc="USABC1234567",
    )
    restored = Track.from_dict(track.to_dict())
    assert restored == track


def test_track_default_statuses():
    track = Track(spotify_id="x", name="n")
    assert track.statuses == default_statuses()
    assert has_status(track.statuses, TrackStatus.MANUAL_REVIEW)


def test_track_from_dict_missing_fields():
    restored = Track.from_dict({"spotify_id": "x"})
    assert restored.name == ""
    assert restored.statuses == default_statuses()


def test_album_roundtrip():
    album = Album(
        spotify_id="a1",
        name="Album",
        album_type="album",
        release_date="1997-05-21",
        images=[Image(url="http://x", width=640, height=640)],
        track_ids=["t1", "t2"],
    )
    assert Album.from_dict(album.to_dict()) == album


def test_playlist_contains_only_references():
    playlist = Playlist(spotify_id="p1", name="Night", tracks=["t1", "t2", "t3"])
    restored = Playlist.from_dict(playlist.to_dict())
    assert restored.tracks == ["t1", "t2", "t3"]


def test_artist_roundtrip():
    artist = Artist(spotify_id="ar1", name="Radiohead", genres=["art rock"])
    assert Artist.from_dict(artist.to_dict()) == artist


def test_local_track_id_stable():
    a = local_track_id("Song", ["Band"], 180000)
    b = local_track_id("Song", ["Band"], 180000)
    c = local_track_id("Song", ["Band"], 180001)
    assert a == b
    assert a != c
    assert a.startswith("local:")
