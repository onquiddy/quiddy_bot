from __future__ import annotations

import discord


class VolumeModal(discord.ui.Modal, title="Music volume"):
    volume = discord.ui.TextInput(label="Volume (0-150)", placeholder="70", min_length=1, max_length=3)

    def __init__(self, manager, session) -> None:
        super().__init__(timeout=60)
        self.manager = manager
        self.session = session
        self.volume.default = str(session.volume)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            value = int(str(self.volume.value).strip())
        except ValueError:
            await interaction.response.send_message("⚠️ Введи число.", ephemeral=True)
            return
        maximum = int(self.manager.cfg.get("player", {}).get("max_volume", 150))
        if not 0 <= value <= maximum:
            await interaction.response.send_message(
                self.manager.tr(interaction.user.id, "music.volume.max", maximum=maximum), ephemeral=True
            )
            return
        self.session.volume = value
        self.session.controller_revision += 1
        await self.session.player.set_volume(value)
        await interaction.response.send_message(
            self.manager.tr(interaction.user.id, "music.volume.changed", volume=value), ephemeral=True
        )
        await self.manager.update_controller(self.session)


class MusicControls(discord.ui.View):
    """Persistent controller. One message per guild/session, state rendered dynamically."""

    def __init__(self, manager, session=None) -> None:
        super().__init__(timeout=None)
        self.manager = manager
        self.session = session
        if session is not None:
            for child in self.children:
                cid = getattr(child, "custom_id", "")
                if cid == "quiddy:music:pause":
                    child.style = discord.ButtonStyle.success if getattr(session.player, "paused", False) else discord.ButtonStyle.secondary
                elif cid == "quiddy:music:loop" and getattr(session.loop_mode, "value", "off") != "off":
                    child.style = discord.ButtonStyle.primary
                elif cid == "quiddy:music:autoplay" and session.autoplay:
                    child.style = discord.ButtonStyle.success

    async def _guard(self, interaction: discord.Interaction):
        uid = interaction.user.id
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(self.manager.tr(uid, "music.button.server_only"), ephemeral=True)
            return None
        try:
            return await self.manager.require_controller(interaction)
        except RuntimeError as exc:
            stale = bool(interaction.guild and interaction.guild.id not in self.manager.sessions)
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            if stale and interaction.message:
                try:
                    await interaction.message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
            return None

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary, custom_id="quiddy:music:previous", row=0)
    async def previous(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        session = await self._guard(interaction)
        if not session:
            return
        try:
            entry = await self.manager.previous(session)
        except RuntimeError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            self.manager.tr(interaction.user.id, "music.previous", title=entry.track.title[:80]), ephemeral=True
        )
        await self.manager.update_controller(session)

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.secondary, custom_id="quiddy:music:pause", row=0)
    async def pause_resume(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        session = await self._guard(interaction)
        if not session:
            return
        await session.player.pause(not session.player.paused)
        session.controller_revision += 1
        await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.button.done"), ephemeral=True)
        await self.manager.update_controller(session)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.primary, custom_id="quiddy:music:skip", row=0)
    async def skip(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        session = await self._guard(interaction)
        if not session:
            return
        await self.manager.skip(session)
        await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.button.skip"), ephemeral=True)

    @discord.ui.button(emoji="↩️", style=discord.ButtonStyle.secondary, custom_id="quiddy:music:replay", row=0)
    async def replay(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        session = await self._guard(interaction)
        if not session or not session.current:
            return
        await session.player.seek(0)
        await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.replay"), ephemeral=True)
        await self.manager.update_controller(session)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="quiddy:music:stop", row=0)
    async def stop(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        session = await self._guard(interaction)
        if not session:
            return
        if not await self.manager.can_destructive(interaction.user):
            await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.dj_required"), ephemeral=True)
            return
        # Acknowledge before deletion so Discord does not race a deleted source message.
        await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.button.stop"), ephemeral=True)
        await self.manager.stop(interaction.guild.id, disconnect=True)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="quiddy:music:loop", row=1)
    async def loop(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        session = await self._guard(interaction)
        if not session:
            return
        mode = self.manager.cycle_loop(session)
        session.controller_revision += 1
        label = self.manager.tr(interaction.user.id, f"music.loop.{mode.value}")
        await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.button.loop", mode=label), ephemeral=True)
        await self.manager.update_controller(session)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="quiddy:music:shuffle", row=1)
    async def shuffle(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        session = await self._guard(interaction)
        if not session:
            return
        count = self.manager.shuffle(session)
        session.controller_revision += 1
        await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.button.shuffle", count=count), ephemeral=True)
        await self.manager.update_controller(session)

    @discord.ui.button(emoji="✨", style=discord.ButtonStyle.secondary, custom_id="quiddy:music:autoplay", row=1)
    async def autoplay(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        session = await self._guard(interaction)
        if not session:
            return
        session.autoplay = not session.autoplay
        session.controller_revision += 1
        import wavelink
        session.player.autoplay = wavelink.AutoPlayMode.enabled if session.autoplay and not session.queue else wavelink.AutoPlayMode.disabled
        state = self.manager.tr(interaction.user.id, "music.state.on" if session.autoplay else "music.state.off")
        await interaction.response.send_message(self.manager.tr(interaction.user.id, "music.autoplay", state=state), ephemeral=True)
        await self.manager.update_controller(session)

    @discord.ui.button(emoji="📜", style=discord.ButtonStyle.secondary, custom_id="quiddy:music:queue", row=1)
    async def queue(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        session = await self._guard(interaction)
        if not session:
            return
        await interaction.response.send_message(
            embed=self.manager.queue_embed(session, 1, self.manager.i18n.get_locale(interaction.user.id)), ephemeral=True
        )

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary, custom_id="quiddy:music:volume", row=1)
    async def volume(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        session = await self._guard(interaction)
        if not session:
            return
        await interaction.response.send_modal(VolumeModal(self.manager, session))


class SearchResultsView(discord.ui.View):
    """Ephemeral search picker bound to the requester who opened it."""

    def __init__(self, manager, requester_id: int, tracks: list, *, timeout: float = 60.0) -> None:
        super().__init__(timeout=timeout)
        self.manager = manager
        self.requester_id = requester_id
        self.tracks = tracks[:10]
        options = []
        for index, track in enumerate(self.tracks):
            title = str(getattr(track, "title", "Unknown"))[:100]
            author = str(getattr(track, "author", "Unknown"))[:100]
            options.append(discord.SelectOption(label=title, description=author, value=str(index), emoji="🎵"))
        self.picker = discord.ui.Select(
            placeholder="Выбери трек / Обери трек / Choose a track",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="quiddy:music:search-pick",
        )
        self.picker.callback = self._picked
        self.add_item(self.picker)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("⚠️ Это меню поиска открыто другим пользователем.", ephemeral=True)
            return False
        return True

    async def _picked(self, interaction: discord.Interaction) -> None:
        try:
            index = int(self.picker.values[0])
            track = self.tracks[index]
            if not interaction.guild or not isinstance(interaction.user, discord.Member):
                await interaction.response.send_message("⚠️ Server only.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            session = await self.manager.connect(interaction.user, interaction.channel)
            added, _ = await self.manager.enqueue_search(session, [track], interaction.user)
            if not added:
                await interaction.followup.send(self.manager.tr(interaction.user.id, "music.queue.rejected"), ephemeral=True)
                return
            from .utils import fmt_ms, safe_title
            await interaction.followup.send(
                self.manager.tr(
                    interaction.user.id,
                    "music.queue.track_added",
                    title=safe_title(track.title, 90),
                    uri=track.uri,
                    duration=fmt_ms(track.length),
                ),
                ephemeral=True,
            )
            self.stop()
        except Exception as exc:
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ {type(exc).__name__}: {exc}", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ {type(exc).__name__}: {exc}", ephemeral=True)
