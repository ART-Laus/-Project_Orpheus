"""Тесты открытых торрент-трекеров: парсинг страниц поиска и качества."""

from pathlib import Path

from orpheus.sources.torrents import (
    Release,
    TorrentSource,
    format_from_title,
    parse_size,
    strip_tags,
)

RUTOR_HTML = """\
<html><body>
<table>
<tr class="tum">
  <td><a href="/torrent/100001">Категория</a></td>
  <td><a href="/torrent/100001" title="Смотреть">Artist - Album [FLAC]</a></td>
  <td><span class="size">1.2 GB</span></td>
  <td><span class="green">55</span> <span class="red">5</span></td>
</tr>
<tr class="gai">
  <td><a href="/torrent/100002">Категория</a></td>
  <td><a href="/torrent/100002" title="Смотреть">Artist - Album [MP3 320 kbps]</a></td>
  <td><span class="size">150 MB</span></td>
  <td><span class="green">12</span> <span class="red">0</span></td>
</tr>
<tr class="tum">
  <td><a href="/torrent/100003">Категория</a></td>
  <td><a href="/torrent/100003" title="Смотреть">Artist - Album [MP3 V0]</a></td>
  <td><span class="size">120 MB</span></td>
  <td><span class="green">0</span> <span class="red">0</span></td>
</tr>
</table>
</body></html>"""

RUTOR_MAGNET = '<a href="magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01">magnet</a>'

X1337_HTML = """\
<html><body>
<table>
<tbody>
<tr>
  <td class="coll-1 name"><a href="/torrent/1234567/Artist-Album-FLAC/">Artist - Album [FLAC]</a></td>
  <td class="coll-2">2.5 GB</td>
  <td class="coll-3">88</td>
  <td class="coll-4">9</td>
</tr>
<tr>
  <td class="coll-1 name"><a href="/torrent/7654321/Artist-Album-MP3/">Artist - Album [MP3 320]</a></td>
  <td class="coll-2">300 MB</td>
  <td class="coll-3">22</td>
  <td class="coll-4">1</td>
</tr>
</tbody>
</table>
</body></html>"""

TPB_HTML = """\
<html><body>
<div id="searchResult">
<table>
<tr>
  <td><a href="magnet:?xt=urn:btih:AAABBB0000000000000000000000000000000000&amp;dn=Artist-Album">Magnet link</a></td>
  <td><a href="/torrent/1">Artist - Album [FLAC]</a></td>
  <td>2.1&nbsp;GiB</td>
  <td>33</td>
  <td>2</td>
</tr>
</table>
</div>
</body></html>"""

LIMETORRENTS_HTML = """\
<html><body>
<div class="table2">
<div class="table-row">
  <a href="/123456/Artist-Album-FLAC.html">Artist - Album [FLAC]</a>
  <td class="tdnormal">1.5 GB</td>
  <td>77</td>
</div>
<div class="table-row">
  <a href="/654321/Artist-Album-MP3.html">Artist - Album [MP3 320]</a>
  <td class="tdnormal">250 MB</td>
  <td>15</td>
</div>
</div>
</body></html>"""


def test_format_from_title():
    assert format_from_title("[FLAC] Album [Lossless]") == (".flac", 0)
    assert format_from_title("Album [MP3 320 kbps]") == (".mp3", 320)
    assert format_from_title("Album [MP3 V0]") == (".mp3", 0)
    assert format_from_title("Album [AAC]") == (".m4a", 320)
    assert format_from_title("Album") == (".mp3", 0)


def test_parse_size():
    assert parse_size("1.5 GB") == int(1.5 * 1024**3)
    assert parse_size("320 MB") == 320 * 1024**2
    assert parse_size("12 KB") == 12 * 1024
    assert parse_size("нет данных") == 0


def test_strip_tags():
    assert strip_tags("<b>  Artist   </b> - <i>Album</i>") == "Artist - Album"


def test_rutor_parse_rows():
    src = _source("rutor", RUTOR_HTML)
    rows = src._parse_rows(RUTOR_HTML)
    assert len(rows) == 3
    assert rows[0].extension == ".flac"
    assert rows[0].size_bytes == int(1.2 * 1024**3)
    assert rows[0].seeders == 55
    assert rows[1].extension == ".mp3" and rows[1].bitrate == 320
    assert rows[2].seeders == 0


def test_rutor_quality_filter():
    src = _source("rutor", RUTOR_HTML, min_quality="mp3_320")
    src._fetch = lambda url: RUTOR_HTML  # type: ignore[method-assign]
    cands = src.search_album(_album())
    # FLAC и MP3 320 проходят; MP3 V0 (без битрейта) и 0 сидеров отсеиваются
    assert len(cands) == 2
    assert cands[0].extension == ".flac"
    assert cands[1].bitrate == 320


def test_x1337_parse_rows():
    src = _source("x1337", X1337_HTML)
    rows = src._parse_rows(X1337_HTML)
    assert len(rows) == 2
    assert rows[0].seeders == 88
    assert rows[0].extra["page_path"] == "/torrent/1234567/Artist-Album-FLAC/"
    assert rows[1].extension == ".mp3" and rows[1].bitrate == 320


def test_tpb_parse_rows():
    src = _source("tpb", TPB_HTML)
    rows = src._parse_rows(TPB_HTML)
    assert len(rows) == 1
    assert rows[0].seeders == 33
    assert rows[0].extension == ".flac"
    assert "AAABBB0000" in rows[0].extra["magnet"]


def test_limetorrents_parse_rows():
    src = _source("limetorrents", LIMETORRENTS_HTML)
    rows = src._parse_rows(LIMETORRENTS_HTML)
    assert len(rows) == 2
    assert rows[0].size_bytes == int(1.5 * 1024**3)
    assert rows[1].extension == ".mp3" and rows[1].bitrate == 320


def test_available_false_on_error():
    src = _source("rutor", "")
    src._fetch = lambda url: (_ for _ in ()).throw(OSError("net"))  # type: ignore[method-assign, assignment]
    assert src.available() is False


def test_magnet_from_page():
    src = _source("rutor", "")
    src._fetch = lambda url: RUTOR_MAGNET  # type: ignore[method-assign]
    magnet = src._magnet_for("/torrent/100001")
    assert magnet and magnet.startswith("magnet:?xt=urn:btih:")


def _source(name: str, html: str, min_quality: str = "any") -> TorrentSource:
    from orpheus.sources.torrents import build_specs

    spec = build_specs()[name]
    src = TorrentSource(
        spec=spec,
        bases=["https://example.test"],
        qbit=None,  # type: ignore[arg-type]
        torrents_dir=Path("/tmp/x"),
        min_quality=min_quality,
    )
    return src


def _album():
    from orpheus.models import Album

    return Album.from_dict(
        {
            "spotify_id": "a1",
            "name": "Album",
            "artist_names": ["Artist"],
            "artist_ids": ["ar1"],
            "release_date": "2020-01-01",
        }
    )
