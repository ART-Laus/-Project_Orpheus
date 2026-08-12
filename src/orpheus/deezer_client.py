"""Клиент Deezer: публичный поиск (api.deezer.com) + полные файлы через gw-light.

Premium/HiFi-доступ даёт ARL-кука (сессионный токен из браузера после входа
на deezer.com, 192 символа): data/cache/deezer_arl.txt. Скачивание MP3 320
(или FLAC на HiFi) идёт через внутренний шлюз www.deezer.com/ajax/gw-light.php
— тот же механизм, что у DeezNET/Lidarr. ARL живёт ~90-120 дней, протухает при
смене пароля или выходе из аккаунта.

Схема запросов (актуальна с 2025):
  * getUserData возвращает checkForm (api_token) и SESSION_ID (кука sid);
    валидный CSRF-токен = api_token=checkForm + Cookie: sid=<SESSION_ID>.
  * Метод трека — song.getData с SNG_ID (track.getData удалён).
  * CDN-URL — POST https://media.deezer.com/v1/get_url с license_token из
    USER.OPTIONS и TRACK_TOKEN из song.getData.
  * MP3/FLAC идут зашифрованными Blowfish (BF_CBC_STRIPE): каждый 3-й чанк
    по 2048 байт расшифровывается ключом из md5(sng_id) и секрета
    jo6aem6aQb5lD4fp; нужен pycryptodome.

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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .sources.base import SourceError

PUBLIC_BASE = "https://api.deezer.com"
GW_LIGHT = "https://www.deezer.com/ajax/gw-light.php"
MEDIA_API = "https://media.deezer.com/v1/get_url"
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# Секрет Blowfish-ключей треков (публично известная константа из веб-плеера).
BF_SECRET = "g4el58wc0zvf9na1"
# Размер чанка шифрования CDN-файлов.
BF_CHUNK = 2048

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
        media_api: str = MEDIA_API,
    ):
        self.public_base = public_base.rstrip("/")
        self.gw_base = gw_base.rstrip("/")
        self.media_api = media_api.rstrip("/")
        self._arl = arl.strip()
        self.arl_file = Path(arl_file) if arl_file else None
        self.timeout_s = timeout_s
        self.request_interval = request_interval
        self.stream_interval = stream_interval
        self._api_token: str | None = None
        self._sid: str | None = None
        self._user_data: dict | None = None
        self._license_token: str = ""
        self._last_request = 0.0
        self._last_stream = 0.0
        self._user_lock = threading.Lock()

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

    def _cookie(self) -> str:
        cookie = f"arl={self.arl}"
        if self._sid:
            cookie += f"; sid={self._sid}"
        return cookie

    def _gw(self, method: str, args: dict, retry_token: bool = True) -> dict:
        """Вызов внутреннего шлюза; body — JSON-объект аргументов (input=3)."""
        token = self._api_token or ""
        url = (
            f"{self.gw_base}?api_version=1.0&api_token={token}"
            f"&input=3&method={method}"
        )
        body = json.dumps(args).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "User-Agent": BROWSER_UA,
                "Content-Type": "text/plain;charset=UTF-8",
                "Cookie": self._cookie(),
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
            code = err[0] if isinstance(err, list) and err else (
                next(iter(err)) if isinstance(err, dict) and err else str(err)
            )
            if retry_token and code in (
                "API_TOKEN_INVALID",
                "INVALID_SESSION",
                "VALID_TOKEN_REQUIRED",
            ):
                self._api_token = None
                self._sid = None
                self.get_user_data()
                return self._gw(method, args, retry_token=False)
            raise SourceError(f"deezer: gw error {code} ({method})")
        results = data.get("results")
        if isinstance(results, dict) and results.get("ERROR"):
            err = results["ERROR"]
            code = err[0] if isinstance(err, list) and err else str(err)
            if retry_token and code in (
                "API_TOKEN_INVALID",
                "INVALID_SESSION",
                "VALID_TOKEN_REQUIRED",
            ):
                self._api_token = None
                self.get_user_data()
                return self._gw(method, args, retry_token=False)
            raise SourceError(f"deezer: gw error {code} ({method})")
        if not isinstance(results, dict):
            raise SourceError(f"deezer: gw неожиданный ответ ({method})")
        return results

    def get_user_data(self) -> dict:
        """Профиль + api_token/checkForm и sid для последующих вызовов."""
        with self._user_lock:
            if self._api_token and self._sid and self._user_data is not None:
                return self._user_data
            if not self.arl:
                raise SourceError("deezer: нет ARL-токена (data/cache/deezer_arl.txt)")
            results = self._gw("deezer.getUserData", {}, retry_token=False)
            self._api_token = results.get("checkForm") or ""
            self._sid = results.get("SESSION_ID") or ""
            options = (results.get("USER") or {}).get("OPTIONS") or {}
            self._license_token = options.get("license_token") or ""
            self._user_data = results
            return results

    def account_info(self) -> dict:
        """Краткая сводка аккаунта для диагностики: имя, id, подписка."""
        if self._user_data is None:
            self.get_user_data()
        user = self._user_data.get("USER") or {}
        info = {
            "user_id": user.get("USER_ID"),
            "login": user.get("LOGIN"),
            "firstname": user.get("FIRSTNAME"),
            "country": user.get("COUNTRY"),
            "offer": self._user_data.get("OFFER_NAME"),
        }
        options = user.get("OPTIONS") or {}
        info["formats"] = []
        if options.get("web_lossless"):
            info["formats"].append("FLAC")
        if options.get("web_hq"):
            info["formats"].append("MP3_320")
        if not info["formats"]:
            info["formats"].append("MP3_128")
        return info

    # --- стримы ------------------------------------------------------------

    _TRACK_FIELDS = [
        "TRACK_TOKEN",
        "MD5_ORIGIN",
        "MEDIA_VERSION",
        "FILESIZE",
        "FILESIZE_MP3_128",
        "FILESIZE_MP3_320",
        "FILESIZE_FLAC",
        "PROVIDER_ID",
    ]

    def _track_data(self, sng_id: int) -> dict:
        """Данные трека из gw (TRACK_TOKEN и пр.) — обновляет токен при ошибке."""
        if self._api_token is None:
            self.get_user_data()
        return self._gw(
            "song.getData",
            {"SNG_ID": int(sng_id), "array_default": self._TRACK_FIELDS},
        )

    def track_media(self, sng_id: int, prefer_flac: bool = False) -> dict | None:
        """Запись для скачивания трека или None (нет доступа/не найдено).

        Возвращает dict с полями track_token, format, sng_id, size и пр.,
        которые нужны stream_url/download. Формат: FLAC при prefer_flac и
        web_lossless на аккаунте, иначе MP3_320 (или MP3_128 без HQ).
        """
        try:
            data = self._track_data(int(sng_id))
        except SourceError:
            return None
        token = data.get("TRACK_TOKEN") or ""
        if not token:
            return None
        formats = self.account_info().get("formats") or ["MP3_128"]
        if prefer_flac and "FLAC" in formats:
            fmt = "FLAC"
        elif "MP3_320" in formats:
            fmt = "MP3_320"
        else:
            fmt = "MP3_128"
        entry = {
            "track_token": token,
            "sng_id": int(sng_id),
            "format": fmt,
            "md5_origin": data.get("MD5_ORIGIN") or "",
            "media_version": data.get("MEDIA_VERSION") or "",
            "size": data.get("FILESIZE") or data.get(f"FILESIZE_{fmt}") or 0,
            "provider_id": data.get("PROVIDER_ID") or "",
        }
        return entry

    def stream_url(self, media: dict) -> str:
        """CDN-ссылка на файл через media.deezer.com/v1/get_url."""
        token = (media or {}).get("track_token") or ""
        fmt = ((media or {}).get("format") or "MP3_320").upper()
        if not token:
            raise SourceError("deezer: нет TRACK_TOKEN для трека")
        if not self._license_token:
            self.get_user_data()
        body = {
            "license_token": self._license_token,
            "media": [
                {
                    "type": "FULL",
                    "formats": [{"cipher": "BF_CBC_STRIPE", "format": fmt}],
                }
            ],
            "track_tokens": [token],
        }
        req = urllib.request.Request(
            self.media_api,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "User-Agent": BROWSER_UA,
                "Content-Type": "application/json",
                "Cookie": self._cookie(),
            },
        )
        self._throttle()
        try:
            with self._urlopen(req, self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise SourceError(f"deezer: media HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise SourceError(f"deezer: media {exc.reason}") from exc
        try:
            return data["data"][0]["media"][0]["sources"][0]["url"]
        except (KeyError, IndexError, TypeError) as exc:
            raise SourceError(f"deezer: media без источника: {str(data)[:120]}") from exc

    def media_extension(self, media: dict) -> str:
        fmt = ((media or {}).get("format") or "").upper()
        return FORMAT_EXT.get(fmt, ".mp3")

    @staticmethod
    def _blowfish_key(sng_id: int) -> bytes:
        """Ключ Blowfish: xor байтов hex-md5(sng_id) с секретом плеера."""
        md5_id = __import__("hashlib").md5(str(int(sng_id)).encode()).hexdigest().encode("ascii")
        return bytes(
            [md5_id[i] ^ md5_id[i + 16] ^ ord(BF_SECRET[i]) for i in range(16)]
        )

    def _decrypt_chunk(self, chunk: bytes, key: bytes) -> bytes:
        from Crypto.Cipher import Blowfish

        cipher = Blowfish.new(key, Blowfish.MODE_CBC, b"\x00\x01\x02\x03\x04\x05\x06\x07")
        return cipher.decrypt(chunk)

    def download(self, url: str, dest: Path, sng_id: int | None = None) -> Path:
        """Скачать файл по CDN-ссылке; при sng_id расшифровывает Blowfish.

        Зашифрованы файлы с /media/ или /mobile/ в URL (BF_CBC_STRIPE):
        каждый 3-й чанк по 2048 байт, без ID3-префикса у FLAC.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._throttle_stream()
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
        try:
            resp = self._urlopen(req, self.timeout_s)
        except urllib.error.HTTPError as exc:
            raise SourceError(f"deezer: CDN HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise SourceError(f"deezer: CDN {exc.reason}") from exc

        encrypted = sng_id is not None and (
            "/media/" in url or "/mobile/" in url
        )
        key = self._blowfish_key(sng_id) if encrypted else b""

        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            with open(tmp, "wb") as f:
                if not encrypted:
                    shutil.copyfileobj(resp, f)
                else:
                    # MP3 может начинаться с незашифрованного ID3-тега.
                    head = resp.read(BF_CHUNK)
                    offset = 0
                    if head[:3] == b"ID3":
                        size = int.from_bytes(head[6:10], "big")
                        f.write(head)
                        offset = size - 10
                        if offset > 0:
                            rest = resp.read(offset)
                            head = rest[-BF_CHUNK:] if len(rest) >= BF_CHUNK else rest
                    chunk_index = 0 if head[:3] != b"ID3" else 1
                    pending = head
                    while True:
                        if len(pending) >= BF_CHUNK:
                            part = pending[:BF_CHUNK]
                            pending = pending[BF_CHUNK:]
                            if chunk_index % 3 == 0:
                                part = self._decrypt_chunk(part, key)
                            f.write(part)
                            chunk_index += 1
                        else:
                            data = resp.read(BF_CHUNK - len(pending))
                            if not data:
                                break
                            pending += data
                    if pending:
                        f.write(pending)
        finally:
            resp.close()
        tmp.replace(dest)
        return dest
