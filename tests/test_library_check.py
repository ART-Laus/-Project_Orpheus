"""Тесты проверки медиатеки (orpheus library check)."""

from pathlib import Path

from orpheus.config import Config
from orpheus.library_check import LibraryCheck, _norm_stem, _probe, _quality_key
from orpheus.models import Track
from orpheus.statuses import TrackStatus
from orpheus.store import Store

T1 = "t" * 22 + "1"
T2 = "t" * 22 + "2"
T3 = "t" * 22 + "3"
T4 = "t" * 22 + "4"
T5 = "t" * 22 + "5"
T6 = "t" * 22 + "6"
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
        "paths:\n  data_dir: data\n", encoding="utf-8"
    )
    cfg = Config(project_root=tmp_path)
    cfg.library_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _write(root: Path, rel: str, size: int = 100) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)


def _analyze(tmp_path, tracks):
    cfg = _make_cfg(tmp_path)
    store = _make_store(tmp_path, tracks)
    lib_c = tmp_path / "C_Library"
    lib_e = tmp_path / "E_Library"
    return LibraryCheck(cfg, store, lib_c=lib_c, lib_e=lib_e), lib_c, lib_e


# --- категории -----------------------------------------------------------


def test_only_c(tmp_path):
    t = _track(T1, "First", downloaded=True)
    _, lib_c, lib_e = _analyze(tmp_path, [t])
    _write(lib_c, "Band/Album/01. First.mp3")

    result = _analyze(tmp_path, [t])[0].analyze()
    assert result["counts"]["only_c"] == 1
    assert result["tracks"]["only_c"][0]["id"] == T1
    assert result["counts"]["only_e"] == 0


def test_only_e(tmp_path):
    t = _track(T1, "First")
    _, lib_c, lib_e = _analyze(tmp_path, [t])
    _write(lib_e, "Band/Album/01. First.mp3")

    result = _analyze(tmp_path, [t])[0].analyze()
    assert result["counts"]["only_e"] == 1
    assert result["tracks"]["only_e"][0]["id"] == T1


def test_both_same(tmp_path):
    t = _track(T1, "First", downloaded=True)
    _, lib_c, lib_e = _analyze(tmp_path, [t])
    _write(lib_c, "Band/Album/01. First.mp3")
    _write(lib_e, "Band/Album/01. First.mp3")

    result = _analyze(tmp_path, [t])[0].analyze()
    assert result["counts"]["both_same"] == 1
    assert result["counts"]["both_diff"] == 0


def test_both_diff(tmp_path):
    t = _track(T1, "First", downloaded=True)
    _, lib_c, lib_e = _analyze(tmp_path, [t])
    _write(lib_c, "Band/Album/01. First.mp3", size=100)
    _write(lib_e, "Band/Album/01. First.mp3", size=200)

    result = _analyze(tmp_path, [t])[0].analyze()
    assert result["counts"]["both_diff"] == 1
    v = result["variants"][0]
    assert v["c"]["size"] == 100
    assert v["e"]["size"] == 200
    assert v["canonical"] == "e"  # больше размер → лучше


def test_nowhere_fuzzy(tmp_path):
    t = _track(T1, "First")
    _, lib_c, lib_e = _analyze(tmp_path, [t])
    # файл на E: с другим форматом имени ("01 - First.mp3")
    _write(lib_e, "Band/Album/01 - First.mp3")

    result = _analyze(tmp_path, [t])[0].analyze()
    assert result["counts"]["nowhere"] == 1
    assert result["tracks"]["nowhere"][0]["fuzzy"]["e"] == ["Band/Album/01 - First.mp3"]
    # фаззи-найденный не попадает в download (файл существует)
    assert result["counts"]["download"] == 0


def test_download_and_phantom(tmp_path):
    pending = _track(T2, "Second")  # без downloaded
    phantom = _track(T3, "Third", downloaded=True)
    _, lib_c, lib_e = _analyze(tmp_path, [pending, phantom])

    result = _analyze(tmp_path, [pending, phantom])[0].analyze()
    assert result["counts"]["download"] == 1
    assert result["download"][0]["id"] == T2
    assert result["counts"]["phantom"] == 1
    assert result["phantom"][0]["id"] == T3


def test_e_not_mounted(tmp_path):
    t = _track(T1, "First", downloaded=True)
    _, lib_c, _ = _analyze(tmp_path, [t])
    _write(lib_c, "Band/Album/01. First.mp3")

    check, _, _ = _analyze(tmp_path, [t])
    result = check.analyze()
    assert result["counts"]["e_mounted"] is False
    assert result["counts"]["only_c"] == 1


# --- сравнение вариантов -------------------------------------------------


def test_quality_key_ranks_flac_above_mp3(tmp_path):
    flac = _probe(Path("/x.flac"))
    mp3 = _probe(Path("/x.mp3"))
    assert _quality_key(flac) > _quality_key(mp3)


def test_canonical_prefers_better_format(tmp_path):
    t = _track(T1, "First", downloaded=True)
    _, lib_c, lib_e = _analyze(tmp_path, [t])
    (lib_c / "Band/Album").mkdir(parents=True, exist_ok=True)
    (lib_e / "Band/Album").mkdir(parents=True, exist_ok=True)
    (lib_c / "Band/Album/01. First.mp3").write_bytes(b"m" * 300)
    (lib_e / "Band/Album/01. First.flac").write_bytes(b"f" * 400)

    result = _analyze(tmp_path, [t])[0].analyze()
    v = result["variants"][0]
    assert v["canonical"] == "e"  # FLAC на E: лучше MP3 на C:
    assert v["variant"] == "c"


