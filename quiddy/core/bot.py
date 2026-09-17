from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from .audit import AuditRecord, AuditService
from .events import DiscordReady, EventBus
from .logging import done, loading, system
from .i18n import LanguageCog

log = logging.getLogger("quiddy.discord")


class QuiddyBot(commands.AutoShardedBot):
    def __init__(self, *, config, services, events: EventBus) -> None:
        dcfg = config.section("discord")
        intents_cfg = dcfg.get("intents", {})
        intents = discord.Intents.none()
        for name, enabled in intents_cfg.items():
            if hasattr(intents, name):
                setattr(intents, name, bool(enabled))

        mentions_cfg = dcfg.get("allowed_mentions", {})
        allowed_mentions = discord.AllowedMentions(
            everyone=bool(mentions_cfg.get("everyone", False)),
            roles=bool(mentions_cfg.get("roles", False)),
            users=bool(mentions_cfg.get("users", True)),
            replied_user=bool(mentions_cfg.get("replied_user", False)),
        )
        super().__init__(
            command_prefix=dcfg.get("command_prefix", "!"),
            intents=intents,
            allowed_mentions=allowed_mentions,
            help_command=None,
            max_messages=int(dcfg.get("max_messages", 1000)),
        )
        self.config = config
        self.services = services
        self.events = events
        self.plugin_manager = None
        self.tree.on_error = self._tree_error

    async def setup_hook(self) -> None:
        if self.get_cog("LanguageCog") is None:
            await self.add_cog(LanguageCog(self.services.get("i18n")))
        if self.plugin_manager:
            loading(log, "Loading enabled plugins…")
            await self.plugin_manager.load_enabled()
        if self.config.get("discord.sync_commands", True):
            loading(log, "Synchronizing application commands…")
            test_guilds = self.config.get("discord.test_guild_ids", []) or []
            synced = 0
            if test_guilds:
                for guild_id in test_guilds:
                    guild = discord.Object(id=int(guild_id))
                    self.tree.copy_global_to(guild=guild)
                    synced += len(await self.tree.sync(guild=guild))
            else:
                synced = len(await self.tree.sync())
            done(log, "Application commands synchronized (%d)", synced)

    async def on_ready(self) -> None:
        users = sum(g.member_count or 0 for g in self.guilds)
        done(log, "Connected as %s (%s)", self.user, self.user.id if self.user else None)
        system(log, "Guilds=%d • users≈%d • shards=%s • latency=%.1fms", len(self.guilds), users, self.shard_count, self.latency * 1000)
        await self.events.publish(DiscordReady(guild_count=len(self.guilds), user_count=users))
        if "console" in self.services.names():
            self.services.get("console").mark_ready()

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type is discord.InteractionType.application_command:
            name = interaction.data.get("name", "unknown") if interaction.data else "unknown"
            log.info("%s (%s) used /%s in guild=%s", interaction.user, interaction.user.id, name, interaction.guild_id)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("Joined guild %s (%s)", guild.name, guild.id)
        try:
            await self.services.get("repository").upsert_guild(guild)
        except Exception:
            log.exception("Failed to persist guild join %s", guild.id)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        log.info("Left guild %s (%s)", guild.name, guild.id)
        try:
            await self.services.get("repository").mark_guild_left(guild.id)
        except Exception:
            log.exception("Failed to persist guild removal %s", guild.id)

    async def on_member_join(self, member: discord.Member) -> None:
        try:
            await self.services.get("repository").upsert_member(member)
        except Exception:
            log.exception("Failed to persist member join %s/%s", member.guild.id, member.id)

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.nick != after.nick or before.global_name != after.global_name or before.avatar != after.avatar:
            try:
                await self.services.get("repository").upsert_member(after)
            except Exception:
                log.exception("Failed to persist member update %s/%s", after.guild.id, after.id)

    async def on_member_remove(self, member: discord.Member) -> None:
        try:
            await self.services.get("repository").mark_member_left(member)
        except Exception:
            log.exception("Failed to persist member removal %s/%s", member.guild.id, member.id)

    async def on_error(self, event_method: str, *args, **kwargs) -> None:
        log.exception("Unhandled Discord event error: %s", event_method)

    async def _tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        log.error(
            "Application command failed: %s",
            getattr(interaction.command, "qualified_name", "unknown"),
            exc_info=(type(error), error, error.__traceback__),
        )
        try:
            audit: AuditService = self.services.get("audit")
            await audit.write(AuditRecord(
                action="command.error",
                guild_id=interaction.guild_id,
                actor_type="discord_user",
                actor_id=str(interaction.user.id),
                entity_type="command",
                entity_id=getattr(interaction.command, "qualified_name", "unknown"),
                metadata={"error": type(error).__name__},
            ))
        except Exception:
            log.exception("Failed to audit command error")
        message = "⚠️ Команда завершилась с ошибкой. Инцидент уже записан в журнал."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass
