"""Клиент Deezer: публичный поиск (api.deezer.com) + полные файлы через gw-light.

Premium/HiFi-доступ даёт ARL-кука (сессионный токен из браузера после входа
на deezer.com, 192 символа): data/cache/deezer_arl.txt. Скачивание MP3 320
(или FLAC на HiFi) идёт через внутренний шлюз www.deezer.com/ajax/gw-light.php
— тот же механизм, что у deemix/d-fi. ARL живёт ~90-120 дней, протухает при
смене пароля или выходе из аккаунта.

Публичный API (поиск, метаданные, обложки) работает без авторизации и
без VPN; полные файлы — только с валидным ARL Premium/HiFi.

Рейт-лимиты: пауза между публичными запросами request_interval (по умолчанию
0.25 c — лимит api.deezer.com ~50 req/5s), между стримами stream_interval
(0.8 c) — чтобы не триггерить анти-бан на массовых загрузках.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .sources.base import SourceError

PUBLIC_BASE = "https://api.deezer.com"
GW_LIGHT = "https://www.deezer.com/ajax/gw-light.php"
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# Ранг формата стрима: качество предпочтения (выше — лучше)
FORMAT_RANK = {
    "FLAC": 9,
    "FLAC_1411": 9,
    "MP3_320": 3,
    "MP3_256": 2,
    "MP3_128": 1,
    "AAC_320": 4,
    "AAC_128": 2,
}
FORMAT_EXT = {
    "FLAC": ".flac",
    "FLAC_1411": ".flac",
    "AAC_320": ".m4a",
    "AAC_128": ".m4a",
}


class DeezerClient:
    """HTTP-слой над Deezer: публичный API + gw-light стримы по ARL."""

    def __init__(
        self,
        arl: str = "",
        arl_file: Path | str | None = None,
        timeout_s: int = 20,
        request_interval: float = 0.25,
        stream_interval: float = 0.8,
        public_base: str = PUBLIC_BASE,
        gw_base: str = GW_LIGHT,
    ):
        self.public_base = public_base.rstrip("/")
        self.gw_base = gw_base.rstrip("/")
        self._arl = arl.strip()
        self.arl_file = Path(arl_file) if arl_file else None
        self.timeout_s = timeout_s
        self.request_interval = request_interval
        self.stream_interval = stream_interval
        self._api_token: str | None = None
        self._user_data: dict | None = None
        self._last_request = 0.0
        self._last_stream = 0.0

    # --- токен ------------------------------------------------------------

    @property
    def arl(self) -> str:
        """ARL: аргумент > DEEZER_ARL (окружение) > файл в data/cache."""
        if self._arl:
            return self._arl
        env = os.getenv("DEEZER_ARL", "").strip()
        if env:
            return env
        if self.arl_file and self.arl_file.exists():
            return self.arl_file.read_text(encoding="utf-8").strip()
        return ""

    def _throttle(self) -> None:
        delay = self.request_interval - (time.monotonic() - self._last_request)
        if delay > 0:
            time.sleep(delay)
        self._last_request = time.monotonic()

    def _throttle_stream(self) -> None:
        delay = self.stream_interval - (time.monotonic() - self._last_stream)
        if delay > 0:
            time.sleep(delay)
        self._last_stream = time.monotonic()

    def _urlopen(self, req: urllib.request.Request, timeout: int):
        """Точка входа для тестов."""
        return urllib.request.urlopen(req, timeout=timeout)

    def _http_get(self, url: str, headers: dict | None = None) -> bytes:
        req = urllib.request.Request(url, headers=headers or {"User-Agent": BROWSER_UA})
        delay = 5
        for attempt in range(4):
            self._throttle()
            try:
                with self._urlopen(req, self.timeout_s) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    time.sleep(delay)
                    delay *= 3
                    continue
                raise SourceError(f"deezer: HTTP {exc.code} {url}") from exc
            except urllib.error.URLError as exc:
                raise SourceError(f"deezer: {exc.reason} ({url})") from exc
        raise SourceError(f"deezer: HTTP 429 {url} (исчерпаны повторы)")

    # --- публичный API ----------------------------------------------------

    def pub(self, path: str, params: dict | None = None) -> dict:
        url = self.public_base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.loads(self._http_get(url).decode("utf-8"))
        if data.get("error"):
            err = data["error"]
            code = err.get("code") if isinstance(err, dict) else ""
            raise SourceError(f"deezer: API error {code} {err} ({path})")
        return data

    def search(self, text: str) -> list[dict]:
        return self.pub("/search", {"q": text}).get("data") or []

    def search_albums(self, text: str) -> list[dict]:
        return self.pub("/search/album", {"q": text}).get("data") or []

    def album(self, album_id: str) -> dict:
        return self.pub(f"/album/{album_id}")

    # --- gw-light (требует ARL) -------------------------------------------

    def _gw(self, method: str, args: dict, retry_token: bool = True) -> dict:
        """Вызов внутреннего шлюза; body — JSON-массив аргументов (input=3)."""
        token = self._api_token or ""
        cid = str(random.randint(100_000_000, 999_999_999))
        url = (
            f"{self.gw_base}?api_version=1.0&api_token={token}"
            f"&input=3&method={method}&cid={cid}"
        )
        body = json.dumps([args]).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "User-Agent": BROWSER_UA,
                "Content-Type": "application/json",
                "Cookie": f"arl={self.arl}",
            },
        )
        try:
            with self._urlopen(req, self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise SourceError(
                    "deezer: ARL недействителен или аккаунт без Premium — "
                    "обновите data/cache/deezer_arl.txt (достаньте ARL-куку заново)"
                ) from exc
            raise SourceError(f"deezer: gw HTTP {exc.code} ({method})") from exc
        except urllib.error.URLError as exc:
            raise SourceError(f"deezer: gw {exc.reason} ({method})") from exc

        if data.get("error"):
            err = data["error"]
            code = err[0] if isinstance(err, list) and err else str(err)
            if retry_token and code in ("API_TOKEN_INVALID", "INVALID_SESSION"):
                self._api_token = None
                self.get_user_data()
                return self._gw(method, args, retry_token=False)
            raise SourceError(f"deezer: gw error {code} ({method})")
        results = data.get("results")
        if isinstance(results, dict) and results.get("ERROR"):
            err = results["ERROR"]
            code = err[0] if isinstance(err, list) and err else str(err)
            if retry_token and code in ("API_TOKEN_INVALID", "INVALID_SESSION"):
                self._api_token = None
                self.get_user_data()
                return self._gw(method, args, retry_token=False)
            raise SourceError(f"deezer: gw error {code} ({method})")
        if not isinstance(results, (dict, list)):
            raise SourceError(f"deezer: gw неожиданный ответ ({method})")
        return results

    def get_user_data(self) -> dict:
        """Профиль + api_token для последующих вызовов. Проверка ARL/подписки."""
        if not self.arl:
            raise SourceError("deezer: нет ARL-токена (data/cache/deezer_arl.txt)")
        results = self._gw("deezer.getUserData", {}, retry_token=False)
        token = results.get("api_token") or ""
        self._api_token = token
        return results

    def account_info(self) -> dict:
        """Краткая сводка аккаунта для диагностики: имя, id, подписка."""
        if self._user_data is None:
            self._user_data = self.get_user_data()
        user = self._user_data.get("USER") or {}
        info = {
            "user_id": user.get("USER_ID"),
            "login": user.get("LOGIN"),
            "firstname": user.get("FIRSTNAME"),
            "country": user.get("COUNTRY"),
        }
        options = user.get("OPTIONS") or {}
        for key in options:
            if "-" in key:
                info.setdefault("options", {})[key] = options[key]
                break
        return info

    def track_media(self, sng_id: int) -> dict | None:
        """Лучший доступный стрим трека (media-запись с href/format) или None."""
        results = self._gw("track.getData", {"sng_id": int(sng_id)})
        media = results.get("media") or []
        if not media:
            return None
        ranked = []
        for entry in media:
            fmt = (entry.get("format") or "").upper()
            rank = FORMAT_RANK.get(fmt)
            if rank is None:
                try:
                    rank = min(int(entry.get("quality") or 0), 9)
                except (TypeError, ValueError):
                    rank = 0
            if not rank:
                continue
            href = entry.get("href") or ""
            if not href:
                continue
            ranked.append((rank, entry.get("media_order") or 0, entry))
        if not ranked:
            return None
        ranked.sort(key=lambda item: (item[0], item[1]))
        best = ranked[-1][2]
        best["format"] = (best.get("format") or "").upper()
        return best

    def stream_url(self, entry: dict) -> str:
        """CDN-ссылка на файл; при наличии license_token дописываем его."""
        href = entry.get("href") or ""
        if not href.startswith(("http://", "https://")):
            raise SourceError(f"deezer: странный CDN-URL: {href!r}")
        token = entry.get("license_token")
        if token:
            sep = "&" if "?" in href else "?"
            href = f"{href}{sep}license_token={urllib.parse.quote(token)}"
        return href

    def media_extension(self, entry: dict) -> str:
        fmt = (entry.get("format") or "").upper()
        return FORMAT_EXT.get(fmt, ".mp3")

    def download(self, url: str, dest: Path) -> Path:
        """Скачать файл по CDN-ссылке (с интервалом — анти-бан)."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._throttle_stream()
        try:
            with self._urlopen(
                urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": BROWSER_UA,
                        "Cookie": f"arl={self.arl}",
                    },
                ),
                self.timeout_s,
            ) as resp, open(dest, "wb") as out:
                shutil.copyfileobj(resp, out)
        except urllib.error.URLError as exc:
            raise SourceError(f"deezer: загрузка: {exc.reason}") from exc
        return dest