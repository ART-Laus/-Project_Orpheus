from orpheus.statuses import (
    TrackStatus,
    add_status,
    default_statuses,
    has_status,
)


def test_enum_values_stable():
    assert TrackStatus.DOWNLOADED.value == "downloaded"
    assert TrackStatus.METADATA_VERIFIED.value == "metadata_verified"
    assert TrackStatus.COVER_VERIFIED.value == "cover_verified"
    assert TrackStatus.CANONICAL_VERSION.value == "canonical_version"
    assert TrackStatus.MANUAL_REVIEW.value == "manual_review"
    assert TrackStatus.REPLACED_WITH_ORIGINAL.value == "replaced_with_original"


def test_default_statuses_is_copy():
    a = default_statuses()
    a.append("x")
    b = default_statuses()
    assert "x" not in b


def test_add_status_is_idempotent():
    statuses = default_statuses()
    add_status(statuses, TrackStatus.DOWNLOADED)
    add_status(statuses, TrackStatus.DOWNLOADED)
    assert statuses.count("downloaded") == 1
    assert has_status(statuses, TrackStatus.DOWNLOADED)
