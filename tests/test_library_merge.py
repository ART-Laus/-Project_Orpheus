"""Тесты умного переноса C: → E: (orpheus library merge)."""

from pathlib import Path

from orpheus.config import Config
from orpheus.library_check import LibraryCheck
from orpheus.library_merge import LibraryMerge
from orpheus.models import Track
from orpheus.statuses import TrackStatus
from orpheus.store import Store

T1 = "t" * 22 + "1"
T2 = "t" * 22 + "2"
T3 = "t" * 22 + "3"
ART = "Band"
ALBUM = "Album"


def _track(tid, name, number=1, downloaded=False):
    statuses = [TrackStatus.MANUAL_REVIEW.value]
    if downloaded:
        statuses.append(TrackStatus.DOWNLOADED.value)
    t = Track(
        spotify_id=tid,
        name=name,
        artist_ids=["art1"],
        artist_names=[ART],
        album_id="album1",
        album_name=ALBUM,
        duration_ms=200_000,
        track_number=number,
        statuses=statuses,
    ).to_dict()
    t["file"] = f"Library/{ART}/{ALBUM}/{number:02d}. {name}.mp3"
    return t


def _make_store(tmp_path, tracks) -> Store:
    store = Store(tmp_path / "db")
    for t in tracks:
        store.upsert("tracks", t["spotify_id"], t)
    return store


def _make_cfg(tmp_path) -> Config:
    (tmp_path / "config.yaml").write_text(
        "paths:\n  data_dir: data\nlibrary:\n  dir: Library\n", encoding="utf-8"
    )
    cfg = Config(project_root=tmp_path)
    cfg.library_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _write(root: Path, rel: str, size: int = 100) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)


def _make(tmp_path, tracks):
    cfg = _make_cfg(tmp_path)
    store = _make_store(tmp_path, tracks)
    lib_c = tmp_path / "C_Library"
    lib_e = tmp_path / "E_Library"
    lib_c.mkdir(parents=True, exist_ok=True)
    lib_e.mkdir(parents=True, exist_ok=True)
    merger = LibraryMerge(
        cfg, store, lib_c=lib_c, lib_e=lib_e, check=LibraryCheck(cfg, store)
    )
    return merger, lib_c, lib_e, store


def _rel(t, number, name):
    return f"{ART}/{ALBUM}/{number:02d}. {name}.mp3"


# --- план ----------------------------------------------------------------


def test_plan_move(tmp_path):
    t = _track(T1, "First", downloaded=True)
    merger, lib_c, lib_e, _ = _make(tmp_path, [t])
    _write(lib_c, _rel(t, 1, "First"))

    plan = merger.plan()
    assert plan["counts"]["move"] == 1
    assert plan["counts"]["dup"] == 0
    assert list(plan["move"]) == [_rel(t, 1, "First")]
    # dry-run ничего не меняет
    assert (lib_c / _rel(t, 1, "First")).exists()
    assert not (lib_e / _rel(t, 1, "First")).exists()


def test_plan_dup_exact(tmp_path):
    t = _track(T1, "First", downloaded=True)
    merger, lib_c, lib_e, _ = _make(tmp_path, [t])
    _write(lib_c, _rel(t, 1, "First"))
    _write(lib_e, _rel(t, 1, "First"))

    plan = merger.plan()
    assert plan["counts"]["move"] == 0
    assert plan["counts"]["dup"] == 1
    assert plan["dup"][_rel(t, 1, "First")]["different"] is False


def test_plan_dup_fuzzy(tmp_path):
    t = _track(T1, "First", downloaded=True)
    t["file"] = f"Library/{ART}/{ALBUM}/01. First (2).mp3"
    merger, lib_c, lib_e, _ = _make(tmp_path, [t])
    _write(lib_c, "Band/Album/01. First (2).mp3")
    _write(lib_e, "Band/Album/01. First.mp3")

    plan = merger.plan()
    assert plan["counts"]["dup"] == 1
    assert plan["dup"]["Band/Album/01. First (2).mp3"]["fuzzy_e"] == "Band/Album/01. First.mp3"


def test_plan_ambiguous(tmp_path):
    t = _track(T1, "First")
    t["file"] = f"Library/{ART}/{ALBUM}/04. First.mp3"
    merger, lib_c, lib_e, _ = _make(tmp_path, [t])
    _write(lib_c, "Band/Album/04. First.mp3")
    _write(lib_e, "Band/Album/04. First (live).mp3")
    _write(lib_e, "Band/Album/04. First (remix).mp3")

    plan = merger.plan()
    assert plan["counts"]["ambiguous"] == 1
    assert plan["counts"]["move"] == 0
    assert len(plan["ambiguous"]["Band/Album/04. First.mp3"]["candidates_e"]) == 2


