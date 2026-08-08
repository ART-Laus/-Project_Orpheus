"""Клиент HTTP API моста Nicotine+ (headless Soulseek в WSL).

Мост (src/orpheus/nicotine/bridge.py) запускается как демон:
python -m orpheus.nicotine.bridge. Скачанные файлы мост кладёт в свой
download_dir (общий диск) -- источник забирает их оттуда.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SEARCH_TIMEOUT_S = 25
POLL_INTERVAL_S = 1.5
BRIDGE_SPAWN_TIMEOUT_S = 90


class NicotineError(RuntimeError):
    pass


class NicotineClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:5390",
        search_timeout_s: int = SEARCH_TIMEOUT_S,
    ):
        self.base_url = base_url.rstrip("/")
        self.search_timeout_s = search_timeout_s

    # --- низкий уровень ----------------------------------------------------

    def _request(self, method: str, path: str, body: Any = None, timeout: int = 15) -> Any:
        req = urllib.request.Request(self.base_url + path, method=method)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:200]
            raise NicotineError(f"nicotine HTTP {exc.code} на {path}: {detail!r}") from exc
        except urllib.error.URLError as exc:
            raise NicotineError(f"nicotine недоступен ({exc.reason}): {self.base_url}") from exc

    def ping(self) -> dict | None:
        """Живой ли мост. None — мост не отвечает."""
        try:
            return self._request("GET", "/ping", timeout=5)
        except NicotineError:
            return None

    def connected(self) -> bool:
        resp = self.ping()
        return bool(resp and resp.get("connected"))

    # --- высокий уровень ---------------------------------------------------

    def search(self, query: str) -> list[dict]:
        """Поиск в сети; ожидание ответов до таймаута. Список файлов."""
        token = self._request("POST", "/search", {"query": query}).get("token")
        if token is None:
            return []
        items: dict[tuple[str, str], dict] = {}
        deadline = time.time() + self.search_timeout_s
        while time.time() < deadline:
            for item in self._request("GET", f"/results?token={token}").get("results", []):
                items[(item.get("username", ""), item.get("path", ""))] = item
            time.sleep(POLL_INTERVAL_S)
        return list(items.values())

    def enqueue_download(self, username: str, filename: str, size: int) -> str:
        """Постановка файла в очередь скачивания; возвращает id передачи."""
        resp = self._request(
            "POST", "/download",
            {"username": username, "path": filename, "size": size},
        )
        return resp["id"]

    def transfer(self, transfer_id: str) -> dict:
        """Состояние передачи: {status, size, path}."""
        path = f"/transfer?id={urllib.parse.quote(transfer_id, safe='')}"
        return self._request("GET", path)

    def wait_download(self, transfer_id: str, timeout_s: int = 300) -> dict:
        """Ожидание завершения передачи; возвращает её состояние."""
        # "Connection closed/timeout" идут в авто-повтор ядра — ждём дальше
        done = {"Queued", "Getting status", "Transferring", "Paused",
                "Connection closed", "Connection timeout"}
        terminal_fail = {"User logged off", "Cancelled", "Filtered",
                         "Download folder error", "Local file error"}
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            state = self.transfer(transfer_id)
            status = state.get("status", "NOT_FOUND")
            if status == "Finished" or status in terminal_fail or status == "NOT_FOUND":
                return state
            time.sleep(POLL_INTERVAL_S)
        return {"status": "Timeout", "size": 0, "path": None}

    # --- запуск моста ------------------------------------------------------

    @staticmethod
    def ensure_running(
        base_url: str,
        username: str,
        password: str,
        data_dir: Path,
        download_dir: Path,
        log_path: Path,
        timeout_s: int = BRIDGE_SPAWN_TIMEOUT_S,
    ) -> bool:
        """Запустить мост, если он ещё не отвечает. True — мост работает."""
        client = NicotineClient(base_url=base_url)
        if client.ping() is not None:
            return True

        if not username or not password:
            return False

        env = dict(os.environ)
        parsed = urllib.parse.urlsplit(base_url)
        env.update(
            {
                "ORPHEUS_NICOTINE_USERNAME": username,
                "ORPHEUS_NICOTINE_PASSWORD": password,
                "ORPHEUS_NICOTINE_HOST": parsed.hostname or "127.0.0.1",
                "ORPHEUS_NICOTINE_PORT": str(parsed.port or 5390),
                "ORPHEUS_NICOTINE_DATA_DIR": str(data_dir),
                "ORPHEUS_NICOTINE_DOWNLOAD_DIR": str(download_dir),
            }
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as log_file:
            subprocess.Popen(
                [sys.executable, "-m", "orpheus.nicotine.bridge"],
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
            )

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if client.ping() is not None:
                return True
            time.sleep(2)
        return False
