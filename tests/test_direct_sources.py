"""Тесты прямых источников: Bandcamp, Jamendo, Zaycev (парсинг JSON/HTML)."""

import json
import urllib.parse

from orpheus.sources.bandcamp import _parse_tralbum, html_unescape
from orpheus.sources.jamendo import _bitrate_from_url
from orpheus.sources.zaycev import ZaycevClient

TRALBUM = {
    "artist": "Test Artist",
    "current": {"title": "Test Album"},
    "trackinfo": [
        {
            "title": "Track One",
            "duration": 180.5,
            "file": {"mp3-128": "https://bcbits.com/stream/track1.mp3"},
        }
    ],
}


def test_tralbum_parse():
    data = _parse_tralbum(TRALBUM)
    assert data["artist"] == "Test Artist"
    assert data["title"] == "Test Album"
    assert data["mp3"] == "https://bcbits.com/stream/track1.mp3"
    assert data["duration_ms"] == 180500


def test_html_unescape():
    assert html_unescape("a &amp; b &quot;c&quot;") == 'a & b "c"'


def test_jamendo_bitrate():
    assert _bitrate_from_url("https://mp3.jamendo.com/file-320.mp3") == 320
    assert _bitrate_from_url("https://mp3.jamendo.com/file-128.mp3") == 128
    assert _bitrate_from_url("https://mp3.jamendo.com/no.mp3") == 0


ZAYCEV_SEARCH = """\
<html><head><title>search</title></head><body>
<script id="__NEXT_DATA__" type="application/json">%s</script>
</body></html>
"""


def _zaycev_search_html(playlist: dict) -> str:
    data = {
        "props": {
            "initialReduxState": {
                "playlist": {
                    "info": playlist,
                }
            }
        }
    }
    return ZAYCEV_SEARCH % json.dumps(data, ensure_ascii=False)


def test_zaycev_search_parse():
    playlist = {
        "21755936": {
            "track": "Nirvana",
            "artistName": "Test Artist",
            "bitrate": 320,
            "duration": "03:20",
        },
        "24591354": {
            "track": "Second",
            "artistName": "Other",
            "bitrate": 96,
            "duration": "02:30",
            "downloadEnabled": False,
        },
    }
    # парсим локально через приватный _search_once с подменённым _fetch
    client = ZaycevClient()
    client._fetch = lambda path, data=None: _zaycev_search_html(playlist)
    items = client._search_once("nirvana")
    assert len(items) == 1
    assert items[0]["id"] == "21755936"
    assert items[0]["duration_ms"] == 200000
    assert items[0]["artist"] == "Test Artist"


def test_zaycev_duration_parse():
    from orpheus.sources.zaycev import _parse_duration

    assert _parse_duration("03:50") == 230000
    assert _parse_duration("1:02:33") == 3753000
    assert _parse_duration("") == 0


def test_zaycev_search_fallback_words():
    # полный запрос пуст, но слово даёт результат
    client = ZaycevClient()
    calls = []

    def fake_fetch(path, data=None):
        calls.append(path)
        q = urllib.parse.unquote(path.split("query_search=")[1].split("&")[0])
        if q.startswith("Длинный"):
            return ZAYCEV_SEARCH % json.dumps(
                {"props": {"initialReduxState": {"playlist": {"info": {}}}}}
            )
        return _zaycev_search_html(
            {
                "1": {
                    "track": "Короткое",
                    "artistName": "Исп",
                    "bitrate": 320,
                    "duration": "01:00",
                }
            }
        )

    client._fetch = fake_fetch
    items = client.search("Длинный запрос с Короткое")
    assert len(items) == 1
    assert items[0]["id"] == "1"
    assert len(calls) >= 2


def test_zaycev_next_data_empty():
    assert ZaycevClient._next_data("<html>no data</html>") == {}
