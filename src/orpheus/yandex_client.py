"""Клиент Яндекс.Музыки: аккаунт, поиск, download-info, прямая загрузка.

Токен OAuth берётся из (в порядке приоритета): аргумента token,
переменной окружения YANDEX_TOKEN, файла token_file
(data/cache/yandex_token.txt). Токен получается один раз через
`orpheus yandex login` (OAuth-страница Яндекс.Паспорта).

Для 320 kbps нужен Яндекс Плюс: проверяется в account_status
(subscription.plus.HasPlus), а фактическая доступность качества —
через /tracks/{id}/download-info (выбираем максимальный битрейт).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from .sources.base import SourceError

API_BASE = "https://api.music.yandex.net"
# Стандартный заголовок клиента, под который API отдаёт прямые ссылки
CLIENT_HEADER = "YandexMusicAndroid/6.03.4"
YANDEX_OAUTH_CLIENT_ID = "23cabbbdc6cd418abb4b39c32c41195d"
AUTHORIZE_URL = (
    "https://oauth.yandex.ru/authorize?response_type=token"
    f"&client_id={YANDEX_OAUTH_CLIENT_ID}"
)
# Соль для подписи прямого URL (та же, что у официальных клиентов)
SIGN_SALT = "XGRlBW9FXlekgbPrRHuSiA"


class YandexClient:
    """Тонкий HTTP-слой над api.music.yandex.net."""

    def __init__(
        self,
        base_url: str = API_BASE,
        token: str = "",
        token_file: Path | str | None = None,
        timeout_s: int = 20,
    ):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.token_file = Path(token_file) if token_file else None
        self.timeout_s = timeout_s

    @property
    def token(self) -> str:
        """Токен: аргумент > YANDEX_TOKEN (окружение) > файл в data/cache."""
        if self._token:
            return self._token
        env = os.getenv("YANDEX_TOKEN", "")
        if env:
            return env
        if self.token_file and self.token_file.exists():
            return self.token_file.read_text(encoding="utf-8").strip()
        return ""

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"OAuth {self.token}",
            "X-Yandex-Music-Client": CLIENT_HEADER,
        }

    def _request(self, path: str, params: dict | None = None) -> dict:
        token = self.token
        if not token:
            raise SourceError("yandex: нет токена (orpheus yandex login или YANDEX_TOKEN)")
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise SourceError(
                    "yandex: токен недействителен — повторите orpheus yandex login"
                ) from exc
            raise SourceError(f"yandex: HTTP {exc.code} {path}") from exc
        except urllib.error.URLError as exc:
            raise SourceError(f"yandex: {exc.reason} ({url})") from exc

    # --- API ---------------------------------------------------------------

    def account_status(self) -> dict:
        """Статус аккаунта: account, permissions (high-quality = 320 kbps),
        subscription. API оборачивает ответ в "result"."""
        data = self._request("/account/status")
        return data.get("result", data) if isinstance(data, dict) else {}

    def search(self, text: str, type_: str = "track", page: int = 0) -> list[dict]:
        """Результаты поиска (order: релевантность): type_ = track | album."""
        data = self._request(
            "/search",
            {"text": text, "type": type_, "page": page},
        )
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, dict):
            result = data.get("search-result") or {}
        block = result.get("albums") if type_ == "album" else result.get("tracks")
        return (block or {}).get("results") or []

    def album_with_tracks(self, album_id: str) -> dict:
        """Альбом целиком: title, artists, volumes (список дисков с треками)."""
        data = self._request(f"/albums/{album_id}/with-tracks")
        result = data.get("result", data) if isinstance(data, dict) else {}
        return result if isinstance(result, dict) else {}

    def track_download_info(self, track_id: str) -> list[dict]:
        """Варианты кодека/битрейта для трека (нужен Плюс для высоких)."""
        data = self._request(f"/tracks/{track_id}/download-info")
        result = data.get("result", data) if isinstance(data, dict) else data
        return result if isinstance(result, list) else []

    def best_download_info(self, track_id: str) -> dict | None:
        """Вариант с максимальным битрейтом или None."""
        infos = self.track_download_info(track_id)
        best = None
        for info in infos:
            bitrate = info.get("bitrateInKbps") or 0
            if not bitrate or info.get("preview"):
                continue
            if best is None or bitrate > (best.get("bitrateInKbps") or 0):
                best = info
        return best

    def direct_link(self, info: dict) -> str | None:
        """Прямая ссылка на файл (живёт ~1 минуту).

        Если в download-info есть directLink — берём его; иначе двухшаговый
        флоу: GET downloadInfoUrl -> XML <download-info> (host/path/ts/s),
        подпись md5(SIGN_SALT + path[1:] + s) ->
        https://{host}/get-mp3/{sign}/{ts}{path}
        """
        link = info.get("directLink") or ""
        if not link:
            info_url = info.get("downloadInfoUrl") or ""
            if not info_url:
                return None
            try:
                with urllib.request.urlopen(
                    urllib.request.Request(info_url, headers=self._headers()),
                    timeout=self.timeout_s,
                ) as resp:
                    text = resp.read().decode("utf-8", errors="replace")
            except urllib.error.URLError as exc:
                raise SourceError(f"yandex: download-info: {exc.reason}") from exc
            try:
                meta = json.loads(text)
            except ValueError:
                root = ET.fromstring(text)
                meta = {child.tag: child.text or "" for child in root}
            host = meta.get("host")
            path = meta.get("path")
            ts = meta.get("ts")
            s = meta.get("s")
            if not host or not path or not ts or not s:
                raise SourceError("yandex: download-info: неожиданный ответ")
            sign = hashlib.md5((SIGN_SALT + path[1:] + s).encode("utf-8")).hexdigest()
            link = f"https://{host}/get-mp3/{sign}/{ts}{path}"
        if link.startswith("//"):
            link = "https:" + link
        elif link.startswith("/"):
            link = self.base_url + link
        elif not link.lower().startswith(("http://", "https://")):
            link = "https://" + link
        return link or None

    def download(self, link: str, dest: Path) -> Path:
        """Скачать файл по прямой ссылке; вернуть путь."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(link, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp, open(
                dest, "wb"
            ) as out:
                shutil.copyfileobj(resp, out)
        except urllib.error.URLError as exc:
            raise SourceError(f"yandex: загрузка: {exc.reason}") from exc
        return dest
