"""Открытые торрент-трекеры: поиск релизов и скачивание через qBittorrent.

Один общий класс TorrentSource обслуживает все поддерживаемые трекеры:
конфиг описывает URL поиска, регулярки для строк таблицы результатов и
шаблон страницы торрента с magnet-ссылкой. Нужен только VPN для
заблокированных доменов (rutor.info и др.).

Парсинг:
  - страница поиска: строки таблицы с названием, размером, сидерами и
    тегами формата ([FLAC], [MP3 320]);
  - страница торрента: magnet-ссылка (или magnet прямо в результатах).
"""

from __future__ import annotations

import html as html_mod
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from ..models import Album, Track
from ..qbit_client import QbitClient
from .base import Candidate, MusicSource, SourceError

_SIZE_RE = re.compile(r"([\d.,]+)\s*(TB|GB|MB|KB)", re.I)
_FORMAT_TAG_RE = re.compile(r"\[([^\]]{2,20})\]")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"


@dataclass
class Release:
    """Один релиз-торрент на странице поиска."""

    title: str
    size_bytes: int = 0
    seeders: int = 0
    extension: str = ".mp3"
    bitrate: int = 0
    extra: dict = field(default_factory=dict)

    def to_candidate(self, source_name: str) -> Candidate:
        return Candidate(
            source=source_name,
            filename=self.title,
            size=self.size_bytes,
            bitrate=self.bitrate,
            extension=self.extension,
            extra=self.extra,
        )


def format_from_title(title: str) -> tuple[str, int]:
    """(расширение, битрейт) по тегам формата в названии релиза."""
    low = title.lower()
    if "[flac" in low or "[lossless" in low or "[ape]" in low or "[wavpack" in low:
        return ".flac", 0
    if "[aac" in low or "[m4a" in low or "[alac" in low:
        return ".m4a", 320
    if "[ogg" in low or "[opus" in low:
        return ".ogg", 320
    m = re.search(r"\[mp3\s*v?\s*(\d{3})", low)
    if m:
        return ".mp3", int(m.group(1))
    if "[mp3" in low:
        return ".mp3", 0
    return ".mp3", 0


def parse_size(text: str) -> int:
    m = _SIZE_RE.search(text or "")
    if not m:
        return 0
    value = float(m.group(1).replace(",", "."))
    mult = {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}[m.group(2).upper()]
    return int(value * mult)


def strip_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html_mod.unescape(text)).strip()


def _passes_quality(rel: Release, min_quality: str) -> bool:
    """Фильтр релизов по минимальному качеству (flac / mp3_320 / any)."""
    if min_quality == "any":
        return True
    if rel.extension == ".flac":
        return True
    if min_quality == "mp3_320":
        return rel.extension == ".mp3" and rel.bitrate >= 320
    return False


@dataclass
class TrackerSpec:
    """Описание одного трекера для TorrentSource."""

    name: str
    search_url: str  # шаблон: {query} подставляется
    row_re: str  # регулярка строки результата; группы: title, size, seeders
    magnet_re: str  # magnet на странице поиска (или None — смотреть страницу)
    page_url: str = ""  # шаблон страницы торрента: {id} подставляется
    id_re: str = r"(?:torrent|details|t)=?(\d+)"
    title_in_group: str = "title"
    size_in_group: str = "size"
    seeders_in_group: str = "seeders"
    encoding: str = "utf-8"
    min_quality: str = "any"


