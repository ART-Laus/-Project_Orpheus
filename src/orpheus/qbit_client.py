"""Клиент WebUI qBittorrent (API v2) для скачивания торрентов.

qBittorrent-nox запускается как системный сервис; этот клиент умеет
логиниться, добавлять торренты по magnet-ссылке, ждать завершения
и отдавать пути к скачанным файлам.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

POLL_INTERVAL_S = 2.0


class QbitError(RuntimeError):
    pass


class QbitClient:
    def __init__(self, base_url: str = "http://localhost:8080", username: str = "", password: str = ""):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        # кука SID из /auth/login должна сохраняться для следующих запросов
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())

    def _headers(self) -> dict[str, str]:
        return {"Referer": self.base_url + "/"}

    def _post(self, path: str, data: dict[str, str]) -> bytes:
        body = urllib.parse.urlencode(data).encode()
        try:
            with self._opener.open(
                urllib.request.Request(
                    self.base_url + path,
                    data=body,
                    headers={**self._headers(), "Content-Type": "application/x-www-form-urlencoded"},
                ),
                timeout=15,
            ) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            raise QbitError(f"qBittorrent HTTP {exc.code} на {path}") from exc
        except urllib.error.URLError as exc:
            raise QbitError(f"qBittorrent недоступен ({exc.reason}): {self.base_url}") from exc

    def _get(self, path: str) -> Any:
        try:
            with self._opener.open(
                urllib.request.Request(self.base_url + path, headers=self._headers()), timeout=15
            ) as resp:
                return json.loads(resp.read().decode() or "[]")
        except urllib.error.HTTPError as exc:
            raise QbitError(f"qBittorrent HTTP {exc.code} на {path}") from exc
        except urllib.error.URLError as exc:
            raise QbitError(f"qBittorrent недоступен ({exc.reason}): {self.base_url}") from exc

    def login(self) -> bool:
        """Логин в WebUI; кука SID сохраняется в opener."""
        try:
            resp = self._post("/api/v2/auth/login", {"username": self.username, "password": self.password})
            return resp.decode().strip() == "Ok."
        except QbitError:
            return False

    def add_torrent(self, magnet: str, save_path: str | Path) -> str:
        """Добавить торрент по magnet-ссылке; вернуть hash."""
        data = {"urls": magnet, "savepath": str(save_path)}
        self._post("/api/v2/torrents/add", data)
        deadline = time.time() + 20
        while time.time() < deadline:
            for t in self._get("/api/v2/torrents/info"):
                if t.get("magnet_uri", "").startswith("magnet:?xt=urn:btih:") and (
                    t.get("hash", "").lower() in magnet.lower() or _magnet_hash(t.get("magnet_uri", "")) in magnet.lower()
                ):
                    return t["hash"]
            time.sleep(1.0)
        raise QbitError("торрент не появился в qBittorrent")

    def torrent_info(self, torrent_hash: str) -> dict | None:
        for t in self._get("/api/v2/torrents/info"):
            if t.get("hash") == torrent_hash:
                return t
        return None

    def wait_complete(self, torrent_hash: str, timeout_s: int = 3600) -> dict:
        """Ожидание завершения скачивания; возвращает объект торрента."""
        deadline = time.time() + timeout_s
        last = None
        while time.time() < deadline:
            t = self.torrent_info(torrent_hash)
            if t is None:
                raise QbitError("торрент исчез из qBittorrent")
            last = t
            if t.get("progress", 0) >= 1.0 and t.get("state") not in ("error", "missingFiles", "stoppedDL"):
                return t
            time.sleep(POLL_INTERVAL_S)
        raise QbitError("таймаут ожидания торрента")

    def files(self, torrent_hash: str) -> list[dict]:
        """Список файлов торрента: {name, size, ...} (name относительно корня торрента)."""
        return self._get(f"/api/v2/torrents/files?hash={torrent_hash}")

    def content_dir(self, torrent: dict) -> str:
        """Корневая папка с файлами: save_path + первый каталог торрента (если есть)."""
        files = self.files(torrent["hash"])
        root = Path(torrent.get("save_path", ""))
        names = [f.get("name", "") for f in files if f.get("name")]
        if not names:
            return str(root)
        parts = names[0].split("/")
        if len(parts) > 1 and len(names) > 1:
            root = root / parts[0]
        return str(root)

    def delete_torrent(self, torrent_hash: str, delete_files: bool = False) -> None:
        self._post(
            "/api/v2/torrents/delete",
            {"hashes": torrent_hash, "deleteFiles": "true" if delete_files else "false"},
        )


def _magnet_hash(magnet: str) -> str:
    """btih-хэш из magnet-ссылки (нижний регистр)."""
    for part in magnet.split("&"):
        if part.startswith("xt=urn:btih:"):
            return part.split(":", 2)[2].lower()
    return ""
