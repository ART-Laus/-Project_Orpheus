import json

from orpheus.models import Track
from orpheus.store import Store


def _track(tid: str) -> dict:
    return Track(spotify_id=tid, name=f"Song {tid}").to_dict()


def test_save_load_roundtrip(tmp_path):
    store = Store(tmp_path)
    store.upsert("tracks", "t1", _track("t1"))
    store.upsert("albums", "a1", {"spotify_id": "a1", "name": "A"})
    store.save_all()

    loaded = Store(tmp_path).load()
    assert loaded.tracks["t1"]["name"] == "Song t1"
    assert loaded.albums["a1"]["name"] == "A"
    assert loaded.meta["version"] == 1


def test_upsert_is_idempotent(tmp_path):
    store = Store(tmp_path)
    store.upsert("tracks", "t1", _track("t1"))
    store.upsert("tracks", "t1", _track("t1"))
    assert len(store.tracks) == 1


def test_resume_merges_without_duplicates(tmp_path):
    s1 = Store(tmp_path)
    s1.upsert("tracks", "t1", _track("t1"))
    s1.upsert("tracks", "t2", _track("t2"))
    s1.save_all()

    s2 = Store(tmp_path).load()
    s2.upsert("tracks", "t2", _track("t2"))
    s2.upsert("tracks", "t3", _track("t3"))
    s2.save_all()

    s3 = Store(tmp_path).load()
    assert sorted(s3.tracks) == ["t1", "t2", "t3"]


def test_load_missing_files_gives_empty(tmp_path):
    store = Store(tmp_path).load()
    assert store.tracks == {}
    assert store.albums == {}
    assert store.meta["version"] == 1


def test_json_is_utf8_readable(tmp_path):
    store = Store(tmp_path)
    store.upsert("tracks", "t1", Track(spotify_id="t1", name="Кино").to_dict())
    store.save_all()
    raw = (tmp_path / "tracks.json").read_text(encoding="utf-8")
    assert "Кино" in raw
    assert json.loads(raw)["t1"]["name"] == "Кино"