def build_specs() -> dict[str, TrackerSpec]:
    """Конфиг поддерживаемых трекеров (по умолчанию; можно перекрыть в config)."""
    return {
        "rutor": TrackerSpec(
            name="rutor",
            search_url="{base}/search/{query}/0/0/0/0",
            row_re=r'<tr[^>]*>\s*<td[^>]*>(?:\s*<a[^>]*>[^<]*</a>\s*)?</td>.*?'
                    r'<a href="/torrent/(\d+)[^"]*"[^>]*>(.*?)</a>.*?'
                    r'<span class="size">([^<]*)</span>.*?'
                    r'<span class="[^"]*green[^"]*"[^>]*>(\d+)</span>',
            magnet_re=r'href="(magnet:\?xt=urn:btih:[a-fA-F0-9]+[^"]*)"',
            page_url="{base}/torrent/{id}",
            title_in_group="title",
            size_in_group="size",
            seeders_in_group="seeders",
            encoding="utf-8",
        ),
        "rutor_direct": TrackerSpec(
            name="rutor_direct",
            search_url="{base}/search/{query}/0/0/0/0",
            row_re=r'<a href="/torrent/(\d+)[^"]*"[^>]*>(.*?)</a>.*?'
                    r'<span class="size">([^<]*)</span>.*?'
                    r'<span class="[^"]*green[^"]*"[^>]*>(\d+)</span>',
            magnet_re=r'href="(magnet:\?xt=urn:btih:[a-fA-F0-9]+[^"]*)"',
            page_url="{base}/torrent/{id}",
            encoding="utf-8",
        ),
        "x1337": TrackerSpec(
            name="x1337",
            search_url="{base}/search/{query}/1/",
            row_re=r'<a href="(/torrent/\d+/[^"]+)"[^>]*>(.*?)</a>.*?'
                    r'<td class="coll-2">([^<]*)</td>.*?'
                    r'<td class="coll-3">([^<]*)</td>',
            magnet_re=r'href="(magnet:\?xt=urn:btih:[a-fA-F0-9]+[^"]*)"',
            page_url="{base}{page}",
            title_in_group="title",
            size_in_group="size",
            seeders_in_group="seeders",
            encoding="utf-8",
        ),
        "tpb": TrackerSpec(
            name="tpb",
            search_url="{base}/search/{query}/0/99/200",
            row_re=r'<td[^>]*><a href="(magnet:[^"]+)"[^>]*>.*?</a></td>\s*'
                    r'<td[^>]*><a[^>]*>(.*?)</a></td>\s*'
                    r'<td[^>]*>([^<]*)</td>\s*'
                    r'<td[^>]*>(\d+)</td>',
            magnet_re=r'href="(magnet:\?xt=urn:btih:[a-fA-F0-9]+[^"]*)"',
            encoding="utf-8",
        ),
        "torrentino": TrackerSpec(
            name="torrentino",
            search_url="{base}/search/{query}/",
            row_re=r'<a href="(/torrents/\d+[^"]*)"[^>]*>(.*?)</a>.*?'
                    r'<span class="size">([^<]*)</span>.*?'
                    r'<span class="seeders">(\d+)</span>',
            magnet_re=r'href="(magnet:\?xt=urn:btih:[a-fA-F0-9]+[^"]*)"',
            page_url="{base}{page}",
            encoding="utf-8",
        ),
        "fast_torrent": TrackerSpec(
            name="fast_torrent",
            search_url="{base}/search?q={query}",
            row_re=r'<a href="(/dl/[^"]+)"[^>]*>(.*?)</a>.*?'
                    r'<span class="size">([^<]*)</span>.*?'
                    r'<span class="seeders">(\d+)</span>',
            magnet_re=r'href="(magnet:\?xt=urn:btih:[a-fA-F0-9]+[^"]*)"',
            page_url="{base}{page}",
            encoding="utf-8",
        ),
        "torrentdownloads": TrackerSpec(
            name="torrentdownloads",
            search_url="{base}/search?search={query}&sort=seeders",
            row_re=r'<a href="(/\d+/[^"]+\.html)"[^>]*>(.*?)</a>.*?'
                    r'<td[^>]*>([\d.,]+\s*(?:GB|MB|KB))</td>.*?'
                    r'<td[^>]*>(\d+)</td>',
            magnet_re=r'href="(magnet:\?xt=urn:btih:[a-fA-F0-9]+[^"]*)"',
            page_url="{base}{page}",
            encoding="utf-8",
        ),
        "limetorrents": TrackerSpec(
            name="limetorrents",
            search_url="{base}/search/all/{query}/",
            row_re=r'<a href="(/\d+/[^"]+\.html)"[^>]*>(.*?)</a>.*?'
                    r'<td class="tdnormal">([\d.,]+\s*(?:GB|MB|KB))</td>.*?'
                    r'<td[^>]*>(\d+)</td>',
            magnet_re=r'href="(magnet:\?xt=urn:btih:[a-fA-F0-9]+[^"]*)"',
            page_url="{base}{page}",
            encoding="utf-8",
        ),
    }


