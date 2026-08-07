"""Конфигурация проекта: config.yaml + .env."""

from __future__ import annotations

import os
from pathlib import Path


class ConfigError(RuntimeError):
    pass


def _load_dotenv(path: Path) -> None:
    """Минимальный загрузчик .env (KEY=VALUE), не перезаписывает окружение."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


class Config:
    def __init__(self, project_root: Path | str | None = None):
        self.root = Path(project_root) if project_root else Path.cwd()
        _load_dotenv(self.root / ".env")

        yaml_path = self.root / "config.yaml"
        if not yaml_path.exists():
            raise ConfigError(f"config.yaml не найден: {yaml_path}")

        import yaml

        raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}

        spot = raw.get("spotify", {})
        self.redirect_uri = spot.get("redirect_uri", "http://localhost:8888/callback")
        self.scope = spot.get("scope", "user-library-read playlist-read-private")

        paths = raw.get("paths", {})
        self.data_dir = self.root / paths.get("data_dir", "data")
        self.raw_dir = self.data_dir / paths.get("raw_dir", "raw")
        self.db_dir = self.data_dir / paths.get("db_dir", "db")

        imp = raw.get("import", {})
        self.raw_enabled = imp.get("raw_enabled", True)
        self.save_interval_s = imp.get("save_interval_s", 60)
        self.retry_base_s = imp.get("retry_base_s", 2.0)

        slskd = raw.get("slskd", {})
        self.slskd_base_url = slskd.get("base_url", "http://localhost:5030")
        self.slskd_config = self.root / slskd.get("config_path", "tools/slskd/slskd.yml")

        quality = raw.get("quality", {})
        self.min_mp3_bitrate = quality.get("min_mp3_bitrate", slskd.get("min_mp3_bitrate", 320))
        self.min_aac_bitrate = quality.get("min_aac_bitrate", slskd.get("min_aac_bitrate", 256))
        self.duration_tolerance_s = quality.get(
            "duration_tolerance_s", slskd.get("duration_tolerance_s", 5)
        )
        self.verify_tolerance_s = quality.get("verify_tolerance_s", 10)
        self.max_candidates = quality.get("max_candidates", 5)

        self.sources_config = raw.get("sources", [])

        library = raw.get("library", {})
        self.library_dir = self.root / library.get("dir", "Library")
        self.cover_min_size = library.get("cover_min_size", 1000)
        self.cover_preferred_size = library.get("cover_preferred_size", 1400)

        self.cache_path = str(self.root / spot.get("cache_path", ".cache"))

    @property
    def client_id(self) -> str:
        return os.getenv("SPOTIFY_CLIENT_ID", "")

    @property
    def client_secret(self) -> str:
        return os.getenv("SPOTIFY_CLIENT_SECRET", "")

    def validate_credentials(self) -> None:
        if not self.client_id or not self.client_secret:
            raise ConfigError(
                "SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET не заданы. "
                "Скопируйте .env.example в .env и впишите ключи "
                "(https://developer.spotify.com/dashboard)."
            )
