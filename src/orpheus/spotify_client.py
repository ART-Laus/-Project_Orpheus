"""Обёртка над spotipy: авторизация, пагинация, ретраи при rate limit."""

from __future__ import annotations

import time
from typing import Any, Callable

import spotipy
from spotipy.oauth2 import SpotifyOAuth

from .config import Config

RETRYABLE_STATUSES = (429, 500, 502, 503, 504)
MAX_ATTEMPTS = 10


class SpotifyClient:
    def __init__(self, cfg: Config):
        auth = SpotifyOAuth(
            client_id=cfg.client_id,
            client_secret=cfg.client_secret,
            redirect_uri=cfg.redirect_uri,
            scope=cfg.scope,
            cache_path=cfg.cache_path,
            open_browser=False,
        )
        self.sp = spotipy.Spotify(auth_manager=auth, requests_timeout=60)
        self.retry_base_s = cfg.retry_base_s

    # --- базовый вызов с ретраем ------------------------------------------

    def _call(self, method: Callable[[], Any]) -> Any:
        last_exc: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return method()
            except spotipy.SpotifyException as exc:
                last_exc = exc
                if exc.http_status not in RETRYABLE_STATUSES:
                    raise
                wait = self._wait_for(exc, attempt)
            except (spotipy.SpotifyOauthError, OSError) as exc:
                last_exc = exc
                wait = self.retry_base_s * attempt
            time.sleep(wait)
        raise RuntimeError("Не удалось выполнить запрос после повторов") from last_exc

    def _wait_for(self, exc: spotipy.SpotifyException, attempt: int) -> float:
        headers = getattr(exc, "headers", None) or {}
        retry_after = headers.get("Retry-After")
        if retry_after:
            try:
                return max(float(retry_after), 0.5)
            except ValueError:
                pass
        return self.retry_base_s * attempt

    # --- пагинация ---------------------------------------------------------

    def iter_saved_tracks(self) -> Any:
        """Итератор по лайкнутым трекам: yield (offset, page)."""
        offset = 0
        while True:
            page = self._call(
                lambda o=offset: self.sp.current_user_saved_tracks(limit=50, offset=o)
            )
            yield offset, page
            if not page.get("items"):
                return
            offset += len(page["items"])

    def iter_playlists(self) -> Any:
        """Итератор по плейлистам пользователя: yield (offset, page)."""
        offset = 0
        while True:
            page = self._call(
                lambda o=offset: self.sp.current_user_playlists(limit=50, offset=o)
            )
            yield offset, page
            if not page.get("items"):
                return
            offset += len(page["items"])

    def iter_playlist_items(self, playlist_id: str) -> Any:
        """Итератор по трекам плейлиста: yield (offset, page)."""
        offset = 0
        while True:
            page = self._call(
                lambda p=playlist_id, o=offset: self.sp.playlist_items(
                    p, limit=50, offset=o, additional_types=("track",)
                )
            )
            yield offset, page
            if not page.get("items"):
                return
            offset += len(page["items"])

    def get_user(self) -> dict:
        return self._call(lambda: self.sp.current_user())

    def get_artists(self, artist_ids: list[str]) -> list[dict]:
        return self._call(lambda: self.sp.artists(artist_ids))["artists"]
