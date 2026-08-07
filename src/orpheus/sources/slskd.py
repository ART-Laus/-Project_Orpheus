"""Источник Soulseek через локальный демон slskd.

SlskdClient — тонкий REST-слой; SlskdSource — адаптер под интерфейс
MusicSource (поиск по трекам, скачивание в папку загрузок демона).
"""

from __future__ import annotations

from pathlib import Path

from ..models import Track
from ..slskd_client import SlskdClient
from .base import Candidate, MusicSource, SourceError


class SlskdSource(MusicSource):
    name = "slskd"

    def __init__(
        self,
        base_url: str = "http://localhost:5030",
        api_key: str = "",
        downloads_dirs: list[Path] | None = None,
        search_timeout_s: int = 25,
    ):
        self.client = SlskdClient(
            base_url=base_url,
            api_key=api_key,
            search_timeout_s=search_timeout_s,
        )
        self.downloads_dirs = downloads_dirs or []

    def available(self) -> bool:
        try:
            self.client.list_downloads()
            return True
        except Exception:
            return False

    def search_track(self, track: Track) -> list[Candidate]:
        query = f"{' '.join(track.artist_names)} {track.name}"
        try:
            responses = self.client.search(query)
        except Exception as exc:
            raise SourceError(f"slskd: {exc}") from exc
        cands: list[Candidate] = []
        for resp in responses:
            username = resp.get("username", "")
            for f in resp.get("files", []):
                filename = f.get("filename", "")
                if not username or not filename:
                    continue
                cands.append(
                    Candidate(
                        source=self.name,
                        filename=filename,
                        size=f.get("size", 0) or 0,
                        bitrate=f.get("bitRate", 0) or 0,
                        length_s=f.get("length", 0) or 0,
                        extension=Path(filename).suffix.lower(),
                        extra={"username": username},
                    )
                )
        return cands

    def download_track(self, cand: Candidate, dest_dir: Path) -> Path | None:
        username = cand.extra.get("username", "")
        if not username:
            return None
        try:
            transfer_id = self.client.enqueue_download(username, cand.filename, cand.size)
        except Exception as exc:
            raise SourceError(f"slskd: {exc}") from exc
        result = self.client.wait_download(transfer_id)
        if "Succeeded" not in result.get("state", ""):
            return None
        name = Path(cand.filename).name
        for d in self.downloads_dirs:
            p = d / name
            if p.exists():
                return p
        return None