def test_plan_unknown(tmp_path):
    merger, lib_c, lib_e, _ = _make(tmp_path, [])
    _write(lib_c, "NoBand/NoAlbum/01. Strange.mp3", size=50)

    plan = merger.plan()
    assert plan["counts"]["unknown_move"] == 1
    assert list(plan["unknown_move"]) == ["NoBand/NoAlbum/01. Strange.mp3"]
    assert plan["counts"]["freed_bytes_c"] == 50


def test_plan_unknown_dup(tmp_path):
    merger, lib_c, lib_e, _ = _make(tmp_path, [])
    _write(lib_c, "NoBand/NoAlbum/01. Strange.mp3")
    _write(lib_e, "NoBand/NoAlbum/01. Strange.mp3")

    plan = merger.plan()
    assert plan["counts"]["unknown_dup"] == 1
    assert plan["counts"]["unknown_move"] == 0


def test_plan_artist_filter(tmp_path):
    t = _track(T1, "First")
    merger, lib_c, lib_e, _ = _make(tmp_path, [t])
    _write(lib_c, _rel(t, 1, "First"))
    _write(lib_c, "Other/Album/01. Other.mp3")

    plan = merger.plan(artist_filter="Band")
    assert plan["counts"]["move"] == 1
    assert plan["counts"]["unknown_move"] == 0
    # фильтр не дотягивает до других папок
    assert "Other/Album/01. Other.mp3" not in plan["move"]


# --- apply ---------------------------------------------------------------


def test_apply_move(tmp_path):
    t = _track(T1, "First", downloaded=True)
    merger, lib_c, lib_e, store = _make(tmp_path, [t])
    _write(lib_c, _rel(t, 1, "First"), size=77)

    result = merger.apply(merger.plan())
    assert result["counts"]["move"] == 1
    assert not (lib_c / _rel(t, 1, "First")).exists()
    assert (lib_e / _rel(t, 1, "First")).exists()
    assert (lib_e / _rel(t, 1, "First")).stat().st_size == 77
    assert store.tracks[T1]["file"] == "/mnt/e/Library/Band/Album/01. First.mp3"
    assert not result["errors"]


def test_apply_creates_album_folder(tmp_path):
    t = _track(T1, "First")
    merger, lib_c, lib_e, _ = _make(tmp_path, [t])
    _write(lib_c, _rel(t, 1, "First"))

    merger.apply(merger.plan())
    assert (lib_e / "Band" / "Album").is_dir()
    assert not (lib_c / "Band").exists()  # папка исполнителя опустела


def test_apply_dup_cut(tmp_path):
    t = _track(T1, "First", downloaded=True)
    merger, lib_c, lib_e, store = _make(tmp_path, [t])
    _write(lib_c, _rel(t, 1, "First"), size=100)
    _write(lib_e, _rel(t, 1, "First"), size=100)

    result = merger.apply(merger.plan())
    assert not (lib_c / _rel(t, 1, "First")).exists()
    assert (lib_e / _rel(t, 1, "First")).exists()
    assert store.tracks[T1]["file"] == "/mnt/e/Library/Band/Album/01. First.mp3"


def test_apply_dup_fuzzy_updates_db(tmp_path):
    t = _track(T1, "First")
    t["file"] = f"Library/{ART}/{ALBUM}/01. First (2).mp3"
    merger, lib_c, lib_e, store = _make(tmp_path, [t])
    _write(lib_c, "Band/Album/01. First (2).mp3")
    _write(lib_e, "Band/Album/01. First.mp3")

    merger.apply(merger.plan())
    assert not (lib_c / "Band/Album/01. First (2).mp3").exists()
    assert store.tracks[T1]["file"] == "/mnt/e/Library/Band/Album/01. First.mp3"


def test_apply_ambiguous_untouched(tmp_path):
    t = _track(T1, "First")
    t["file"] = f"Library/{ART}/{ALBUM}/04. First.mp3"
    merger, lib_c, lib_e, _ = _make(tmp_path, [t])
    _write(lib_c, "Band/Album/04. First.mp3")
    _write(lib_e, "Band/Album/04. First (live).mp3")
    _write(lib_e, "Band/Album/04. First (remix).mp3")

    merger.apply(merger.plan())
    assert (lib_c / "Band/Album/04. First.mp3").exists()


