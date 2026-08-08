"""Мост к headless-ядру Nicotine+ (Soulseek) в WSL.

Запускает pynicotine.core без GUI (GTK не требуется, единственная
зависимость -- сам пакет nicotine-plus) и отдаёт его по HTTP API:

  GET  /ping                -> {"ok": true, "connected": bool}
  POST /search              {"query": "..."}         -> {"token": int}
  GET  /results?token=123   -> {"term": ..., "results": [...]}
  POST /download            {"username", "path", "size"} -> {"id": str}
  GET  /transfer?id=<url>   -> {"status": "...", "path": ...|null, "size": int}

Скачанные файлы кладутся в каталог загрузок (download_dir); Orpheus
забирает их оттуда напрямую (общий диск WSL). Конфигурация -- через
переменные окружения ORPHEUS_NICOTINE_*, чтобы секреты не попадали
в командную строку. Запуск: python -m orpheus.nicotine.bridge
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5390

# Компоненты ядра по умолчанию (как в core.init_components) минус "cli":
# CLI-промпт читает stdin, он нам не нужен.
ENABLED_COMPONENTS = {
    "error_handler", "signal_handler", "portmapper", "network_thread", "shares", "users",
    "notifications", "network_filter", "now_playing", "statistics", "update_checker",
    "search", "downloads", "uploads", "interests", "userbrowse", "userinfo", "buddies",
    "chatrooms", "privatechat", "pluginhandler",
}

# Сколько времени храним результаты поиска (пока Orpheus их опрашивает)
SEARCH_TTL_S = 600.0
SEARCH_MAX_ITEMS = 1500


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def bridge_defaults(project_root: str | None = None) -> dict[str, str]:
    """Параметры моста по умолчанию (можно переопределить окружением)."""
    root = project_root or os.getcwd()
    data_dir = _env("ORPHEUS_NICOTINE_DATA_DIR", os.path.join(root, "data", "nicotine"))
    download_dir = _env("ORPHEUS_NICOTINE_DOWNLOAD_DIR", os.path.join(root, "data", "tmp", "nicotine"))
    return {
        "host": _env("ORPHEUS_NICOTINE_HOST", DEFAULT_HOST),
        "port": _env("ORPHEUS_NICOTINE_PORT", str(DEFAULT_PORT)),
        "username": _env("ORPHEUS_NICOTINE_USERNAME"),
        "password": _env("ORPHEUS_NICOTINE_PASSWORD"),
        "data_dir": data_dir,
        "download_dir": download_dir,
    }


class BridgeError(RuntimeError):
    pass


class Bridge:
    """Headless-ядро Nicotine+ + HTTP API. Запуск: run()."""

    def __init__(self, host: str, port: int, username: str, password: str,
                 data_dir: str, download_dir: str):
        if not username or not password:
            raise BridgeError(
                "Задайте ORPHEUS_NICOTINE_USERNAME и ORPHEUS_NICOTINE_PASSWORD "
                "(учётная запись Soulseek)"
            )
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.data_dir = data_dir
        self.download_dir = download_dir

        self._results: dict[int, dict[str, Any]] = {}
        self._results_lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None

        # Импорты ядра ленивые: bridge должен подняться и без установленного
        # nicotine-plus (тогда падаем с понятной ошибкой).
        try:
            from pynicotine.config import config
            from pynicotine.core import core
            from pynicotine.events import events
            from pynicotine.logfacility import log
            from pynicotine.users import UserStatus
        except ImportError as exc:
            raise BridgeError(
                "Пакет nicotine-plus не установлен в venv "
                "(pip install --no-deps nicotine-plus)"
            ) from exc

        self._config = config
        self._core = core
        self._events = events
        self._log = log
        self._user_status = UserStatus

    # --- запуск ------------------------------------------------------------

    def run(self) -> int:
        from pynicotine.slskmessages import FileListMessage

        self._file_list_message = FileListMessage

        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.download_dir, exist_ok=True)

        # Каталоги ядра: конфиг, кэши, INCOMPLETE-файлы -- всё в data_dir
        self._config.set_config_file(os.path.join(self.data_dir, "config"))
        self._config.set_data_folder(self.data_dir)

        self._log.add_log_level("search", is_permanent=False)

        self._core.init_components(enabled_components=ENABLED_COMPONENTS, isolated_mode=True)

        # Учётная запись и каталог загрузок
        self._config.sections["server"]["login"] = self.username
        self._config.sections["server"]["passw"] = self.password
        self._config.sections["server"]["auto_connect_startup"] = True
        self._config.sections["transfers"]["downloaddir"] = self.download_dir
        self._config.sections["transfers"]["usernamesubfolders"] = False

        # Свои обработчики событий ядра
        self._events.connect("log-message", self._on_log_message)
        self._events.connect("add-search", self._on_add_search)
        self._events.connect("file-search-response", self._on_search_response)

        # HTTP-сервер в отдельном потоке; вызовы ядра маришалим в main thread
        self._httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        self._httpd.bridge = self
        threading.Thread(target=self._httpd.serve_forever, name="http-bridge", daemon=True).start()
        self._log.add(f"Orpheus bridge listening on {self.host}:{self.port}")

        self._core.start()
        if self._config.sections["server"]["auto_connect_startup"]:
            self._core.connect()

        # Главный цикл ядра: обработка событий 10 раз в секунду
        while self._events.process_thread_events():
            time.sleep(0.1)

        return 0

    # --- события ядра ------------------------------------------------------

    def _on_log_message(self, timestamp_format: str, msg: str, _title: str, _level: str):
        if timestamp_format:
            line = f"[{time.strftime(timestamp_format)}] {msg}"
        else:
            line = msg
        try:
            print(line, flush=True)
        except OSError:
            pass

    def _on_add_search(self, token: int, search: Any, _switch_page: bool):
        with self._results_lock:
            self._results[token] = {
                "term": search.term,
                "started": time.time(),
                "items": {},
            }

    def _on_search_response(self, msg: Any):
        """Peer-ответ с результатами поиска (msg.list: tuple-ы файлов)."""
        token = msg.token
        with self._results_lock:
            state = self._results.get(token)
            if state is None:
                return
            items = state["items"]
            for _code, file_path, size, _ext, attributes, *_unused in msg.list:
                if len(items) >= SEARCH_MAX_ITEMS:
                    return
                bitrate, length, *_rest = self._file_list_message.parse_audio_quality_length(
                    size, attributes
                )
                items[(msg.username, file_path)] = {
                    "username": msg.username,
                    "path": file_path,
                    "size": int(size or 0),
                    "bitrate": max(int(bitrate or 0), 0),
                    "length": max(int(length or 0), 0),
                }

    # --- вызовы ядра из HTTP-потока ---------------------------------------

    def _call_main(self, func, *args, timeout_s: float = 30.0) -> Any:
        """Выполнить func в main thread ядра (как CLI Nicotine+)."""
        event = threading.Event()
        box: dict[str, Any] = {}

        def wrapper():
            try:
                box["result"] = func(*args)
            except Exception as exc:  # noqa: BLE001 - наружу уходит через box
                box["error"] = exc
            finally:
                event.set()

        self._events.invoke_main_thread(wrapper)
        if not event.wait(timeout_s):
            raise BridgeError("Таймаут вызова main thread ядра")
        if "error" in box:
            raise BridgeError(f"Ошибка ядра: {box['error']}")
        return box.get("result")

    # --- HTTP API ----------------------------------------------------------

    def api_ping(self) -> dict[str, Any]:
        def _login_status():
            return self._core.users.login_status

        status = self._call_main(_login_status)
        connected = status != self._user_status.OFFLINE
        return {"ok": True, "connected": connected, "username": self.username}

    def api_search(self, query: str) -> dict[str, Any]:
        query = (query or "").strip()
        if not query:
            raise BridgeError("Пустой поисковый запрос")

        def _do_search():
            before = set(self._core.search.searches)
            self._core.search.do_search(query, "global")
            token = max(set(self._core.search.searches) - before, default=None)
            self._cleanup_searches()
            return token

        token = self._call_main(_do_search)
        if token is None:
            raise BridgeError("Не удалось создать поиск")
        return {"token": token}

    def api_results(self, token: int) -> dict[str, Any]:
        def _cleanup():
            self._cleanup_searches()

        self._call_main(_cleanup)
        with self._results_lock:
            state = self._results.get(token)
            if state is None:
                return {"term": "", "results": []}
            return {
                "term": state["term"],
                "results": list(state["items"].values()),
            }

    def api_download(self, username: str, path: str, size: int) -> dict[str, Any]:
        if not username or not path:
            raise BridgeError("download: нужны username и path")

        def _enqueue():
            folder = os.path.join(self.download_dir, uuid.uuid4().hex[:12])
            os.makedirs(folder, exist_ok=True)
            self._core.downloads.enqueue_download(
                username, path, folder_path=folder, size=int(size or 0)
            )
            return folder

        self._call_main(_enqueue)
        return {"id": self._quote(username + path)}

    def api_transfer(self, transfer_id: str) -> dict[str, Any]:
        key = self._unquote(transfer_id)

        def _status():
            from pynicotine.transfers import TransferStatus

            transfer = self._core.downloads.transfers.get(key)
            if transfer is None:
                return {"status": "NOT_FOUND"}
            data: dict[str, Any] = {
                "status": str(transfer.status or TransferStatus.QUEUED),
                "size": transfer.size,
                "path": None,
            }
            if transfer.status == TransferStatus.FINISHED:
                basename = self._core.downloads.get_download_basename(
                    transfer.virtual_path, transfer.folder_path
                )
                data["path"] = os.path.join(transfer.folder_path, basename)
            return data

        return self._call_main(_status)

    # --- внутреннее --------------------------------------------------------

    def _cleanup_searches(self):
        """Удаляем поиски, которые Orpheus больше не опрашивает."""
        now = time.time()
        with self._results_lock:
            stale = [t for t, s in self._results.items() if now - s["started"] > SEARCH_TTL_S]
            for token in stale:
                del self._results[token]
                try:
                    self._core.search.remove_search(token)
                except Exception:
                    pass

    @staticmethod
    def _quote(value: str) -> str:
        from urllib.parse import quote

        return quote(value, safe="")

    @staticmethod
    def _unquote(value: str) -> str:
        from urllib.parse import unquote

        return unquote(value)


class _Handler(BaseHTTPRequestHandler):
    """HTTP API моста. Ссылка на Bridge -- в server.bridge."""

    def log_message(self, *args):  # тишина в логах: всё уже пишет _on_log_message
        pass

    def _bridge(self) -> Bridge:
        return self.server.bridge  # type: ignore[attr-defined]

    def _send(self, data: Any, code: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, exc: Exception, code: int = 500):
        self._send({"error": str(exc)}, code)

    def _read_body(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length", 0) or 0)
        if not size:
            return {}
        try:
            return json.loads(self.rfile.read(size).decode())
        except (ValueError, UnicodeDecodeError) as exc:
            raise BridgeError("Некорректный JSON") from exc

    def _get_param(self, name: str) -> str:
        query = parse_qs(urlparse(self.path).query)
        values = query.get(name)
        if not values:
            raise BridgeError(f"Нет параметра {name}")
        return values[0]

    # --- маршруты ----------------------------------------------------------

    def do_GET(self):
        path = urlparse(self.path).path
        bridge = self._bridge()
        try:
            if path == "/ping":
                self._send(bridge.api_ping())
            elif path == "/results":
                token = int(self._get_param("token"))
                self._send(bridge.api_results(token))
            elif path == "/transfer":
                self._send(bridge.api_transfer(self._get_param("id")))
            else:
                self._send_error(BridgeError(f"Неизвестный путь {path}"), 404)
        except ValueError as exc:
            self._send_error(exc, 400)
        except Exception as exc:  # noqa: BLE001
            self._send_error(exc, 500)

    def do_POST(self):
        path = urlparse(self.path).path
        bridge = self._bridge()
        try:
            body = self._read_body()
            if path == "/search":
                self._send(bridge.api_search(body.get("query", "")))
            elif path == "/download":
                self._send(
                    bridge.api_download(
                        body.get("username", ""),
                        body.get("path", ""),
                        int(body.get("size", 0) or 0),
                    )
                )
            else:
                self._send_error(BridgeError(f"Неизвестный путь {path}"), 404)
        except ValueError as exc:
            self._send_error(exc, 400)
        except Exception as exc:  # noqa: BLE001
            self._send_error(exc, 500)


def main(argv: list[str] | None = None) -> int:
    """Точка входа: python -m orpheus.nicotine.bridge [--host H] [--port P]..."""
    import argparse

    parser = argparse.ArgumentParser(description="Мост к headless Nicotine+ (Soulseek)")
    parser.add_argument("--host", default=_env("ORPHEUS_NICOTINE_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(_env("ORPHEUS_NICOTINE_PORT", str(DEFAULT_PORT))))
    parser.add_argument("--data-dir", default=_env("ORPHEUS_NICOTINE_DATA_DIR"))
    parser.add_argument("--download-dir", default=_env("ORPHEUS_NICOTINE_DOWNLOAD_DIR"))
    parser.add_argument("--username", default=_env("ORPHEUS_NICOTINE_USERNAME"))
    parser.add_argument("--password", default=_env("ORPHEUS_NICOTINE_PASSWORD"))
    args = parser.parse_args(argv)

    root = os.getcwd()
    bridge = Bridge(
        host=args.host,
        port=args.port,
        username=args.username,
        password=args.password,
        data_dir=args.data_dir or os.path.join(root, "data", "nicotine"),
        download_dir=args.download_dir or os.path.join(root, "data", "tmp", "nicotine"),
    )
    try:
        return bridge.run()
    except BridgeError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
