"""Источник Soulseek через headless Nicotine+ (мост HTTP API в WSL).

NicotineClient — тонкий HTTP-слой к мосту (orpheus.nicotine.bridge);
NicotineSource — адаптер под интерфейс MusicSource. Файлы мост
скачивает в download_dir; источник перекладывает их в staging.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from ..models import Track
from ..nicotine_client import NicotineClient
from .base import Candidate, MusicSource, SourceError

BRIDGE_CONNECT_WAIT_S = 60


class NicotineSource(MusicSource):
    name = "nicotine"

    def __init__(
        self,
        client: NicotineClient,
        download_dir: Path,
        data_dir: Path,
        log_dir: Path,
        username: str = "",
        password: str = "",
        search_timeout_s: int = 25,
    ):
        self.client = client
        self.download_dir = download_dir
        self.data_dir = data_dir
        self.log_dir = log_dir
        self.username = username
        self.password = password
        self.search_timeout_s = search_timeout_s

    def available(self) -> bool:
        try:
            resp = self.client.ping()
        except Exception:
            resp = None
        if resp is not None:
            return bool(resp.get("connected"))
        # Мост не запущен — поднимаем его сами и ждём логина в Soulseek
        spawned = self.client.ensure_running(
            base_url=self.client.base_url,
            username=self.username,
            password=self.password,
            data_dir=self.data_dir,
            download_dir=self.download_dir,
            log_path=self.log_dir / "nicotine-bridge.log",
        )
        if not spawned:
            return False
        deadline = time.time() + BRIDGE_CONNECT_WAIT_S
        while time.time() < deadline:
            if self.client.connected():
                return True
            time.sleep(2)
        return False

    def search_track(self, track: Track) -> list[Candidate]:
        query = f"{' '.join(track.artist_names)} {track.name}"
        try:
            results = self.client.search(query)
        except Exception as exc:
            raise SourceError(f"nicotine: {exc}") from exc
        cands: list[Candidate] = []
        for item in results:
            path = item.get("path", "")
            username = item.get("username", "")
            if not path or not username:
                continue
            cands.append(
                Candidate(
                    source=self.name,
                    filename=path,
                    size=item.get("size", 0) or 0,
                    bitrate=item.get("bitrate", 0) or 0,
                    length_s=item.get("length", 0) or 0,
                    extension=Path(path).suffix.lower(),
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
            state = self.client.wait_download(transfer_id)
        except Exception as exc:
            raise SourceError(f"nicotine: {exc}") from exc
        if state.get("status") != "Finished" or not state.get("path"):
            return None
        file_path = Path(state["path"])
        if not file_path.exists():
            return None
        dest = dest_dir / file_path.name
        shutil.move(str(file_path), str(dest))
        return dest
