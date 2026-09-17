from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from pathlib import Path
from urllib.parse import urlparse
from collections import deque

import discord
import wavelink

from quiddy.core.logging import done, loading, system

from .models import GuildMusicSession, LoopMode, QueueEntry
from .runtime import EmbeddedLavalinkRuntime
from .playlist_store import PlaylistStore, SavedTrack
from .utils import fmt_ms, progress_bar, safe_title, source_label

log = logging.getLogger("quiddy.music")


class MusicManager:
    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self.ctx = plugin.ctx
        self.cfg = self.ctx.config
        self.i18n = self.ctx.services.get("i18n")
        self.sessions: dict[int, GuildMusicSession] = {}
        self.node: wavelink.Node | None = None
        self.runtime: EmbeddedLavalinkRuntime | None = None
        self._search_cache: dict[str, tuple[float, list]] = {}
        self._search_locks: dict[str, asyncio.Lock] = {}
        plcfg = self.cfg.get("playlists", {})
        storage = Path(str(plcfg.get("storage", "data/music_playlists.json")))
        if not storage.is_absolute():
            storage = self.ctx.root.parent.parent / storage
        self.playlists = PlaylistStore(
            storage,
            max_playlists=int(plcfg.get("max_per_user_per_guild", 5)),
            max_tracks=int(plcfg.get("max_tracks", 100)),
        )

    def tr(self, user_id: int | None, key: str, **kwargs) -> str:
        return self.i18n.user(user_id, key, **kwargs)

    def tr_locale(self, locale: str, key: str, **kwargs) -> str:
        return self.i18n.t(locale, key, **kwargs)

    async def start(self) -> None:
        await self.playlists.load()
        node_cfg = self.cfg.get("node", {})
        try:
            mode = str(node_cfg.get("mode", "embedded")).lower()
            if mode == "embedded":
                self.runtime = EmbeddedLavalinkRuntime(self.plugin)
                await self.runtime.start()
                uri = self.runtime.uri
                password = self.runtime.password
                identifier = str(node_cfg.get("identifier", "local-1"))
            elif mode == "external":
                env_name = str(node_cfg.get("password_env", "LAVALINK_PASSWORD"))
                password = os.getenv(env_name)
                if not password:
                    raise RuntimeError(f"Missing Lavalink password in environment variable {env_name}")
                uri = str(node_cfg.get("uri", "http://127.0.0.1:2333"))
                identifier = str(node_cfg.get("identifier", "external-1"))
            else:
                raise RuntimeError(f"Unknown Lavalink node mode: {mode}")

            loading(log, "[Music] Connecting Wavelink to %s (%s)…", identifier, uri)
            self.node = wavelink.Node(uri=uri, password=password, identifier=identifier)
            await wavelink.Pool.connect(
                nodes=[self.node], client=self.ctx.bot,
                cache_capacity=int(node_cfg.get("cache_capacity", 100)),
            )
            info = await self.node.fetch_info()
            plugins = ", ".join(f"{p.name}:{p.version}" for p in info.plugins) or "none"
            done(log, "[Music] Lavalink %s ready • plugins=%s", info.version.semver, plugins)
        except BaseException:
            # Prevent Wavelink aiohttp sessions / embedded subprocesses from leaking
            # when startup fails halfway through.
            await self.shutdown()
            raise

    async def shutdown(self) -> None:
        for guild_id in list(self.sessions):
            try:
                await self.stop_guild(guild_id, disconnect=True)
            except Exception:
                log.exception("[Music] Failed to stop guild=%s", guild_id)
        if self.node:
            try:
                await asyncio.wait_for(wavelink.Pool.close(), timeout=8)
            except Exception:
                log.exception("[Music] Failed to close Wavelink pool")
            self.node = None
        if self.runtime:
            await self.runtime.stop()
            self.runtime = None

    def session(self, guild_id: int) -> GuildMusicSession:
        session = self.sessions.get(guild_id)
        if session is None:
            pcfg = self.cfg.get("player", {})
            session = GuildMusicSession(
                guild_id=guild_id,
                volume=int(pcfg.get("default_volume", 70)),
                autoplay=bool(pcfg.get("autoplay_default", False)),
            )
            self.sessions[guild_id] = session
        return session

    async def connect(self, member: discord.Member, text_channel: discord.abc.Messageable) -> GuildMusicSession:
        if not member.voice or not member.voice.channel:
            raise RuntimeError(self.tr(member.id, "music.voice.join_first"))
        session = self.session(member.guild.id)
        current = member.guild.voice_client
        if current and isinstance(current, wavelink.Player):
            player = current
            if self.cfg.get("player", {}).get("same_voice_channel_required", True):
                if player.channel and player.channel.id != member.voice.channel.id:
                    raise RuntimeError(self.tr(member.id, "music.voice.busy", channel=player.channel.mention))
        else:
            player = await member.voice.channel.connect(cls=wavelink.Player, self_deaf=True)
            player.autoplay = wavelink.AutoPlayMode.disabled
            player.inactive_timeout = int(self.cfg.get("player", {}).get("inactive_timeout_seconds", 180))
            await player.set_volume(session.volume)
            log.info("[Music] Connected guild=%s voice=%s", member.guild.id, member.voice.channel.id)
        session.player = player
        new_channel_id = getattr(text_channel, "id", None)
        if session.home_channel_id and new_channel_id and session.home_channel_id != new_channel_id and session.controller_message_id:
            await self.delete_controller(session)
        session.home_channel_id = new_channel_id
        return session

    async def require_controller(self, interaction: discord.Interaction) -> GuildMusicSession:
        uid = interaction.user.id
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            raise RuntimeError(self.tr(uid, "music.guild_only"))
        session = self.sessions.get(interaction.guild.id)
        player = interaction.guild.voice_client
        if not session or not isinstance(player, wavelink.Player) or not player.connected:
            raise RuntimeError(self.tr(uid, "music.nothing_playing"))
        session.player = player
        if self.cfg.get("player", {}).get("same_voice_channel_required", True):
            if not interaction.user.voice or not interaction.user.voice.channel:
                raise RuntimeError(self.tr(uid, "music.voice.join_first"))
            if player.channel and interaction.user.voice.channel.id != player.channel.id:
                raise RuntimeError(self.tr(uid, "music.voice.control_same", channel=player.channel.mention))
        return session

    async def can_destructive(self, member: discord.Member) -> bool:
        pcfg = self.cfg.get("permissions", {})
        if not bool(pcfg.get("destructive_requires_dj", False)):
            return True
        if member.guild_permissions.manage_guild and bool(pcfg.get("manage_guild_is_dj", True)):
            return True
        role_ids = {int(x) for x in pcfg.get("dj_role_ids", []) or []}
        return any(role.id in role_ids for role in member.roles)


    def _is_url(self, query: str) -> bool:
        return bool(re.match(r"^[a-z][a-z0-9+.-]*://", query, flags=re.I))

    def _safe_media_url(self, query: str) -> bool:
        # Пользовательский URL уходит в отдельный Lavalink-процесс. Не разрешаю
        # превращать музыкальную команду в SSRF до localhost/VPC/metadata endpoint.
        try:
            parsed = urlparse(query)
            host = (parsed.hostname or "").lower().rstrip(".")
            return parsed.scheme == "https" and (
                host in {"youtube.com", "www.youtube.com", "music.youtube.com", "youtu.be", "m.youtube.com"}
                or host.endswith(".youtube.com")
            )
        except ValueError:
            return False

    def _known_search_prefix(self, query: str) -> bool:
        lower = query.lower()
        return lower.startswith((
            "ytsearch:", "ytmsearch:", "scsearch:",
            "amsearch:", "dzsearch:", "ymsearch:", "vksearch:",
        ))

    async def _fetch_identifier(self, identifier: str):
        """Fetch an *exact* Lavalink identifier without Wavelink rewriting it.

        This intentionally uses Pool.fetch_tracks rather than Playable.search. The
        latter helpfully adds a search prefix and was the source of the previous
        ytmsearch:/ytsearch: routing bugs. Pool.fetch_tracks sends our identifier
        verbatim to Lavalink.
        """
        return await wavelink.Pool.fetch_tracks(identifier, node=self.node)

    async def _yt_dlp_search_urls(self, query: str, limit: int) -> list[str]:
        """Last-resort YouTube search performed by Quiddy's own yt-dlp.

        LavaSrc normally handles ``ytsearch:`` directly. If a Lavalink/plugin
        regression ever makes that search return nothing, this bypasses the search
        source completely, asks the exact yt-dlp binary that powers playback for
        results, then feeds the resulting direct YouTube URLs back to Lavalink.
        """
        runtime = self.runtime
        if not runtime or not runtime.ytdlp_path:
            return []
        limit = max(1, min(int(limit), 10))
        args = [
            str(runtime.ytdlp_path),
            "-q", "--no-warnings", "--flat-playlist", "--skip-download",
            "--dump-single-json",
            *list(runtime.ytdlp_extra_args or []),
            f"ytsearch{limit}:{query}",
        ]
        cfg = self.cfg.get("search", {}) or {}
        timeout = max(5.0, float(cfg.get("fallback_timeout_seconds", 25)))
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            log.warning("[Music] yt-dlp search fallback timed out for %r", query)
            return []
        if proc.returncode != 0:
            tail = stderr.decode("utf-8", "replace")[-500:].replace("\n", " | ")
            log.warning("[Music] yt-dlp search fallback failed code=%s • %s", proc.returncode, tail)
            return []
        try:
            payload = json.loads(stdout.decode("utf-8", "replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            log.warning("[Music] yt-dlp search fallback returned invalid JSON")
            return []

        entries = payload.get("entries") or []
        urls: list[str] = []
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("webpage_url") or "").strip()
            video_id = str(entry.get("id") or "").strip()
            if not url and video_id:
                url = f"https://www.youtube.com/watch?v={video_id}"
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
            if len(urls) >= limit:
                break
        return urls

    async def _youtube_search(self, query: str):
        """Fast deterministic YouTube search with a short hot cache and hard fallback."""
        cfg = self.cfg.get("search", {}) or {}
        key = " ".join(query.casefold().split())
        ttl = max(0, int(cfg.get("cache_ttl_seconds", 45)))
        cached = self._search_cache.get(key)
        if cached and cached[0] > time.monotonic():
            return list(cached[1])

        lock = self._search_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._search_cache.get(key)
            if cached and cached[0] > time.monotonic():
                return list(cached[1])

            identifier = f"ytsearch:{query}"
            try:
                timeout = max(2.0, float(cfg.get("primary_timeout_seconds", 8)))
                result = await asyncio.wait_for(self._fetch_identifier(identifier), timeout=timeout)
                if result:
                    tracks = list(result.tracks) if isinstance(result, wavelink.Playlist) else list(result)
                    self._search_cache[key] = (time.monotonic() + ttl, tracks)
                    # Не даю поисковому кешу расти бесконечно на больших серверах.
                    if len(self._search_cache) > 256:
                        now = time.monotonic()
                        self._search_cache = {k: v for k, v in self._search_cache.items() if v[0] > now}
                    return tracks
            except Exception as exc:
                log.warning("[Music] Primary YouTube search failed for %r: %s: %s", query, type(exc).__name__, exc)

            if not bool(cfg.get("yt_dlp_fallback", True)):
                return []
            limit = int(cfg.get("fallback_limit", 5))
            urls = await self._yt_dlp_search_urls(query, limit)
            if not urls:
                return []

            semaphore = asyncio.Semaphore(max(1, min(int(cfg.get("fallback_parallelism", 3)), 5)))

            async def load_one(url: str):
                async with semaphore:
                    try:
                        loaded = await self._fetch_identifier(url)
                        if isinstance(loaded, wavelink.Playlist):
                            return loaded.tracks[0] if loaded.tracks else None
                        return loaded[0] if loaded else None
                    except Exception as exc:
                        log.debug("[Music] Fallback direct load failed %s: %s", url, exc)
                        return None

            tracks = await asyncio.gather(*(load_one(url) for url in urls))
            resolved = [track for track in tracks if track is not None]
            if resolved:
                self._search_cache[key] = (time.monotonic() + ttl, list(resolved))
                log.info("[Music] YouTube search fallback resolved %s/%s result(s) for %r", len(resolved), len(urls), query)
            return resolved

    async def search(self, query: str, source: str = "auto"):
        query = query.strip()
        if not query:
            raise ValueError("empty query")
        lower = query.lower()


        if self._is_url(query):
            if not self._safe_media_url(query):
                raise ValueError("Only HTTPS YouTube URLs are allowed")
            return await self._fetch_identifier(query)
        if self._known_search_prefix(query):
            # Пользовательские search-prefix оставляю только YouTube: остальные
            # источники в этой сборке не нужны и расширяют поверхность Lavalink.
            if not query.lower().startswith(("ytsearch:", "ytmsearch:")):
                raise ValueError("Unsupported search source")
            return await self._fetch_identifier(query)


        # Both Auto and the two YouTube UI choices intentionally use ytsearch:.
        # Our playback backend is yt-dlp; ytmsearch: is not used because it was
        # demonstrably returning empty results on the live node.
        return await self._youtube_search(query)

    def saved_track(self, track: wavelink.Playable) -> SavedTrack:
        uri = getattr(track, "uri", None)
        title = str(getattr(track, "title", "Unknown"))
        author = str(getattr(track, "author", "Unknown"))
        return SavedTrack(
            query=uri or f"{title} {author}", title=title, author=author, uri=uri,
            source=source_label(track), length_ms=int(getattr(track, "length", 0) or 0),
            artwork=getattr(track, "artwork", None),
        )

    def _valid_track(self, track: wavelink.Playable) -> bool:
        pcfg = self.cfg.get("player", {})
        length = int(getattr(track, "length", 0) or 0)
        is_stream = bool(getattr(track, "is_stream", False))
        if is_stream and not bool(pcfg.get("allow_streams", True)):
            return False
        max_ms = int(pcfg.get("max_track_length_seconds", 21600)) * 1000
        return is_stream or max_ms <= 0 or length <= max_ms

    async def enqueue_search(
        self, session: GuildMusicSession, result, requester: discord.Member, *, front: bool = False
    ) -> tuple[int, str]:
        max_queue = int(self.cfg.get("player", {}).get("max_queue_size", 500))
        max_playlist = int(self.cfg.get("player", {}).get("max_playlist_tracks", 200))
        added: list[QueueEntry] = []
        label = "playlist" if isinstance(result, wavelink.Playlist) else "track"
        tracks = list(result.tracks)[:max_playlist] if isinstance(result, wavelink.Playlist) else list(result[:1])
        async with session.lock:
            room = max(0, max_queue - len(session.queue))
            for track in tracks[:room]:
                if self._valid_track(track):
                    added.append(QueueEntry(track=track, requester_id=requester.id, requester_name=str(requester)))
            if front:
                for entry in reversed(added):
                    session.queue.appendleft(entry)
            else:
                session.queue.extend(added)
            if added:
                session.controller_revision += 1
            should_start = session.player is not None and session.current is None and not session.player.playing
        if should_start:
            await self.advance(session)
        elif added:
            await self.update_controller(session)
        return len(added), label

    async def advance(self, session: GuildMusicSession) -> None:
        player = session.player
        if not player or not player.connected:
            return
        entry: QueueEntry | None = None
        became_empty = False
        async with session.lock:
            bypass_track_loop = session.force_next
            session.force_next = False
            if session.current and session.loop_mode == LoopMode.TRACK and not bypass_track_loop:
                entry = session.current
            elif session.queue:
                if session.current:
                    session.history.append(session.current)
                entry = session.queue.popleft()
                session.current = entry
                if session.loop_mode == LoopMode.QUEUE:
                    session.queue.append(entry)
            else:
                if session.current:
                    session.history.append(session.current)
                session.current = None
                session.controller_revision += 1
                became_empty = True
                if session.autoplay:
                    player.autoplay = wavelink.AutoPlayMode.enabled
            if entry is not None:
                session.controller_locale = self.i18n.get_locale(entry.requester_id)
                session.controller_revision += 1
                player.autoplay = wavelink.AutoPlayMode.disabled
        if entry is None:
            session.loading_track = False
            session.playback_started = False
            if became_empty and not session.autoplay and bool(self.cfg.get("ui", {}).get("delete_controller_when_idle", True)):
                await self.delete_controller(session)
            return

        # player.play() может ждать, пока yt-dlp/Lavalink подготовит поток. Для первого
        # трека панель не создаю вообще; между треками уже существующая панель честно
        # показывает загрузку, а TrackStart переключает её в полноценный плеер.
        session.loading_track = True
        session.playback_started = False
        session.controller_revision += 1
        if session.controller_message_id:
            await self.update_controller(session, loading=True, force=True)
        await player.play(entry.track, volume=session.volume)
        log.info("[Music] ▶ requested %s — %s • guild=%s requester=%s",
                 safe_title(entry.track.author, 40), safe_title(entry.track.title, 70), session.guild_id, entry.requester_id)

    async def skip(self, session: GuildMusicSession) -> None:
        session.force_next = True
        await session.player.skip(force=True)

    async def handle_track_start(self, payload) -> None:
        player = payload.player
        if not player or not player.guild:
            return
        session = self.sessions.get(player.guild.id)
        if not session or session.player is not player:
            return
        session.loading_track = False
        session.playback_started = True
        session.controller_revision += 1
        await self.update_controller(session, force=True)

    async def handle_track_end(self, payload) -> None:
        player = payload.player
        if not player or not player.guild:
            return
        session = self.sessions.get(player.guild.id)
        if not session or session.player is not player:
            return
        if session.suppress_advance:
            session.suppress_advance = False
            return
        await self.advance(session)

    async def handle_track_problem(self, payload, kind: str) -> None:
        player = payload.player
        if not player or not player.guild:
            return
        session = self.sessions.get(player.guild.id)
        log.warning("[Music] Track %s guild=%s track=%s", kind, player.guild.id, getattr(payload, "track", None))
        if session:
            await self.update_controller(session, failed=True)
            session.force_next = True
        try:
            await player.skip(force=True)
        except Exception:
            log.exception("[Music] Failed to recover from track %s", kind)

    async def stop_guild(self, guild_id: int, *, disconnect: bool) -> None:
        session = self.sessions.get(guild_id)
        if not session:
            return
        async with session.lock:
            session.queue.clear()
            session.history.clear()
            session.current = None
            session.suppress_advance = True
            session.force_next = False
            session.loading_track = False
            session.playback_started = False
            session.controller_revision += 1
        player = session.player
        if player:
            try:
                await player.stop()
            except Exception:
                pass
        if bool(self.cfg.get("ui", {}).get("delete_controller_on_stop", True)):
            await self.delete_controller(session)
        if player and disconnect and player.connected:
            try:
                await player.disconnect()
            except Exception:
                log.exception("[Music] Failed to disconnect guild=%s", guild_id)
        if disconnect:
            self.sessions.pop(guild_id, None)
        log.info("[Music] Stopped guild=%s disconnect=%s", guild_id, disconnect)

    async def stop(self, guild_id: int, *, disconnect: bool = False) -> None:
        await self.stop_guild(guild_id, disconnect=disconnect)

    def cycle_loop(self, session: GuildMusicSession) -> LoopMode:
        order = [LoopMode.OFF, LoopMode.TRACK, LoopMode.QUEUE]
        session.loop_mode = order[(order.index(session.loop_mode) + 1) % len(order)]
        return session.loop_mode

    def set_loop(self, session: GuildMusicSession, value: str) -> LoopMode:
        session.loop_mode = LoopMode(value); return session.loop_mode

    def shuffle(self, session: GuildMusicSession) -> int:
        items = list(session.queue); random.shuffle(items); session.queue = deque(items); return len(items)

    def remove(self, session: GuildMusicSession, position: int) -> QueueEntry:
        items = list(session.queue)
        if position < 1 or position > len(items): raise IndexError(position)
        entry = items.pop(position - 1); session.queue = deque(items); return entry

    def move(self, session: GuildMusicSession, source: int, target: int) -> None:
        items = list(session.queue)
        if not (1 <= source <= len(items) and 1 <= target <= len(items)): raise IndexError
        item = items.pop(source - 1); items.insert(target - 1, item); session.queue = deque(items)
        session.controller_revision += 1

    def dedupe_queue(self, session: GuildMusicSession) -> int:
        seen: set[str] = set()
        unique: list[QueueEntry] = []
        removed = 0
        for entry in session.queue:
            track = entry.track
            key = str(getattr(track, "uri", None) or f"{getattr(track, 'author', '')}|{getattr(track, 'title', '')}").casefold()
            if key in seen:
                removed += 1
                continue
            seen.add(key)
            unique.append(entry)
        session.queue = deque(unique)
        if removed:
            session.controller_revision += 1
        return removed

    async def jump(self, session: GuildMusicSession, position: int) -> QueueEntry:
        items = list(session.queue)
        if position < 1 or position > len(items):
            raise IndexError(position)
        target = items.pop(position - 1)
        session.queue = deque([target, *items])
        session.force_next = True
        await session.player.skip(force=True)
        return target

    def history_embed(self, session: GuildMusicSession, locale: str | None = None) -> discord.Embed:
        locale = locale or session.controller_locale
        items = list(session.history)[-10:][::-1]
        color = int(self.cfg.get("ui", {}).get("color", 0xFFB000))
        lines = [
            f"`{i:02d}.` **{safe_title(e.track.title, 60)}** — {safe_title(e.track.author, 35)}"
            for i, e in enumerate(items, 1)
        ]
        return discord.Embed(
            title=self.tr_locale(locale, "music.history.title"),
            description="\n".join(lines) if lines else self.tr_locale(locale, "music.history.empty"),
            color=color,
        )

    def stats_embed(self, session: GuildMusicSession, locale: str | None = None) -> discord.Embed:
        locale = locale or session.controller_locale
        color = int(self.cfg.get("ui", {}).get("color", 0xFFB000))
        embed = discord.Embed(title=self.tr_locale(locale, "music.stats.title"), color=color)
        embed.add_field(name=self.tr_locale(locale, "music.stats.queue"), value=str(len(session.queue)))
        embed.add_field(name=self.tr_locale(locale, "music.stats.history"), value=str(len(session.history)))
        embed.add_field(name=self.tr_locale(locale, "music.stats.volume"), value=f"{session.volume}%")
        embed.add_field(name=self.tr_locale(locale, "music.stats.loop"), value=self._loop_label(session, locale))
        embed.add_field(name=self.tr_locale(locale, "music.stats.autoplay"), value=self.tr_locale(locale, "music.state.on" if session.autoplay else "music.state.off"))
        embed.add_field(name=self.tr_locale(locale, "music.stats.node"), value=self.node.identifier if self.node else "—")
        return embed

    async def previous(self, session: GuildMusicSession) -> QueueEntry:
        if not session.history:
            raise RuntimeError(self.tr_locale(session.controller_locale, "music.history.empty"))
        previous = session.history.pop()
        if session.current: session.queue.appendleft(session.current)
        session.current = previous
        session.controller_locale = self.i18n.get_locale(previous.requester_id)
        session.suppress_advance = True
        await session.player.play(previous.track, volume=session.volume)
        return previous

    def _loop_label(self, session: GuildMusicSession, locale: str) -> str:
        return self.tr_locale(locale, f"music.loop.{session.loop_mode.value}")

    def now_embed(self, session: GuildMusicSession, locale: str | None = None) -> discord.Embed:
        locale = locale or session.controller_locale
        entry = session.current
        color = int(self.cfg.get("ui", {}).get("color", 0xFFB000))
        if not entry:
            return discord.Embed(title=self.tr_locale(locale, "music.now.empty"), color=color)
        track = entry.track
        pos = int(getattr(session.player, "position", 0) or 0) if session.player else 0
        length = int(getattr(track, "length", 0) or 0)
        stream = bool(getattr(track, "is_stream", False))
        timing = "LIVE" if stream else f"{fmt_ms(pos)} / {fmt_ms(length)}"
        embed = discord.Embed(
            title=self.tr_locale(locale, "music.now.title"),
            description=f"**[{safe_title(track.title, 100)}]({track.uri})**\n{track.author}\n\n`{progress_bar(pos, length)}`\n`{timing}`",
            color=color,
        )
        artwork = getattr(track, "artwork", None)
        if artwork: embed.set_thumbnail(url=artwork)
        embed.add_field(name=self.tr_locale(locale, "music.now.source"), value=source_label(track), inline=True)
        embed.add_field(name=self.tr_locale(locale, "music.now.volume"), value=f"{session.volume}%", inline=True)
        embed.add_field(name=self.tr_locale(locale, "music.now.queue"), value=str(len(session.queue)), inline=True)
        embed.add_field(name=self.tr_locale(locale, "music.now.loop"), value=self._loop_label(session, locale), inline=True)
        embed.add_field(name=self.tr_locale(locale, "music.now.autoplay"), value=self.tr_locale(locale, "music.state.on" if session.autoplay else "music.state.off"), inline=True)
        embed.set_footer(text=self.tr_locale(locale, "music.now.requested_by", name=entry.requester_name))
        return embed

    def loading_embed(self, session: GuildMusicSession) -> discord.Embed:
        locale = session.controller_locale
        entry = session.current
        color = int(self.cfg.get("ui", {}).get("color", 0xFFB000))
        title = safe_title(entry.track.title, 100) if entry else self.tr_locale(locale, "music.loading.unknown")
        author = safe_title(entry.track.author, 60) if entry else ""
        description = self.tr_locale(locale, "music.loading.panel_description", title=title, author=author)
        embed = discord.Embed(
            title=self.tr_locale(locale, "music.loading.panel_title"),
            description=description,
            color=color,
        )
        if entry:
            artwork = getattr(entry.track, "artwork", None)
            if artwork:
                embed.set_thumbnail(url=artwork)
        embed.set_footer(text=self.tr_locale(locale, "music.loading.panel_footer", queue=len(session.queue)))
        return embed

    def error_embed(self, session: GuildMusicSession) -> discord.Embed:
        locale = session.controller_locale
        title = safe_title(session.current.track.title, 100) if session.current else "track"
        return discord.Embed(
            title=self.tr_locale(locale, "music.error.title"),
            description=self.tr_locale(locale, "music.error.description", title=title),
            color=0xED4245,
        )

    def queue_embed(self, session: GuildMusicSession, page: int = 1, locale: str | None = None) -> discord.Embed:
        locale = locale or session.controller_locale
        color = int(self.cfg.get("ui", {}).get("color", 0xFFB000))
        per_page = max(1, int(self.cfg.get("ui", {}).get("queue_page_size", 10)))
        items = list(session.queue); pages = max(1, (len(items) + per_page - 1) // per_page); page = max(1, min(page, pages))
        start = (page - 1) * per_page
        lines = [f"`{idx:02d}.` **{safe_title(e.track.title, 60)}** — {safe_title(e.track.author, 35)} · `{fmt_ms(e.track.length)}`" for idx, e in enumerate(items[start:start+per_page], start=start+1)]
        embed = discord.Embed(
            title=self.tr_locale(locale, "music.queue.title", count=len(items)),
            description="\n".join(lines) if lines else self.tr_locale(locale, "music.queue.empty"), color=color,
        )
        embed.set_footer(text=self.tr_locale(locale, "music.queue.page", page=page, pages=pages)); return embed

    async def delete_controller(self, session: GuildMusicSession) -> None:
        async with session.controller_lock:
            message_id = session.controller_message_id
            channel_id = session.home_channel_id
            session.controller_message_id = None
            session.controller_rendered_revision = -1
            if not message_id or not channel_id:
                return
            guild = session.player.guild if session.player and session.player.guild else self.ctx.bot.get_guild(session.guild_id)
            if not guild:
                return
            channel = guild.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.ctx.bot.fetch_channel(channel_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    return
            if not hasattr(channel, "get_partial_message"):
                return
            try:
                await channel.get_partial_message(message_id).delete()
            except (discord.NotFound, discord.Forbidden):
                pass
            except discord.HTTPException:
                log.warning("[Music] Could not delete controller guild=%s message=%s", session.guild_id, message_id)

    async def update_controller(self, session: GuildMusicSession, *, failed: bool = False, loading: bool = False, force: bool = False) -> None:
        player = session.player
        if not player or not player.guild or not session.home_channel_id:
            return
        channel = player.guild.get_channel(session.home_channel_id)
        if channel is None:
            try:
                channel = await self.ctx.bot.fetch_channel(session.home_channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return
        if not hasattr(channel, "send"):
            return
        async with session.controller_lock:
            # Build after acquiring the render lock so competing TrackStart/button/ticker
            # updates can never overwrite a newer state with an older panel.
            if loading or session.loading_track:
                embed = self.loading_embed(session)
            else:
                embed = self.error_embed(session) if failed else self.now_embed(session)
            from .views import MusicControls
            view = MusicControls(self, session)
            if session.controller_message_id:
                try:
                    if hasattr(channel, "get_partial_message"):
                        message = channel.get_partial_message(session.controller_message_id)
                    else:
                        message = await channel.fetch_message(session.controller_message_id)
                    retries = max(1, int(self.cfg.get("ui", {}).get("controller_edit_retries", 2)))
                    for attempt in range(retries):
                        try:
                            await message.edit(embed=embed, view=view)
                            session.controller_rendered_revision = session.controller_revision
                            return
                        except discord.HTTPException:
                            if attempt + 1 >= retries:
                                raise
                            await asyncio.sleep(0.35 * (attempt + 1))
                except discord.NotFound:
                    session.controller_message_id = None
                except discord.Forbidden:
                    log.warning("[Music] No permission to edit controller guild=%s", session.guild_id)
                    return
                except discord.HTTPException as exc:
                    # Do not immediately create a duplicate panel on transient Discord failures.
                    log.warning("[Music] Controller edit failed guild=%s: %s", session.guild_id, exc)
                    return
            # Новую панель создаю только после реального TrackStart. Пока первый
            # трек готовится, пользователь видит локализованный loading-response команды.
            if not session.playback_started:
                return
            try:
                message = await channel.send(embed=embed, view=view)
                session.controller_message_id = message.id
                session.controller_rendered_revision = session.controller_revision
            except discord.HTTPException:
                log.exception("[Music] Failed to create controller guild=%s", session.guild_id)

    async def refresh_controllers(self) -> None:
        for session in tuple(self.sessions.values()):
            player = session.player
            try:
                if not player or not player.connected:
                    if session.controller_message_id:
                        await self.delete_controller(session)
                    continue
                if not session.current:
                    if session.controller_message_id and bool(self.cfg.get("ui", {}).get("delete_controller_when_idle", True)):
                        await self.delete_controller(session)
                    continue
                await self.update_controller(session, loading=session.loading_track)
            except Exception:
                log.exception("[Music] Periodic controller refresh failed guild=%s", session.guild_id)

    async def health(self) -> dict:
        if not self.node: return {"status": "down", "node": None, "players": len(self.sessions)}
        try:
            stats = await self.node.fetch_stats()
            return {"status": "up", "node": self.node.identifier, "players": stats.players, "playing": stats.playing, "uptime_ms": stats.uptime}
        except Exception as exc:
            return {"status": "down", "error": f"{type(exc).__name__}: {exc}", "players": len(self.sessions)}

    async def console(self, args: list[str]) -> None:
        from rich.table import Table
        from rich.panel import Panel
        from quiddy.core.logging import console
        action = args[0].lower() if args else "status"
        if action in {"status", "nodes"}:
            health = await self.health(); system(log, "[Music] status=%s node=%s players=%s playing=%s", health.get("status"), health.get("node"), health.get("players"), health.get("playing", "—")); return
        if action == "doctor":
            runtime = await self.runtime.doctor() if self.runtime else {"mode": "external"}; health = await self.health()
            table = Table(title="Music Doctor", header_style="bold #ffb000"); table.add_column("Check"); table.add_column("Value")
            for key, value in {**runtime, "wavelink": health.get("status"), "players": health.get("players")}.items(): table.add_row(str(key), str(value))
            console.print(table); return
        if action == "logs":
            if not self.runtime: log.warning("[Music] Embedded runtime is not active"); return
            count = int(args[1]) if len(args) > 1 and args[1].isdigit() else 30
            console.print(Panel("\n".join(self.runtime.recent_logs(count)) or "No Lavalink logs yet", title="Lavalink tail")); return
        if action == "cipher":
            if not self.runtime or not self.runtime.cipher:
                log.warning("[Music] Embedded cipher is not active (an external cipher may be configured)"); return
            sub = args[1].lower() if len(args) > 1 else "status"
            if sub == "logs":
                count = int(args[2]) if len(args) > 2 and args[2].isdigit() else 30
                console.print(Panel("\n".join(self.runtime.recent_cipher_logs(count)) or "No cipher logs yet", title="yt-cipher tail")); return
            if sub == "restart":
                loading(log, "[Music] Restarting local yt-cipher…")
                await self.runtime.restart_cipher(); done(log, "[Music] yt-cipher restarted"); return
            if sub == "update":
                loading(log, "[Music] Updating local yt-cipher source…")
                await self.runtime.restart_cipher(refresh=True); done(log, "[Music] yt-cipher updated and restarted"); return
            info = await self.runtime.cipher.doctor()
            table = Table(title="yt-cipher", header_style="bold #ffb000"); table.add_column("Check"); table.add_column("Value")
            for key, value in info.items(): table.add_row(str(key), str(value))
            console.print(table); return
        if action == "restart":
            if not self.runtime: log.warning("[Music] Embedded runtime is not active"); return
            loading(log, "[Music] Restarting embedded Lavalink…")
            if self.node:
                try: await self.node.close(eject=True)
                except Exception: pass
                self.node = None
            await self.runtime.restart(); node_cfg = self.cfg.get("node", {}); identifier = str(node_cfg.get("identifier", "local-1"))
            self.node = wavelink.Node(uri=self.runtime.uri, password=self.runtime.password, identifier=identifier)
            await wavelink.Pool.connect(nodes=[self.node], client=self.ctx.bot, cache_capacity=int(node_cfg.get("cache_capacity", 100)))
            done(log, "[Music] Embedded Lavalink restarted"); return
        if action == "players":
            table = Table(title="Music Players", header_style="bold #ffb000"); table.add_column("Guild"); table.add_column("Voice"); table.add_column("Track"); table.add_column("Queue"); table.add_column("Loop")
            for gid, session in sorted(self.sessions.items()):
                voice = str(session.player.channel) if session.player and session.player.channel else "—"; track = safe_title(session.current.track.title, 40) if session.current else "—"
                table.add_row(str(gid), voice, track, str(len(session.queue)), session.loop_mode.value)
            console.print(table); return
        if action == "disconnect" and len(args) >= 2:
            await self.stop_guild(int(args[1]), disconnect=True); done(log, "[Music] Disconnected guild=%s", args[1]); return
        log.warning("Usage: music [status|nodes|doctor|logs [n]|cipher [status|logs [n]|restart|update]|restart|players|disconnect <guild_id>]")