def test_apply_unknown_move(tmp_path):
    merger, lib_c, lib_e, store = _make(tmp_path, [])
    _write(lib_c, "NoBand/NoAlbum/01. Strange.mp3", size=33)

    merger.apply(merger.plan())
    assert not (lib_c / "NoBand/NoAlbum/01. Strange.mp3").exists()
    assert (lib_e / "NoBand/NoAlbum/01. Strange.mp3").exists()
    assert len(store.tracks) == 0  # база не тронута


def test_apply_idempotent(tmp_path):
    t = _track(T1, "First")
    merger, lib_c, lib_e, store = _make(tmp_path, [t])
    _write(lib_c, _rel(t, 1, "First"))

    merger.apply(merger.plan())
    # повторный apply после обрыва — ничего не ломает
    result = merger.apply(merger.plan())
    assert not result["errors"]
    assert (lib_e / _rel(t, 1, "First")).exists()


# --- безопасность (ревизия 15.08) ---------------------------------------


def test_plan_live_version_is_ambiguous(tmp_path):
    """'04. First.mp3' vs '04. First (live).mp3' — ДРУГОЙ трек, не дубль."""
    t = _track(T1, "First")
    t["file"] = f"Library/{ART}/{ALBUM}/04. First.mp3"
    merger, lib_c, lib_e, _ = _make(tmp_path, [t])
    _write(lib_c, "Band/Album/04. First.mp3")
    _write(lib_e, "Band/Album/04. First (live).mp3")

    plan = merger.plan()
    assert plan["counts"]["dup"] == 0
    assert plan["counts"]["ambiguous"] == 1
    assert len(plan["ambiguous"]["Band/Album/04. First.mp3"]["candidates_e"]) == 1


def test_plan_acoustic_version_is_ambiguous(tmp_path):
    """'04. Вечная.mp3' vs '04. Вечная (acoustic version).mp3' — не трогаем."""
    t = _track(T1, "First")
    t["file"] = f"Library/{ART}/{ALBUM}/04. First.mp3"
    merger, lib_c, lib_e, _ = _make(tmp_path, [t])
    _write(lib_c, "Band/Album/04. First.mp3")
    _write(lib_e, "Band/Album/04. First (acoustic version).mp3")

    plan = merger.plan()
    assert plan["counts"]["dup"] == 0
    assert plan["counts"]["ambiguous"] == 1


def test_plan_numeric_suffix_is_dup(tmp_path):
    """'02. First (2).mp3' vs '02. First.mp3' — мусор reorganize, дубль."""
    t = _track(T1, "First")
    t["file"] = f"Library/{ART}/{ALBUM}/02. First (2).mp3"
    merger, lib_c, lib_e, _ = _make(tmp_path, [t])
    _write(lib_c, "Band/Album/02. First (2).mp3")
    _write(lib_e, "Band/Album/02. First.mp3")

    plan = merger.plan()
    assert plan["counts"]["dup"] == 1
    assert plan["counts"]["ambiguous"] == 0


def test_apply_cut_requires_e_file(tmp_path):
    """E:-копия исчезла между планом и apply — C: не вырезается."""
    t = _track(T1, "First", downloaded=True)
    merger, lib_c, lib_e, store = _make(tmp_path, [t])
    _write(lib_c, _rel(t, 1, "First"))
    _write(lib_e, _rel(t, 1, "First"))
    plan = merger.plan()
    (lib_e / _rel(t, 1, "First")).unlink()  # копия пропала

    result = merger.apply(plan)
    assert (lib_c / _rel(t, 1, "First")).exists()  # C: не тронут
    assert any("не тронуто" in e for e in result["errors"])


def test_apply_cut_fuzzy_requires_e_file(tmp_path):
    """Fuzzy-дубль: E:-кандидат исчез — C: остаётся, ошибка в отчёте."""
    t = _track(T1, "First")
    t["file"] = f"Library/{ART}/{ALBUM}/02. First (2).mp3"
    merger, lib_c, lib_e, store = _make(tmp_path, [t])
    _write(lib_c, "Band/Album/02. First (2).mp3")
    _write(lib_e, "Band/Album/02. First.mp3")
    plan = merger.plan()
    (lib_e / "Band/Album/02. First.mp3").unlink()

    result = merger.apply(plan)
    assert (lib_c / "Band/Album/02. First (2).mp3").exists()
    assert any("не тронуто" in e for e in result["errors"])
