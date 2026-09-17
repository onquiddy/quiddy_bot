from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_NAME_RE = re.compile(r"^[^\\/\n\r\t]{1,32}$")


@dataclass(slots=True)
class SavedTrack:
    query: str
    title: str
    author: str
    uri: str | None = None
    source: str = "unknown"
    length_ms: int = 0
    artwork: str | None = None


class PlaylistStore:
    """Small durable per-guild/per-user playlist store.

    The bot remains API-first for platform data. Music playlists are plugin-owned state,
    kept in one atomic JSON file so the feature also works in local/dev deployments.
    The storage boundary is intentionally isolated and can later be swapped for API DB
    persistence without changing command/controller code.
    """

    def __init__(self, path: Path, *, max_playlists: int = 5, max_tracks: int = 100) -> None:
        self.path = path
        self.max_playlists = max(1, max_playlists)
        self.max_tracks = max(1, max_tracks)
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] = {"version": 1, "users": {}}

    async def load(self) -> None:
        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                await self._save_locked()
                return
            try:
                raw = json.loads(await asyncio.to_thread(self.path.read_text, "utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("users", {}), dict):
                    self._data = raw
            except (OSError, json.JSONDecodeError):
                # Preserve the broken file for inspection instead of silently destroying it.
                broken = self.path.with_suffix(self.path.suffix + f".broken-{int(time.time())}")
                try:
                    self.path.replace(broken)
                except OSError:
                    pass
                self._data = {"version": 1, "users": {}}
                await self._save_locked()

    @staticmethod
    def _key(guild_id: int, user_id: int) -> str:
        return f"{guild_id}:{user_id}"

    @staticmethod
    def validate_name(name: str) -> str:
        name = " ".join(name.strip().split())
        if not _NAME_RE.match(name):
            raise ValueError("Playlist name must be 1-32 characters and cannot contain slashes/newlines")
        return name

    def _bucket(self, guild_id: int, user_id: int, *, create: bool = False) -> dict[str, Any]:
        users = self._data.setdefault("users", {})
        key = self._key(guild_id, user_id)
        if create:
            return users.setdefault(key, {"playlists": {}})
        return users.get(key, {"playlists": {}})

    async def list(self, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        async with self._lock:
            playlists = self._bucket(guild_id, user_id).get("playlists", {})
            return [
                {"name": value.get("name", key), "tracks": len(value.get("tracks", [])), "updated_at": value.get("updated_at", 0)}
                for key, value in playlists.items()
            ]

    async def get(self, guild_id: int, user_id: int, name: str) -> dict[str, Any] | None:
        key = self.validate_name(name).casefold()
        async with self._lock:
            value = self._bucket(guild_id, user_id).get("playlists", {}).get(key)
            return json.loads(json.dumps(value)) if value is not None else None

    async def create(self, guild_id: int, user_id: int, name: str) -> dict[str, Any]:
        clean = self.validate_name(name)
        key = clean.casefold()
        async with self._lock:
            playlists = self._bucket(guild_id, user_id, create=True).setdefault("playlists", {})
            if key in playlists:
                raise FileExistsError(clean)
            if len(playlists) >= self.max_playlists:
                raise OverflowError(self.max_playlists)
            now = int(time.time())
            playlists[key] = {"name": clean, "created_at": now, "updated_at": now, "tracks": []}
            await self._save_locked()
            return json.loads(json.dumps(playlists[key]))

    async def delete(self, guild_id: int, user_id: int, name: str) -> bool:
        key = self.validate_name(name).casefold()
        async with self._lock:
            playlists = self._bucket(guild_id, user_id, create=True).setdefault("playlists", {})
            existed = playlists.pop(key, None) is not None
            if existed:
                await self._save_locked()
            return existed

    async def rename(self, guild_id: int, user_id: int, old_name: str, new_name: str) -> None:
        old_key = self.validate_name(old_name).casefold()
        clean = self.validate_name(new_name)
        new_key = clean.casefold()
        async with self._lock:
            playlists = self._bucket(guild_id, user_id, create=True).setdefault("playlists", {})
            if old_key not in playlists:
                raise KeyError(old_name)
            if new_key != old_key and new_key in playlists:
                raise FileExistsError(new_name)
            item = playlists.pop(old_key)
            item["name"] = clean
            item["updated_at"] = int(time.time())
            playlists[new_key] = item
            await self._save_locked()

    async def add_track(self, guild_id: int, user_id: int, name: str, track: SavedTrack) -> int:
        key = self.validate_name(name).casefold()
        async with self._lock:
            playlists = self._bucket(guild_id, user_id, create=True).setdefault("playlists", {})
            if key not in playlists:
                raise KeyError(name)
            tracks = playlists[key].setdefault("tracks", [])
            if len(tracks) >= self.max_tracks:
                raise OverflowError(self.max_tracks)
            tracks.append(asdict(track))
            playlists[key]["updated_at"] = int(time.time())
            await self._save_locked()
            return len(tracks)

    async def remove_track(self, guild_id: int, user_id: int, name: str, position: int) -> dict[str, Any]:
        key = self.validate_name(name).casefold()
        async with self._lock:
            playlists = self._bucket(guild_id, user_id, create=True).setdefault("playlists", {})
            if key not in playlists:
                raise KeyError(name)
            tracks = playlists[key].setdefault("tracks", [])
            if position < 1 or position > len(tracks):
                raise IndexError(position)
            item = tracks.pop(position - 1)
            playlists[key]["updated_at"] = int(time.time())
            await self._save_locked()
            return json.loads(json.dumps(item))

    async def clear(self, guild_id: int, user_id: int, name: str) -> int:
        key = self.validate_name(name).casefold()
        async with self._lock:
            playlists = self._bucket(guild_id, user_id, create=True).setdefault("playlists", {})
            if key not in playlists:
                raise KeyError(name)
            tracks = playlists[key].setdefault("tracks", [])
            count = len(tracks)
            tracks.clear()
            playlists[key]["updated_at"] = int(time.time())
            await self._save_locked()
            return count


    async def add_tracks(self, guild_id: int, user_id: int, name: str, tracks: list[SavedTrack]) -> tuple[int, int]:
        """Atomically append as many tracks as fit. Returns (added, skipped_duplicates)."""
        key = self.validate_name(name).casefold()
        async with self._lock:
            playlists = self._bucket(guild_id, user_id, create=True).setdefault("playlists", {})
            if key not in playlists:
                raise KeyError(name)
            dest = playlists[key].setdefault("tracks", [])
            existing = {str(x.get("uri") or f"{x.get('author','')}|{x.get('title','')}").casefold() for x in dest}
            added = 0
            skipped = 0
            for track in tracks:
                fingerprint = str(track.uri or f"{track.author}|{track.title}").casefold()
                if fingerprint in existing:
                    skipped += 1
                    continue
                if len(dest) >= self.max_tracks:
                    break
                dest.append(asdict(track))
                existing.add(fingerprint)
                added += 1
            if added:
                playlists[key]["updated_at"] = int(time.time())
                await self._save_locked()
            return added, skipped

    async def move_track(self, guild_id: int, user_id: int, name: str, source: int, target: int) -> None:
        key = self.validate_name(name).casefold()
        async with self._lock:
            playlists = self._bucket(guild_id, user_id, create=True).setdefault("playlists", {})
            if key not in playlists:
                raise KeyError(name)
            tracks = playlists[key].setdefault("tracks", [])
            if not (1 <= source <= len(tracks) and 1 <= target <= len(tracks)):
                raise IndexError
            item = tracks.pop(source - 1)
            tracks.insert(target - 1, item)
            playlists[key]["updated_at"] = int(time.time())
            await self._save_locked()

    async def dedupe(self, guild_id: int, user_id: int, name: str) -> int:
        key = self.validate_name(name).casefold()
        async with self._lock:
            playlists = self._bucket(guild_id, user_id, create=True).setdefault("playlists", {})
            if key not in playlists:
                raise KeyError(name)
            tracks = playlists[key].setdefault("tracks", [])
            seen: set[str] = set()
            unique = []
            for item in tracks:
                fingerprint = str(item.get("uri") or f"{item.get('author','')}|{item.get('title','')}").casefold()
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                unique.append(item)
            removed = len(tracks) - len(unique)
            if removed:
                playlists[key]["tracks"] = unique
                playlists[key]["updated_at"] = int(time.time())
                await self._save_locked()
            return removed

    async def _save_locked(self) -> None:
        payload = json.dumps(self._data, ensure_ascii=False, separators=(",", ":")) + "\n"
        await asyncio.to_thread(self._write_atomic, payload)

    def _write_atomic(self, payload: str) -> None:
        # fsync может подвиснуть на диске. На gateway loop ему делать нечего.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