class TorrentSource(MusicSource):
    """Торрент-источник по спецификации трекера.

    Поиск: страница поиска -> парсинг строк -> кандидаты с magnet
    (прямо из результатов или со страницы торрента). Скачивание:
    magnet -> qBittorrent -> ожидание -> путь к папке.
    """

    name = "torrent"
    album_capable = True

    def __init__(
        self,
        spec: TrackerSpec,
        bases: list[str],
        qbit: QbitClient,
        torrents_dir: Path,
        min_quality: str = "any",
        timeout_s: int = 20,
    ):
        self.spec = spec
        self.bases = [b.rstrip("/") for b in bases]
        self.qbit = qbit
        self.torrents_dir = torrents_dir
        self.min_quality = min_quality
        self.timeout_s = timeout_s
        self.name = spec.name
        self._base = self.bases[0] if self.bases else ""

    # --- HTTP -----------------------------------------------------------

    def _fetch(self, url: str) -> str:
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": UA}),
                timeout=self.timeout_s,
            ) as resp:
                return resp.read().decode(self.spec.encoding, errors="replace")
        except urllib.error.URLError as exc:
            raise SourceError(f"{self.name}: {exc.reason} ({url})") from exc

    def _try_bases(self, path: str) -> str:
        """Попробовать все зеркала базы; вернуть (html, base)."""
        last = None
        for base in self.bases:
            try:
                return self._fetch(base + path), base
            except SourceError as exc:
                last = exc
        raise SourceError(str(last or f"{self.name}: нет доступных зеркал"))

    # --- доступность -----------------------------------------------------

    def available(self) -> bool:
        try:
            self._fetch(self.bases[0])
            return True
        except Exception:
            return False

    # --- поиск ------------------------------------------------------------

    def _search(self, query: str) -> list[Release]:
        last = None
        for base in self.bases:
            path = self.spec.search_url.format(base=base, query=urllib.parse.quote(query))
            try:
                page = self._fetch(path)
            except SourceError as exc:
                last = exc
                continue
            self._base = base
            return self._parse_rows(page)
        raise SourceError(str(last or f"{self.name}: нет доступных зеркал"))

    def _parse_rows(self, page: str) -> list[Release]:
        releases: list[Release] = []
        for m in re.finditer(self.spec.row_re, page, re.S):
            try:
                g = m.groups()
                first = (g[0] if g else "").strip()
                title = strip_tags(g[1] if len(g) > 1 else "")
                size = parse_size(g[2] if len(g) > 2 else "")
                seeders = int(re.search(r"\d+", g[3] or "")[0]) if len(g) > 3 and g[3] else 0
            except Exception:
                continue
            if not title:
                continue
            ext, bitrate = format_from_title(title)
            extra: dict = {}
            if first.startswith("magnet:"):
                extra["magnet"] = html_mod.unescape(first)
            else:
                extra["page_path"] = first
            releases.append(
                Release(
                    title=title,
                    size_bytes=size,
                    seeders=seeders,
                    extension=ext,
                    bitrate=bitrate,
                    extra=extra,
                )
            )
        return releases

    def _magnet_for(self, page_path: str) -> str | None:
        if not page_path:
            return None
        try:
            page = self._fetch(self._base + page_path)
        except Exception:
            return None
        m = re.search(self.spec.magnet_re, page)
        return html_mod.unescape(m.group(1)) if m else None

    def search_track(self, track: Track) -> list[Candidate]:
        query = " ".join(track.artist_names + [track.name]).strip()
        return self._search(query)

    def search_album(self, album: Album) -> list[Candidate]:
        artist = " ".join(album.artist_names) if album.artist_names else ""
        query = f"{artist} {album.name}".strip()
        cands = []
        for rel in self._search(query):
            if not _passes_quality(rel, self.min_quality):
                continue
            if rel.seeders <= 0:
                continue
            cands.append(rel.to_candidate(self.name))
        return cands

    # --- скачивание ------------------------------------------------------

    def download_album(self, cand: Candidate, dest_dir: Path) -> Path | None:
        return self._download(cand, dest_dir)

    def download_track(self, cand: Candidate, dest_dir: Path) -> Path | None:
        return self._download(cand, dest_dir)

    def _download(self, cand: Candidate, dest_dir: Path) -> Path | None:
        magnet = cand.extra.get("magnet") or self._magnet_for(cand.extra.get("page_path") or "")
        if not magnet:
            return None
        save_path = self.torrents_dir / f"{self.name}-{abs(hash(magnet))}"
        save_path.mkdir(parents=True, exist_ok=True)
        try:
            torrent_hash = self.qbit.add_torrent(magnet, save_path)
            torrent = self.qbit.wait_complete(torrent_hash)
            folder = Path(self.qbit.content_dir(torrent))
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(f"{self.name}: qBittorrent: {exc}") from exc
        return folder if folder.exists() else None


def build_torrent_sources(
    cfg,
    qbit: QbitClient,
    specs: dict[str, TrackerSpec] | None = None,
    raw_sections: list[dict] | None = None,
) -> list[MusicSource]:
    """Создание торрент-источников по config.yaml (sources.torrents.sources)."""
    specs = specs or build_specs()
    sources: list[MusicSource] = []
    for section in raw_sections if raw_sections is not None else cfg.torrents_config:
        name = section.get("name", "")
        spec = specs.get(name)
        if not spec or not section.get("enabled", True):
            continue
        bases = [b for b in section.get("bases", []) if b]
        if not bases:
            continue
        sources.append(
            TorrentSource(
                spec=spec,
                bases=bases,
                qbit=qbit,
                torrents_dir=cfg.data_dir / "torrents",
                min_quality=section.get("min_quality", "any"),
            )
        )
    return sources
