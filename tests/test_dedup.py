import json

from orpheus.config import Config
from orpheus.dedup import DedupAnalyzer, DedupApplier, normalize_key
from orpheus.models import Album, Track
from orpheus.store import Store

ISRC1 = "USAAA0100001"
A = "a" * 22
B = "b" * 22
C = "c" * 22
D = "d" * 22
E = "e" * 22


def _track(tid, name, artist="Band", isrc=ISRC1, liked=False, playlists=None):
    return Track(
        spotify_id=tid,
        name=name,
        artist_ids=["art1"],
        artist_names=[artist],
        album_id=tid,
        album_name="Album " + tid[:3],
        isrc=isrc,
        liked=liked,
        playlists=list(playlists or []),
    ).to_dict()


def _album(aid, name, date="2020-01-01", track_ids=None):
    return Album(
        spotify_id=aid, name=name, release_date=date, track_ids=list(track_ids or [])
    ).to_dict()


def _make_store(tmp_path):
    store = Store(tmp_path / "db")
    store.upsert("tracks", A, _track(A, "Песня"))
    store.upsert("tracks", B, _track(B, "Песня", liked=True, playlists=["Night"]))
    store.upsert("tracks", C, _track(C, "Другая", isrc="USZZZ0000002"))
    store.upsert("albums", A, _album(A, "Альбом А", track_ids=[A]))
    store.upsert("albums", B, _album(B, "Песня", date="2019-05-05", track_ids=[B]))
    store.upsert("albums", C, _album(C, "Альбом C", track_ids=[C]))
    store.upsert("artists", "art1", {"spotify_id": "art1", "name": "Band", "genres": []})
    store.upsert(
        "playlists",
        "night",
        {"spotify_id": "night", "name": "Night", "tracks": [B]},
    )
    store.upsert(
        "playlists",
        "oldpl",
        {"spotify_id": "oldpl", "name": "Old", "tracks": [A, B]},
    )
    store.upsert(
        "playlists",
        "likedpl",
        {"spotify_id": "likedpl", "name": "Liked Songs", "tracks": [B]},
    )
    return store


def _cfg(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "paths:\n  data_dir: data\n", encoding="utf-8"
    )
    return Config(project_root=tmp_path)


def test_normalize_key():
    assert normalize_key("Песня!") == normalize_key("песня")
    assert normalize_key("Ég Er )") == "éger"
    assert normalize_key("太陽系デスコ (feat. 初音ミク)") == normalize_key("太陽系デスコfeat初音ミク")
    assert normalize_key("THE SONG") == "thesong"


def test_isrc_groups(tmp_path):
    store = _make_store(tmp_path)
    cfg = _cfg(tmp_path)
    groups = DedupAnalyzer(cfg, store).isrc_groups()
    assert set(groups[ISRC1]) == {A, B}
    assert len(groups) == 1


def test_name_groups_excludes_same_isrc(tmp_path):
    store = _make_store(tmp_path)
    cfg = _cfg(tmp_path)
    groups = DedupAnalyzer(cfg, store).name_groups()
    assert len(groups) == 0


def test_suggest_canonical_prefers_album_then_date(tmp_path):
    store = _make_store(tmp_path)
    cfg = _cfg(tmp_path)
    assert DedupAnalyzer(cfg, store).suggest_canonical([A, B]) == A


def test_suggest_canonical_liked_as_tiebreaker(tmp_path):
    store = _make_store(tmp_path)
    store.upsert(
        "tracks", D, _track(D, "Дубль", liked=True, playlists=["Night"])
    )
    store.upsert(
        "albums", D, _album(D, "Альбом Д", date="2020-01-01", track_ids=[D])
    )
    store.upsert("tracks", E, _track(E, "Дубль", isrc="USZZZ0000003"))
    store.upsert(
        "albums", E, _album(E, "Альбом Е", date="2020-01-01", track_ids=[E])
    )
    cfg = _cfg(tmp_path)
    assert DedupAnalyzer(cfg, store).suggest_canonical([D, E]) == D


def test_write_report(tmp_path):
    store = _make_store(tmp_path)
    cfg = _cfg(tmp_path)
    analysis = DedupAnalyzer(cfg, store).analyze()
    path = DedupAnalyzer(cfg, store).write_report(analysis)
    md = path.read_text(encoding="utf-8")
    assert "Песня" in md
    assert (path.parent / "duplicates.json").exists()
    assert analysis["exact_groups"][0]["canonical"] == A


def test_apply_remaps_deletes_backs_up(tmp_path):
    store = _make_store(tmp_path)
    cfg = _cfg(tmp_path)
    stats = DedupApplier(cfg, store).apply()

    assert stats["removed_tracks"] == 1
    assert B not in store.tracks
    assert A in store.tracks
    assert store.playlists["night"]["tracks"] == [A]
    assert store.playlists["oldpl"]["tracks"] == [A]
    assert store.playlists["likedpl"]["tracks"] == [A]
    assert set(store.albums) == {A, C}
    assert "art1" in store.artists

    backups = list((tmp_path / "data" / "backups").glob("dedup-*.json"))
    assert len(backups) == 1
    backup = json.loads(backups[0].read_text(encoding="utf-8"))
    assert backup["removed"][0]["track"]["spotify_id"] == B
    assert backup["removed"][0]["canonical"] == A


def test_apply_second_run_is_noop(tmp_path):
    store = _make_store(tmp_path)
    cfg = _cfg(tmp_path)
    DedupApplier(cfg, store).apply()
    stats = DedupApplier(cfg, store).apply()
    assert stats["removed_tracks"] == 0


def test_apply_respects_user_decision(tmp_path):
    store = _make_store(tmp_path)
    cfg = _cfg(tmp_path)
    (cfg.root / "data").mkdir(parents=True)
    (cfg.root / "data" / "decisions.json").write_text(
        json.dumps({ISRC1: {"canonical": A, "reason": "люблю сингл"}}),
        encoding="utf-8",
    )
    stats = DedupApplier(cfg, store).apply()
    assert stats["removed_tracks"] == 1
    assert A in store.tracks
    assert B not in store.tracks
    assert store.playlists["night"]["tracks"] == [A]


def test_apply_skip_keeps_everything(tmp_path):
    store = _make_store(tmp_path)
    cfg = _cfg(tmp_path)
    (cfg.root / "data").mkdir(parents=True)
    (cfg.root / "data" / "decisions.json").write_text(
        json.dumps({ISRC1: {"skip": True, "reason": "не трогать"}}),
        encoding="utf-8",
    )
    stats = DedupApplier(cfg, store).apply()
    assert stats["removed_tracks"] == 0
    assert {A, B} <= set(store.tracks)