def test_canonical_tie_prefers_c(tmp_path):
    t = _track(T1, "First", downloaded=True)
    _, lib_c, lib_e = _analyze(tmp_path, [t])
    _write(lib_c, "Band/Album/01. First.mp3", size=123)
    _write(lib_e, "Band/Album/01. First.mp3", size=123)
    # разное содержимое при равном размере — чтобы это был вариант (both_diff),
    # а не идентичный дубль (both_same)
    (lib_c / "Band/Album/01. First.mp3").write_bytes(b"c" * 123)
    (lib_e / "Band/Album/01. First.mp3").write_bytes(b"e" * 123)

    result = _analyze(tmp_path, [t])[0].analyze()
    assert result["counts"]["both_diff"] == 1
    v = result["variants"][0]
    assert v["canonical"] == "c"


# --- отчёт ----------------------------------------------------------------


def test_write_report(tmp_path):
    t = _track(T1, "First", downloaded=True)
    _, lib_c, lib_e = _analyze(tmp_path, [t])
    _write(lib_c, "Band/Album/01. First.mp3", size=100)
    _write(lib_e, "Band/Album/01. First.mp3", size=200)
    check, _, _ = _analyze(tmp_path, [t])
    result = check.analyze()
    md = check.write_report(result)
    assert md.exists()
    text = md.read_text(encoding="utf-8")
    assert "Варианты" in text
    assert "КАНОН" in text


def test_phantom_find(tmp_path):
    phantom = _track(T3, "Third", downloaded=True)
    fuzzy = _track(T4, "Fourth", downloaded=True, number=4)
    ok = _track(T5, "Fifth", downloaded=True, number=5)
    _, lib_c, lib_e = _analyze(tmp_path, [phantom, fuzzy, ok])
    _write(lib_e, "Band/Album/04 - Fourth.mp3")  # похожий файл по названию
    _write(lib_c, "Band/Album/05. Fifth.mp3")    # файл есть — не фантом
    check = _analyze(tmp_path, [phantom, fuzzy, ok])[0]

    result = check.find_phantoms()
    ids = {p["id"] for p in result["phantoms"]}
    assert T3 in ids
    assert T4 in ids
    assert T5 not in ids
    assert result["count"] == 2
    assert result["with_fuzzy"] == 1
    fp = next(p for p in result["phantoms"] if p["id"] == T4)
    assert fp["fuzzy_e"] == ["Band/Album/04 - Fourth.mp3"]
    assert fp["fuzzy_c"] == []


def test_phantom_report_written(tmp_path):
    t = _track(T3, "Third", downloaded=True)
    check, _, _ = _analyze(tmp_path, [t])
    result = check.find_phantoms()
    md = check.write_phantom_report(result)
    assert md.exists()
    text = md.read_text(encoding="utf-8")
    assert "Фантомы" in text
    assert "Без файла нигде" in text


def test_fix_paths_updates_file(tmp_path):
    t = _track(T3, "Third", downloaded=True, number=3)
    _, lib_c, lib_e = _analyze(tmp_path, [t])
    _write(lib_e, "Band/Album/03 - Third.mp3")  # файл под другим именем
    check = _analyze(tmp_path, [t])[0]

    result = check.fix_phantom_paths()
    assert len(result["fixed"]) == 1
    r = result["fixed"][0]
    assert r["fixed_from"] == "Band/Album/03. Third.mp3"
    assert r["fixed_to"] == "Band/Album/03 - Third.mp3"
    assert r["target"] == "e"
    assert not result["ambiguous"]
    assert check.store.tracks[T3]["file"] == "Library/Band/Album/03 - Third.mp3"


def test_fix_paths_ambiguous_left_alone(tmp_path):
    t = _track(T3, "Third", downloaded=True, number=3)
    _, lib_c, lib_e = _analyze(tmp_path, [t])
    _write(lib_e, "Band/Album/03 - Third.mp3")
    _write(lib_e, "Band/Album/03. Third (radio).mp3")  # два кандидата
    check = _analyze(tmp_path, [t])[0]

    result = check.fix_phantom_paths()
    assert not result["fixed"]
    assert len(result["ambiguous"]) == 1
    assert check.store.tracks[T3]["file"] == "Library/Band/Album/03. Third.mp3"


def test_fix_paths_prefers_c(tmp_path):
    t = _track(T3, "Third", downloaded=True, number=3)
    _, lib_c, lib_e = _analyze(tmp_path, [t])
    _write(lib_c, "Band/Album/03 - Third.mp3")
    _write(lib_e, "Band/Album/03 - Third.mp3")
    check = _analyze(tmp_path, [t])[0]

    result = check.fix_phantom_paths()
    assert len(result["fixed"]) == 1  # есть и на C:, и на E:
    assert result["fixed"][0]["target"] == "c"
    assert check.store.tracks[T3]["file"] == "Library/Band/Album/03 - Third.mp3"


def test_fix_report_written(tmp_path):
    t = _track(T3, "Third", downloaded=True, number=3)
    _, lib_c, lib_e = _analyze(tmp_path, [t])
    _write(lib_e, "Band/Album/03 - Third.mp3")
    check = _analyze(tmp_path, [t])[0]

    result = check.fix_phantom_paths()
    md = check.write_fix_report(result)
    assert md.exists()
    assert "Исправлено" in md.read_text(encoding="utf-8")


# --- хелперы --------------------------------------------------------------


def test_norm_stem():
    assert _norm_stem("01 - First.mp3") == _norm_stem("01. First.mp3")
    assert _norm_stem("First (2).mp3") == "first"
