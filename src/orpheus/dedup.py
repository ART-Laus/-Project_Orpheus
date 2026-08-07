"""Дедупликация: анализ, решения, применение.

Три уровня дублей:
1. Точные — одинаковый ISRC (одна запись в разных релизах: сингл/альбом/сборник)
2. Кандидаты — имя трека + первый исполнитель совпадают, ISRC разный
   (ремастеры, каверы, альтернативные версии) — только ручное решение
3. Псевдо-дубли — один ID в нескольких плейлистах: это не дубли, по философии
   проекта один трек может входить в любое количество плейлистов.

Эвристика канона (без album_type — его нет в выгрузке Exportify):
  1. решение пользователя (data/decisions.json);
  2. членство в «Liked Songs»;
  3. не-сингл (имя альбома != имени трека);
  4. более ранняя дата релиза;
  5. больше плейлистов;
  6. стабильный порядок (ID).

Применение (apply):
  - перелинковка ссылок плейлистов на канонический ID;
  - жёсткое удаление неканонических записей из tracks.json;
  - снапшот удалённого в data/backups/dedup-<timestamp>.json;
  - сборка мусора: альбомы без треков, исполнители без ссылок.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .models import Track
from .store import Store

REPORTS_DIR_NAME = "reports"
BACKUPS_DIR_NAME = "backups"
DECISIONS_FILE = "decisions.json"
DECISIONS_PATH = "data/decisions.json"

_SQUASH_RE = re.compile(r"[\W_]+", re.UNICODE)


def normalize_key(text: str) -> str:
    """Нормализация имени для сравнения: регистр и пунктуация, буквы всех
    алфавитов (латиница, кириллица, CJK) сохраняются."""
    text = unicodedata.normalize("NFKC", text.lower())
    return _SQUASH_RE.sub("", text)


def group_key(track: dict) -> tuple[str, str]:
    name = normalize_key(track.get("name", ""))
    artist = normalize_key((track.get("artist_names") or [""])[0])
    return name, artist


def _release_date(track: dict, albums: dict[str, dict]) -> str:
    album = albums.get(track.get("album_id", ""), {})
    return album.get("release_date", "") or ""


def _album_name(track: dict, albums: dict[str, dict]) -> str:
    album = albums.get(track.get("album_id", ""), {})
    return album.get("name", "") or ""


class DedupAnalyzer:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store

    # --- группировка ------------------------------------------------------

    def isrc_groups(self) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = {}
        for tid, track in self.store.tracks.items():
            isrc = track.get("isrc")
            if isrc:
                groups.setdefault(isrc, []).append(tid)
        return {k: v for k, v in groups.items() if len(v) > 1}

    def name_groups(self) -> dict[tuple[str, str], list[str]]:
        groups: dict[tuple[str, str], list[str]] = {}
        for tid, track in self.store.tracks.items():
            key = group_key(track)
            groups.setdefault(key, []).append(tid)
        return {
            k: v
            for k, v in groups.items()
            if len(v) > 1 and len({t.get("isrc") for t in (self.store.tracks[i] for i in v)}) > 1
        }

    # --- канон ------------------------------------------------------------

    def suggest_canonical(self, member_ids: list[str]) -> str:
        tracks = self.store.tracks
        albums = self.store.albums

        def score(tid: str) -> tuple:
            t = tracks[tid]
            liked = 1 if t.get("liked") else 0
            not_single = 0 if _album_name(t, albums).strip().lower() == t.get("name", "").strip().lower() else 1
            date = _release_date(t, albums)
            playlists = len(t.get("playlists", []))
            return (liked, not_single, date, playlists, tid)

        return max(member_ids, key=score)

    # --- отчёт -------------------------------------------------------------

    def analyze(self) -> dict[str, Any]:
        playlist_names = {
            pl_id: pl.get("name", pl_id)
            for pl_id, pl in self.store.playlists.items()
        }
        liked_playlist_id = ""
        for pl_id, pl in self.store.playlists.items():
            if pl.get("name") == "Liked Songs":
                liked_playlist_id = pl_id
                break

        isrc_groups = self.isrc_groups()
        name_groups = self.name_groups()

        def describe(member_ids: list[str]) -> list[dict]:
            rows = []
            for tid in member_ids:
                t = self.store.tracks[tid]
                rows.append(
                    {
                        "id": tid,
                        "name": t.get("name", ""),
                        "album": _album_name(t, self.store.albums),
                        "release_date": _release_date(t, self.store.albums),
                        "duration_ms": t.get("duration_ms", 0),
                        "playlists": [
                            playlist_names.get(pid, pid)
                            for pid in t.get("playlists", [])
                        ],
                        "in_liked": bool(t.get("liked")),
                        "isrc": t.get("isrc"),
                    }
                )
            return rows

        exact = []
        for isrc, member_ids in sorted(isrc_groups.items()):
            canonical = self.suggest_canonical(member_ids)
            exact.append(
                {
                    "kind": "exact",
                    "key": isrc,
                    "group_name": self.store.tracks[member_ids[0]].get("name", ""),
                    "canonical": canonical,
                    "members": describe(member_ids),
                }
            )

        candidates = []
        for (name_key, artist_key), member_ids in sorted(
            name_groups.items(), key=lambda item: item[0][1]
        ):
            canonical = self.suggest_canonical(member_ids)
            candidates.append(
                {
                    "kind": "candidate",
                    "key": f"{name_key}|{artist_key}",
                    "group_name": self.store.tracks[member_ids[0]].get("name", ""),
                    "canonical": canonical,
                    "members": describe(member_ids),
                }
            )

        # псевдо-дубли: один трек в нескольких плейлистах
        cross = [
            (tid, t.get("playlists", []))
            for tid, t in self.store.tracks.items()
            if len(t.get("playlists", [])) > 1
        ]

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "liked_playlist_id": liked_playlist_id,
            "exact_groups": exact,
            "candidate_groups": candidates,
            "cross_playlist_count": len(cross),
        }

    def write_report(self, analysis: dict[str, Any]) -> Path:
        reports_dir = self.cfg.data_dir / REPORTS_DIR_NAME
        reports_dir.mkdir(parents=True, exist_ok=True)
        md_path = reports_dir / "duplicates.md"
        json_path = reports_dir / "duplicates.json"

        json_path.write_text(
            json.dumps(analysis, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        md_path.write_text(self._render_md(analysis), encoding="utf-8")
        return md_path

    def _render_md(self, analysis: dict[str, Any]) -> str:
        lines = [
            "# Дубликаты",
            f"Сформировано: {analysis['generated_at']}",
            "",
            f"Точных групп (ISRC): **{len(analysis['exact_groups'])}**",
            f"Кандидатов по имени: **{len(analysis['candidate_groups'])}**",
            f"Один трек в нескольких плейлистах (не дубли): {analysis['cross_playlist_count']}",
            "",
            "Согласие с автоматическим каноном не требуется; для ручных решений",
            "впишите записи в data/decisions.json, затем выполните `orpheus dedup apply`.",
            "Формат decisions.json:",
            '  { "<ISRC>": {"canonical": "<track_id>", "reason": "..."} }',
            '  { "имя|исполнитель": {"skip": true, "reason": "..."} }',
            "",
        ]
        for section, title in (
            (analysis["exact_groups"], "Точные дубли (одинаковый ISRC)"),
            (analysis["candidate_groups"], "Кандидаты (имя совпадает, ISRC разный)"),
        ):
            lines.append(f"## {title}: {len(section)}")
            lines.append("")
            for group in section:
                canonical = group["canonical"]
                lines.append(
                    f"### [{group['kind']}] {group['group_name']} "
                    f"(ключ: {group['key']}, {len(group['members'])} шт.)"
                )
                lines.append("")
                for m in group["members"]:
                    mark = "**КАНОН** " if m["id"] == canonical else ""
                    liked = "лайкнут" if m["in_liked"] else "—"
                    pls = ", ".join(m["playlists"]) or "—"
                    lines.append(
                        f"- {mark}`{m['id']}` — «{m['name']}» — "
                        f"альбом «{m['album']}» ({m['release_date']}) — "
                        f"{liked} — плейлисты: {pls}"
                    )
                lines.append("")
        return "\n".join(lines)


class DedupApplier:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store

    def load_decisions(self) -> dict:
        path = self.cfg.root / DECISIONS_FILE
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def apply(self) -> dict[str, Any]:
        analyzer = DedupAnalyzer(self.cfg, self.store)
        analysis = analyzer.analyze()
        decisions = self.load_decisions()

        removed: list[dict] = []
        remap: dict[str, str] = {}
        stats = {"removed_tracks": 0, "remapped_refs": 0, "merged_groups": 0}

        for group in analysis["exact_groups"]:
            self._resolve_group(group, decisions, remap, removed, stats)
        for group in analysis["candidate_groups"]:
            decision = decisions.get(group["key"])
            if decision and not decision.get("skip"):
                self._resolve_group(group, decisions, remap, removed, stats)

        if removed:
            for pl in self.store.playlists.values():
                old = pl.get("tracks", [])
                new: list[str] = []
                changed = False
                for tid in old:
                    target = remap.get(tid, tid)
                    if target != tid:
                        changed = True
                    if target not in new:
                        new.append(target)
                if changed:
                    pl["tracks"] = new
                    stats["remapped_refs"] += sum(
                        1 for a, b in zip(old, pl["tracks"]) if a != b
                    )

            for rec in removed:
                del self.store.tracks[rec["track"]["spotify_id"]]

            self._cleanup_albums()
            self._cleanup_artists()
            self._write_backup(removed)
            self.store.save_all()

        stats["removed_tracks"] = len(removed)
        return stats

    def _resolve_group(
        self,
        group: dict,
        decisions: dict,
        remap: dict[str, str],
        removed: list[dict],
        stats: dict,
    ) -> None:
        decision = decisions.get(group["key"])
        if decision and not decision.get("skip"):
            canonical = decision.get("canonical")
            if canonical not in {m["id"] for m in group["members"]}:
                canonical = None
        else:
            canonical = group["canonical"] if decision is None else None
        if not canonical:
            return

        stats["merged_groups"] += 1
        for m in group["members"]:
            if m["id"] != canonical:
                remap[m["id"]] = canonical
                removed.append(
                    {
                        "group_key": group["key"],
                        "canonical": canonical,
                        "track": self.store.tracks[m["id"]],
                    }
                )

    def _cleanup_albums(self) -> None:
        for aid, album in list(self.store.albums.items()):
            album["track_ids"] = [t for t in album.get("track_ids", []) if t in self.store.tracks]
            if not album["track_ids"]:
                del self.store.albums[aid]

    def _cleanup_artists(self) -> None:
        referenced = set()
        for track in self.store.tracks.values():
            referenced.update(track.get("artist_ids", []))
        for album in self.store.albums.values():
            referenced.update(album.get("artist_ids", []))
        for aid in list(self.store.artists):
            if aid not in referenced:
                del self.store.artists[aid]

    def _write_backup(self, removed: list[dict]) -> None:
        backups_dir = self.cfg.data_dir / BACKUPS_DIR_NAME
        backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = backups_dir / f"dedup-{stamp}.json"
        path.write_text(
            json.dumps(
                {"removed": removed, "created_at": stamp}, ensure_ascii=False, indent=1
            ),
            encoding="utf-8",
        )
