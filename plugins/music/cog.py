from __future__ import annotations

import asyncio
import logging

import discord
import wavelink
from discord import app_commands
from discord.ext import commands

from .utils import fmt_ms, safe_title

log = logging.getLogger("quiddy.music.commands")


class MusicCog(commands.GroupCog, group_name="music", group_description="Music / Музика / Музыка"):
    playlist_group = app_commands.Group(name="playlist", description="Personal playlists / Личные плейлисты")
    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self.manager = plugin.manager

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.guild_only"), ephemeral=True)
            return False
        return True

    async def _session(self, interaction: discord.Interaction):
        try:
            return await self.manager.require_controller(interaction)
        except RuntimeError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return None

    @app_commands.command(name="play", description="Play a track / Увімкнути трек / Включить трек")
    @app_commands.describe(query="Track name or YouTube URL", source="Where to search when query is not a URL", play_next="Put this request at the front of the queue")
    @app_commands.choices(source=[
        app_commands.Choice(name="YouTube (recommended)", value="auto"),
        app_commands.Choice(name="YouTube", value="youtube"),
    ])
    async def play(self, interaction: discord.Interaction, query: str, source: app_commands.Choice[str] | None = None, play_next: bool = False) -> None:
        uid = interaction.user.id
        await interaction.response.defer(thinking=True, ephemeral=True)
        await interaction.edit_original_response(content=self.manager.tr(uid, "music.loading.search", query=safe_title(query, 80)))
        try:
            session = await self.manager.connect(interaction.user, interaction.channel)
            result = await self.manager.search(query, source.value if source else "auto")
            if not result:
                await interaction.edit_original_response(content=self.manager.tr(uid, "music.search.none"))
                return
            added, _ = await self.manager.enqueue_search(session, result, interaction.user, front=play_next)
            if not added:
                await interaction.edit_original_response(content=self.manager.tr(uid, "music.queue.rejected"))
                return
            if isinstance(result, wavelink.Playlist):
                name = getattr(result, "name", "playlist") or "playlist"
                text = self.manager.tr(uid, "music.queue.playlist_added", name=name, count=added)
            else:
                track = result[0]
                text = self.manager.tr(uid, "music.queue.track_added", title=safe_title(track.title, 90), uri=track.uri, duration=fmt_ms(track.length))
            # Important: no now-playing embed here. The single controller is created/edited only on TrackStartEvent.
            await interaction.edit_original_response(content=text)
        except Exception as exc:
            log.exception("/music play failed")
            await interaction.edit_original_response(content=self.manager.tr(uid, "music.play.failed", error=f"{type(exc).__name__}: {exc}"))

    @app_commands.command(name="search", description="Search YouTube / Пошук / Поиск")
    @app_commands.choices(source=[
        app_commands.Choice(name="YouTube (recommended)", value="auto"),
        app_commands.Choice(name="YouTube", value="youtube"),
    ])
    async def search(self, interaction: discord.Interaction, query: str, source: app_commands.Choice[str] | None = None) -> None:
        uid = interaction.user.id
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self.manager.search(query, source.value if source else "auto")
            if not result or isinstance(result, wavelink.Playlist):
                await interaction.followup.send(self.manager.tr(uid, "music.search.none_short"), ephemeral=True); return
            tracks = list(result[:10])
            lines = [f"`{i}.` **{safe_title(t.title, 70)}** — {safe_title(t.author, 35)} · `{fmt_ms(t.length)}`" for i, t in enumerate(tracks, 1)]
            from .views import SearchResultsView
            await interaction.followup.send(
                self.manager.tr(uid, "music.search.title") + "\n" + "\n".join(lines),
                view=SearchResultsView(self.manager, uid, tracks),
                ephemeral=True,
            )
        except Exception as exc:
            log.exception("/music search failed")
            await interaction.followup.send(self.manager.tr(uid, "music.play.failed", error=f"{type(exc).__name__}: {exc}"), ephemeral=True)

    @app_commands.command(name="now", description="Now playing / Зараз грає / Сейчас играет")
    async def now(self, interaction: discord.Interaction) -> None:
        session = await self._session(interaction)
        if session:
            await interaction.response.send_message(embed=self.manager.now_embed(session, self.manager.i18n.get_locale(interaction.user.id)), ephemeral=True)

    @app_commands.command(name="queue", description="Queue / Черга / Очередь")
    async def queue(self, interaction: discord.Interaction, page: app_commands.Range[int, 1, 50] = 1) -> None:
        session = await self._session(interaction)
        if session:
            await interaction.response.send_message(embed=self.manager.queue_embed(session, page, self.manager.i18n.get_locale(interaction.user.id)), ephemeral=True)

    @app_commands.command(name="pause", description="Pause")
    async def pause(self, interaction: discord.Interaction) -> None:
        session = await self._session(interaction)
        if session:
            await session.player.pause(True); await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.pause")); await self.manager.update_controller(session)

    @app_commands.command(name="resume", description="Resume")
    async def resume(self, interaction: discord.Interaction) -> None:
        session = await self._session(interaction)
        if session:
            await session.player.pause(False); await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.resume")); await self.manager.update_controller(session)

    @app_commands.command(name="skip", description="Skip")
    async def skip(self, interaction: discord.Interaction) -> None:
        session = await self._session(interaction)
        if session:
            await self.manager.skip(session); await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.skip"))

    @app_commands.command(name="stop", description="Stop and clear queue")
    async def stop(self, interaction: discord.Interaction) -> None:
        session = await self._session(interaction)
        if not session: return
        if not await self.manager.can_destructive(interaction.user):
            await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.dj_required"), ephemeral=True); return
        await self.manager.stop(interaction.guild.id, disconnect=False)
        await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.stop"))

    @app_commands.command(name="disconnect", description="Disconnect")
    async def disconnect(self, interaction: discord.Interaction) -> None:
        session = await self._session(interaction)
        if not session: return
        if not await self.manager.can_destructive(interaction.user):
            await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.dj_required"), ephemeral=True); return
        await self.manager.stop(interaction.guild.id, disconnect=True)
        await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.disconnect"))

    @app_commands.command(name="volume", description="Volume")
    async def volume(self, interaction: discord.Interaction, value: app_commands.Range[int, 0, 150]) -> None:
        session = await self._session(interaction)
        if not session: return
        maximum = int(self.manager.cfg.get("player", {}).get("max_volume", 150))
        if value > maximum:
            await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.volume.max", maximum=maximum), ephemeral=True); return
        session.volume = int(value); await session.player.set_volume(session.volume)
        await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.volume.changed", volume=session.volume)); await self.manager.update_controller(session)

    @app_commands.command(name="seek", description="Seek")
    async def seek(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]) -> None:
        session = await self._session(interaction)
        if not session or not session.current: return
        if getattr(session.current.track, "is_stream", False):
            await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.seek.live"), ephemeral=True); return
        target = min(int(seconds) * 1000, int(session.current.track.length)); await session.player.seek(target)
        await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.seek.changed", position=fmt_ms(target))); await self.manager.update_controller(session)

    @app_commands.command(name="loop", description="Loop mode")
    @app_commands.choices(mode=[app_commands.Choice(name="Off", value="off"), app_commands.Choice(name="Track", value="track"), app_commands.Choice(name="Queue", value="queue")])
    async def loop(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        session = await self._session(interaction)
        if session:
            value = self.manager.set_loop(session, mode.value); label = self.manager.tr(interaction.user.id, f"music.loop.{value.value}")
            await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.loop.changed", mode=label)); await self.manager.update_controller(session)

    @app_commands.command(name="shuffle", description="Shuffle queue")
    async def shuffle(self, interaction: discord.Interaction) -> None:
        session = await self._session(interaction)
        if session:
            count = self.manager.shuffle(session); await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.shuffle", count=count)); await self.manager.update_controller(session)

    @app_commands.command(name="clear", description="Clear queue")
    async def clear(self, interaction: discord.Interaction) -> None:
        session = await self._session(interaction)
        if session:
            count = len(session.queue); session.queue.clear(); await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.clear", count=count)); await self.manager.update_controller(session)

    @app_commands.command(name="remove", description="Remove queue item")
    async def remove(self, interaction: discord.Interaction, position: app_commands.Range[int, 1, 500]) -> None:
        session = await self._session(interaction)
        if not session: return
        try: entry = self.manager.remove(session, int(position))
        except IndexError:
            await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.remove.invalid"), ephemeral=True); return
        await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.remove.done", title=safe_title(entry.track.title, 80))); await self.manager.update_controller(session)

    @app_commands.command(name="move", description="Move queue item")
    async def move(self, interaction: discord.Interaction, source: app_commands.Range[int, 1, 500], target: app_commands.Range[int, 1, 500]) -> None:
        session = await self._session(interaction)
        if not session: return
        try: self.manager.move(session, int(source), int(target))
        except IndexError:
            await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.move.invalid"), ephemeral=True); return
        await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.move.done", source=source, target=target)); await self.manager.update_controller(session)

    @app_commands.command(name="previous", description="Previous track")
    async def previous(self, interaction: discord.Interaction) -> None:
        session = await self._session(interaction)
        if not session: return
        try: entry = await self.manager.previous(session)
        except RuntimeError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True); return
        await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.previous", title=safe_title(entry.track.title, 80)))

    @app_commands.command(name="replay", description="Replay current track")
    async def replay(self, interaction: discord.Interaction) -> None:
        session = await self._session(interaction)
        if session and session.current:
            await session.player.seek(0); await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.replay")); await self.manager.update_controller(session)

    @app_commands.command(name="autoplay", description="Autoplay recommendations")
    async def autoplay(self, interaction: discord.Interaction, enabled: bool) -> None:
        session = await self._session(interaction)
        if session:
            session.autoplay = enabled
            session.player.autoplay = wavelink.AutoPlayMode.enabled if enabled and not session.queue else wavelink.AutoPlayMode.disabled
            state = self.manager.tr(interaction.user.id, "music.state.on" if enabled else "music.state.off")
            await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.autoplay", state=state)); await self.manager.update_controller(session)

    @app_commands.command(name="jump", description="Jump directly to a queue position")
    async def jump(self, interaction: discord.Interaction, position: app_commands.Range[int, 1, 500]) -> None:
        session = await self._session(interaction)
        if not session: return
        try:
            entry = await self.manager.jump(session, int(position))
        except IndexError:
            await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.remove.invalid"), ephemeral=True); return
        await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.jump", title=safe_title(entry.track.title, 80)))

    @app_commands.command(name="history", description="Recently played tracks")
    async def history(self, interaction: discord.Interaction) -> None:
        session = await self._session(interaction)
        if session:
            await interaction.response.send_message(embed=self.manager.history_embed(session, self.manager.i18n.get_locale(interaction.user.id)), ephemeral=True)

    @app_commands.command(name="dedupe", description="Remove duplicate tracks from the queue")
    async def dedupe(self, interaction: discord.Interaction) -> None:
        session = await self._session(interaction)
        if session:
            removed = self.manager.dedupe_queue(session)
            await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.dedupe", count=removed), ephemeral=True)
            await self.manager.update_controller(session)

    @app_commands.command(name="stats", description="Music session statistics")
    async def stats(self, interaction: discord.Interaction) -> None:
        session = await self._session(interaction)
        if session:
            await interaction.response.send_message(embed=self.manager.stats_embed(session, self.manager.i18n.get_locale(interaction.user.id)), ephemeral=True)

    @app_commands.command(name="filter", description="Audio filter")
    @app_commands.choices(preset=[app_commands.Choice(name="Reset", value="reset"), app_commands.Choice(name="Nightcore", value="nightcore"), app_commands.Choice(name="Vaporwave", value="vaporwave"), app_commands.Choice(name="8D", value="8d")])
    async def filter(self, interaction: discord.Interaction, preset: app_commands.Choice[str]) -> None:
        session = await self._session(interaction)
        if not session: return
        filters = wavelink.Filters()
        if preset.value == "nightcore": filters.timescale.set(pitch=1.2, speed=1.2, rate=1.0)
        elif preset.value == "vaporwave": filters.timescale.set(pitch=0.8, speed=0.85, rate=1.0)
        elif preset.value == "8d": filters.rotation.set(rotation_hz=0.2)
        await session.player.set_filters(filters)
        await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.filter", name=preset.name))


    @playlist_group.command(name="create", description="Create one of your five playlists")
    async def playlist_create(self, interaction: discord.Interaction, name: str) -> None:
        uid = interaction.user.id
        try:
            item = await self.manager.playlists.create(interaction.guild.id, uid, name)
            await interaction.response.send_message(self.manager.tr(uid, "music.playlist.created", name=item["name"]), ephemeral=True)
        except FileExistsError:
            await interaction.response.send_message(self.manager.tr(uid, "music.playlist.exists"), ephemeral=True)
        except OverflowError as exc:
            await interaction.response.send_message(self.manager.tr(uid, "music.playlist.limit", limit=exc.args[0]), ephemeral=True)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)

    @playlist_group.command(name="list", description="Show your playlists on this server")
    async def playlist_list(self, interaction: discord.Interaction) -> None:
        uid = interaction.user.id
        items = await self.manager.playlists.list(interaction.guild.id, uid)
        if not items:
            await interaction.response.send_message(self.manager.tr(uid, "music.playlist.none"), ephemeral=True)
            return
        lines = [f"`{i}.` **{x['name']}** · {x['tracks']}" for i, x in enumerate(items, 1)]
        await interaction.response.send_message(self.manager.tr(uid, "music.playlist.list_title") + "\n" + "\n".join(lines), ephemeral=True)

    @playlist_group.command(name="delete", description="Delete a playlist")
    async def playlist_delete(self, interaction: discord.Interaction, name: str) -> None:
        uid = interaction.user.id
        try:
            ok = await self.manager.playlists.delete(interaction.guild.id, uid, name)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True); return
        await interaction.response.send_message(self.manager.tr(uid, "music.playlist.deleted" if ok else "music.playlist.not_found", name=name), ephemeral=True)

    @playlist_group.command(name="rename", description="Rename a playlist")
    async def playlist_rename(self, interaction: discord.Interaction, name: str, new_name: str) -> None:
        uid = interaction.user.id
        try:
            await self.manager.playlists.rename(interaction.guild.id, uid, name, new_name)
            await interaction.response.send_message(self.manager.tr(uid, "music.playlist.renamed", name=new_name), ephemeral=True)
        except KeyError:
            await interaction.response.send_message(self.manager.tr(uid, "music.playlist.not_found", name=name), ephemeral=True)
        except FileExistsError:
            await interaction.response.send_message(self.manager.tr(uid, "music.playlist.exists"), ephemeral=True)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)

    @playlist_group.command(name="add", description="Add current track or a search/URL to a playlist")
    @app_commands.choices(source=[
        app_commands.Choice(name="YouTube (recommended)", value="auto"),
        app_commands.Choice(name="YouTube", value="youtube"),
    ])
    async def playlist_add(self, interaction: discord.Interaction, name: str, query: str | None = None, source: app_commands.Choice[str] | None = None) -> None:
        uid = interaction.user.id
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            track = None
            if query:
                result = await self.manager.search(query, source.value if source else "auto")
                if result and not isinstance(result, wavelink.Playlist):
                    track = list(result[:1])[0]
                elif isinstance(result, wavelink.Playlist) and result.tracks:
                    track = result.tracks[0]
            else:
                session = self.manager.sessions.get(interaction.guild.id)
                if session and session.current:
                    track = session.current.track
            if track is None:
                await interaction.followup.send(self.manager.tr(uid, "music.search.none_short"), ephemeral=True); return
            pos = await self.manager.playlists.add_track(interaction.guild.id, uid, name, self.manager.saved_track(track))
            await interaction.followup.send(self.manager.tr(uid, "music.playlist.added", title=safe_title(track.title, 80), position=pos, name=name), ephemeral=True)
        except KeyError:
            await interaction.followup.send(self.manager.tr(uid, "music.playlist.not_found", name=name), ephemeral=True)
        except OverflowError as exc:
            await interaction.followup.send(self.manager.tr(uid, "music.playlist.track_limit", limit=exc.args[0]), ephemeral=True)
        except Exception as exc:
            log.exception("playlist add failed")
            await interaction.followup.send(self.manager.tr(uid, "music.play.failed", error=f"{type(exc).__name__}: {exc}"), ephemeral=True)

    @playlist_group.command(name="show", description="Show tracks in a playlist")
    async def playlist_show(self, interaction: discord.Interaction, name: str, page: app_commands.Range[int, 1, 20] = 1) -> None:
        uid = interaction.user.id
        try:
            item = await self.manager.playlists.get(interaction.guild.id, uid, name)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True); return
        if not item:
            await interaction.response.send_message(self.manager.tr(uid, "music.playlist.not_found", name=name), ephemeral=True); return
        tracks = item.get("tracks", [])
        per_page = 10
        pages = max(1, (len(tracks) + per_page - 1) // per_page)
        page = max(1, min(int(page), pages)); start = (page - 1) * per_page
        lines = [f"`{i}.` **{safe_title(t.get('title','?'), 60)}** — {safe_title(t.get('author','?'), 35)} · `{fmt_ms(int(t.get('length_ms',0)))}`" for i, t in enumerate(tracks[start:start+per_page], start+1)]
        text = self.manager.tr(uid, "music.playlist.show_title", name=item.get("name", name), count=len(tracks), page=page, pages=pages)
        await interaction.response.send_message(text + "\n" + ("\n".join(lines) if lines else self.manager.tr(uid, "music.playlist.empty")), ephemeral=True)

    @playlist_group.command(name="remove", description="Remove a track from a playlist")
    async def playlist_remove(self, interaction: discord.Interaction, name: str, position: app_commands.Range[int, 1, 100]) -> None:
        uid = interaction.user.id
        try:
            item = await self.manager.playlists.remove_track(interaction.guild.id, uid, name, int(position))
            await interaction.response.send_message(self.manager.tr(uid, "music.playlist.removed", title=safe_title(item.get("title", "?"), 80)), ephemeral=True)
        except KeyError:
            await interaction.response.send_message(self.manager.tr(uid, "music.playlist.not_found", name=name), ephemeral=True)
        except IndexError:
            await interaction.response.send_message(self.manager.tr(uid, "music.remove.invalid"), ephemeral=True)

    @playlist_group.command(name="clear", description="Clear a playlist")
    async def playlist_clear(self, interaction: discord.Interaction, name: str) -> None:
        uid = interaction.user.id
        try:
            count = await self.manager.playlists.clear(interaction.guild.id, uid, name)
            await interaction.response.send_message(self.manager.tr(uid, "music.playlist.cleared", count=count), ephemeral=True)
        except KeyError:
            await interaction.response.send_message(self.manager.tr(uid, "music.playlist.not_found", name=name), ephemeral=True)

    @playlist_group.command(name="import", description="Import a YouTube playlist into a personal playlist")
    async def playlist_import(self, interaction: discord.Interaction, name: str, url: str) -> None:
        uid = interaction.user.id
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            item = await self.manager.playlists.get(interaction.guild.id, uid, name)
            if not item:
                await interaction.followup.send(self.manager.tr(uid, "music.playlist.not_found", name=name), ephemeral=True); return
            result = await self.manager.search(url)
            tracks = list(result.tracks) if isinstance(result, wavelink.Playlist) else list(result[:1])
            saved = [self.manager.saved_track(t) for t in tracks if self.manager._valid_track(t)]
            added, duplicates = await self.manager.playlists.add_tracks(interaction.guild.id, uid, name, saved)
            await interaction.followup.send(self.manager.tr(uid, "music.playlist.imported", name=name, added=added, duplicates=duplicates), ephemeral=True)
        except Exception as exc:
            log.exception("playlist import failed")
            await interaction.followup.send(self.manager.tr(uid, "music.play.failed", error=f"{type(exc).__name__}: {exc}"), ephemeral=True)

    @playlist_group.command(name="addqueue", description="Save the current track and queue into a playlist")
    async def playlist_addqueue(self, interaction: discord.Interaction, name: str, include_current: bool = True) -> None:
        uid = interaction.user.id
        session = self.manager.sessions.get(interaction.guild.id)
        if not session:
            await interaction.response.send_message(self.manager.tr(uid, "music.nothing_playing"), ephemeral=True); return
        entries = ([] if not include_current or not session.current else [session.current]) + list(session.queue)
        tracks = [self.manager.saved_track(e.track) for e in entries]
        try:
            added, duplicates = await self.manager.playlists.add_tracks(interaction.guild.id, uid, name, tracks)
            await interaction.response.send_message(self.manager.tr(uid, "music.playlist.bulk_added", name=name, added=added, duplicates=duplicates), ephemeral=True)
        except KeyError:
            await interaction.response.send_message(self.manager.tr(uid, "music.playlist.not_found", name=name), ephemeral=True)

    @playlist_group.command(name="move", description="Move a saved track inside a playlist")
    async def playlist_move(self, interaction: discord.Interaction, name: str, source: app_commands.Range[int, 1, 100], target: app_commands.Range[int, 1, 100]) -> None:
        uid = interaction.user.id
        try:
            await self.manager.playlists.move_track(interaction.guild.id, uid, name, int(source), int(target))
            await interaction.response.send_message(self.manager.tr(uid, "music.playlist.moved", source=source, target=target), ephemeral=True)
        except KeyError:
            await interaction.response.send_message(self.manager.tr(uid, "music.playlist.not_found", name=name), ephemeral=True)
        except IndexError:
            await interaction.response.send_message(self.manager.tr(uid, "music.move.invalid"), ephemeral=True)

    @playlist_group.command(name="dedupe", description="Remove duplicate saved tracks")
    async def playlist_dedupe(self, interaction: discord.Interaction, name: str) -> None:
        uid = interaction.user.id
        try:
            removed = await self.manager.playlists.dedupe(interaction.guild.id, uid, name)
            await interaction.response.send_message(self.manager.tr(uid, "music.playlist.deduped", count=removed), ephemeral=True)
        except KeyError:
            await interaction.response.send_message(self.manager.tr(uid, "music.playlist.not_found", name=name), ephemeral=True)

    @playlist_group.command(name="play", description="Queue one of your playlists")
    async def playlist_play(self, interaction: discord.Interaction, name: str, shuffle: bool = False, next_up: bool = False) -> None:
        uid = interaction.user.id
        await interaction.response.defer(ephemeral=True, thinking=True)
        item = await self.manager.playlists.get(interaction.guild.id, uid, name)
        if not item:
            await interaction.followup.send(self.manager.tr(uid, "music.playlist.not_found", name=name), ephemeral=True); return
        stored = list(item.get("tracks", []))
        if shuffle:
            import random as _random
            _random.shuffle(stored)
        if not stored:
            await interaction.followup.send(self.manager.tr(uid, "music.playlist.empty"), ephemeral=True); return
        try:
            session = await self.manager.connect(interaction.user, interaction.channel)
            first, rest = stored[0], stored[1:]
            first_query = first.get("uri") or first.get("query") or f"{first.get('title','')} {first.get('author','')}"
            first_result = await self.manager.search(first_query)
            if not first_result:
                await interaction.edit_original_response(content=self.manager.tr(uid, "music.search.none"))
                return
            first_added, _ = await self.manager.enqueue_search(session, first_result, interaction.user, front=next_up)

            async def preload_rest() -> None:
                # Первый трек запускается сразу. Остаток плейлиста резолвлю параллельно,
                # но добавляю в исходном порядке, чтобы ускорение не меняло очередь.
                limit = max(1, int(self.manager.cfg.get("preload", {}).get("parallelism", 4)))
                semaphore = asyncio.Semaphore(min(limit, 8))
                async def resolve(saved):
                    query = saved.get("uri") or saved.get("query") or f"{saved.get('title','')} {saved.get('author','')}"
                    async with semaphore:
                        try:
                            return await self.manager.search(query)
                        except Exception:
                            log.debug("[Music] playlist preload failed", exc_info=True)
                            return None
                results = await asyncio.gather(*(resolve(saved) for saved in rest))
                for result in results:
                    if result:
                        await self.manager.enqueue_search(session, result, interaction.user, front=False)

            if rest:
                self.plugin.tasks.create(preload_rest(), name=f"music:preload:{interaction.guild.id}:{uid}")
            await interaction.edit_original_response(content=self.manager.tr(
                uid, "music.playlist.loading", name=item.get("name", name), added=first_added, pending=len(rest)
            ))
        except Exception as exc:
            log.exception("playlist play failed")
            await interaction.followup.send(self.manager.tr(uid, "music.play.failed", error=f"{type(exc).__name__}: {exc}"), ephemeral=True)

    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload) -> None:
        log.info("[Music] Node ready %s resumed=%s", payload.node.identifier, payload.resumed)

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload) -> None:
        player = payload.player
        if not player or not player.guild: return
        session = self.manager.sessions.get(player.guild.id)
        if not session: return
        await self.manager.handle_track_start(payload)

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload) -> None:
        await self.manager.handle_track_end(payload)

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload) -> None:
        await self.manager.handle_track_problem(payload, "exception")

    @commands.Cog.listener()
    async def on_wavelink_track_stuck(self, payload: wavelink.TrackStuckEventPayload) -> None:
        await self.manager.handle_track_problem(payload, "stuck")

    @commands.Cog.listener()
    async def on_wavelink_inactive_player(self, player: wavelink.Player) -> None:
        if player.guild:
            log.info("[Music] Inactive timeout guild=%s", player.guild.id)
            await self.manager.stop_guild(player.guild.id, disconnect=True)
