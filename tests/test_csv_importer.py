from orpheus.csv_importer import (
    CsvImporter,
    LIKED_SONGS_NAME,
    is_liked_file,
    normalize_header,
    parse_uri,
    playlist_csv_key,
    split_names,
    split_uris,
)
from orpheus.store import Store

SAMPLE_HEADER = (
    "Track URI,Track Name,Artist URI(s),Artist Name(s),Album URI,Album Name,"
    "Album Artist URI(s),Album Artist Name(s),Album Release Date,Album Image URL,"
    "Disc Number,Track Number,Track Duration (ms),Track Preview URL,Explicit,"
    "Popularity,ISRC,Added By,Added At,Artist Genres,Danceability,Energy,Key,"
    "Loudness,Mode,Speechiness,Acousticness,Instrumentalness,Liveness,Valence,"
    "Tempo,Time Signature,Album Genres,Label,Copyrights"
)

A = "a" * 22
B = "b" * 22
C = "c" * 22


def _row(**overrides) -> str:
    base = {
        "track_uri": f"spotify:track:{A}",
        "track_name": "Song One",
        "artist_uris": f"spotify:artist:{B}",
        "artist_names": "Band",
        "album_uri": f"spotify:album:{C}",
        "album_name": "Album A",
        "album_artist_uris": f"spotify:artist:{B}",
        "album_artist_names": "Band",
        "album_release_date": "2020-05-01",
        "album_image_url": "http://img/a",
        "disc_number": "1",
        "track_number": "1",
        "track_duration_ms": "210000",
        "track_preview_url": "",
        "explicit": "false",
        "popularity": "70",
        "isrc": "USAAA0100001",
        "added_by": "",
        "added_at": "2024-01-01T00:00:00Z",
        "artist_genres": "art rock; slowcore",
        "danceability": "0.55",
        "energy": "0.5",
        "key": "6",
        "loudness": "-13.1",
        "mode": "0",
        "speechiness": "0.03",
        "acousticness": "0.7",
        "instrumentalness": "0.01",
        "liveness": "0.1",
        "valence": "0.4",
        "tempo": "120.5",
        "time_signature": "4",
        "album_genres": "",
        "label": "Sony Music",
        "copyrights": "(C) Sony",
    }
    base.update(overrides)
    order = (
        "track_uri", "track_name", "artist_uris", "artist_names", "album_uri",
        "album_name", "album_artist_uris", "album_artist_names", "album_release_date",
        "album_image_url", "disc_number", "track_number", "track_duration_ms",
        "track_preview_url", "explicit", "popularity", "isrc", "added_by",
        "added_at", "artist_genres", "danceability", "energy", "key", "loudness",
        "mode", "speechiness", "acousticness", "instrumentalness", "liveness",
        "valence", "tempo", "time_signature", "album_genres", "label", "copyrights",
    )
    return ",".join(base[k] for k in order)


def test_parse_uri():
    assert parse_uri(f"spotify:track:{A}") == A
    assert parse_uri("") is None
    assert parse_uri(" ") is None


def test_split_names_uris():
    assert split_names("A; B; C") == ["A", "B", "C"]
    uris = split_uris(f"spotify:artist:{A};spotify:artist:{B}")
    assert uris == [A, B]


def test_normalize_header():
    assert normalize_header("Artist Name(s)") == "artist_names"
    assert normalize_header("Track URI") == "track_uri"
    assert normalize_header("Track Duration (ms)") == "track_duration_ms"


def test_is_liked_file():
    assert is_liked_file("liked")
    assert is_liked_file("liked.csv".removesuffix(".csv"))
    assert is_liked_file("Liked Songs")
    assert is_liked_file("liked_songs")
    assert not is_liked_file("Night")


def test_playlist_csv_key_stable():
    assert playlist_csv_key("Night") == playlist_csv_key("Night")
    assert playlist_csv_key("Night") != playlist_csv_key("Rain")


def test_csv_import(tmp_path):
    csv_dir = tmp_path / "exports"
    csv_dir.mkdir()
    liked = csv_dir / "liked.csv"
    liked.write_text(
        SAMPLE_HEADER + "\n" + _row() + "\n", encoding="utf-8"
    )
    night = csv_dir / "Night.csv"
    night.write_text(
        SAMPLE_HEADER
        + "\n"
        + _row(added_at="")
        + "\n",
        encoding="utf-8",
    )

    store = Store(tmp_path / "db")
    CsvImporter(None, store).run(csv_dir)

    assert len(store.playlists) == 2
    track = store.tracks[A]
    assert track["name"] == "Song One"
    assert track["liked"] is True
    assert track["isrc"] == "USAAA0100001"
    assert sorted(track["playlists"]) == sorted(
        [playlist_csv_key("Night"), playlist_csv_key(LIKED_SONGS_NAME)]
    )
    assert track["audio_features"] == {
        "danceability": 0.55,
        "energy": 0.5,
        "key": 6.0,
        "loudness": -13.1,
        "mode": 0.0,
        "speechiness": 0.03,
        "acousticness": 0.7,
        "instrumentalness": 0.01,
        "liveness": 0.1,
        "valence": 0.4,
        "tempo": 120.5,
        "time_signature": 4.0,
    }
    assert store.albums[C]["album_type"] == ""
    assert store.albums[C]["label"] == "Sony Music"
    assert store.albums[C]["track_ids"] == [A]
    assert store.artists[B]["name"] == "Band"
    assert store.artists[B]["genres"] == ["art rock", "slowcore"]

    liked_pl = store.playlists[playlist_csv_key(LIKED_SONGS_NAME)]
    assert liked_pl["name"] == "Liked Songs"
    assert liked_pl["tracks"] == [A]


def test_csv_import_dedups_rows_in_playlist(tmp_path):
    csv_dir = tmp_path / "exports"
    csv_dir.mkdir()
    dup = csv_dir / "Dup.csv"
    dup.write_text(
        SAMPLE_HEADER + "\n" + _row() + "\n" + _row() + "\n", encoding="utf-8"
    )
    store = Store(tmp_path / "db")
    CsvImporter(None, store).run(csv_dir)
    assert store.playlists[playlist_csv_key("Dup")]["tracks"] == [A]
