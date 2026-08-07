"""Клиент REST API локального демона slskd (Soulseek).

Демон запускается отдельно (tools/slskd/), API-ключ берётся из его конфига.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SEARCH_TIMEOUT_S = 25
POLL_INTERVAL_S = 1.5


class SlskdError(RuntimeError):
    pass


class SlskdClient:
    def __init__(
        self,
        base_url: str = "http://localhost:5030",
        api_key: str = "",
        search_timeout_s: int = SEARCH_TIMEOUT_S,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.search_timeout_s = search_timeout_s

    @staticmethod
    def api_key_from_config(config_path: Path) -> str:
        """API-ключ из slskd.yml (tools/slskd/slskd.yml)."""
        if not config_path.exists():
            return ""
        text = config_path.read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("key:"):
                return line.split(":", 1)[1].strip().strip('"')
        return ""

    def _request(self, method: str, path: str, body: Any = None) -> Any:
        req = urllib.request.Request(
            self.base_url + path,
            method=method,
            headers={"X-API-Key": self.api_key},
        )
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, data=data, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raise SlskdError(f"slskd HTTP {exc.code} на {path}: {exc.read()[:200]!r}") from exc
        except urllib.error.URLError as exc:
            raise SlskdError(f"slskd недоступен ({exc.reason}): {self.base_url}") from exc

    def search(self, query: str) -> list[dict]:
        """Поиск по сети; ожидание ответов до таймаута. Возвращает список response."""
        resp = self._request("POST", "/api/v0/searches", {"searchText": query})
        sid = resp["id"]
        deadline = time.time() + self.search_timeout_s
        while time.time() < deadline:
            state = self._request("GET", f"/api/v0/searches/{sid}")
            if state.get("state") in ("Completed", "Cancelled", "TimedOut", "Rejected"):
                break
            time.sleep(POLL_INTERVAL_S)
        return self._request("GET", f"/api/v0/searches/{sid}/responses")

    def enqueue_download(self, username: str, filename: str, size: int) -> str:
        """Постановка файла в очередь скачивания; возвращает id передачи."""
        resp = self._request(
            "POST",
            f"/api/v0/transfers/downloads/{username}",
            [{"filename": filename, "size": size}],
        )
        return resp["enqueued"][0]["id"]

    def list_downloads(self) -> list[dict]:
        """Все передачи (скачивания) с полным состоянием."""
        return self._request("GET", "/api/v0/transfers/downloads")

    def wait_download(self, transfer_id: str, timeout_s: int = 300) -> dict:
        """Ожидание завершения передачи; возвращает её объект."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            for group in self.list_downloads():
                for folder in group.get("directories", []):
                    for f in folder.get("files", []):
                        if f.get("id") == transfer_id:
                            if "Succeeded" in f.get("state", ""):
                                return f
                            if "Aborted" in f.get("state", "") or "Failed" in f.get("state", ""):
                                return f
            time.sleep(POLL_INTERVAL_S)
        return {"state": "Timeout"}
