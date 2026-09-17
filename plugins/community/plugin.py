from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from quiddy.core.plugin import BasePlugin, PluginContext
from .store import CommunityStore

log = logging.getLogger("quiddy.community")


def render(text: str, member: discord.Member) -> str:
    guild = member.guild
    values = {
        "mention": member.mention,
        "name": member.display_name,
        "username": member.name,
        "server": guild.name,
        "member_count": guild.member_count or len(guild.members),
        "boost_count": guild.premium_subscription_count or 0,
        "id": member.id,
    }
    try:
        return text.format_map(values)
    except (KeyError, ValueError):
        # Если ошибся в шаблоне, лучше отправить исходный текст, чем уронить весь event handler.
        log.warning("Community template has an unknown/broken placeholder")
        return text


class CommunityDebugCog(commands.GroupCog, group_name="debug", group_description="Quiddy debug tools"):
    community = app_commands.Group(name="community", description="Community diagnostics")

    def __init__(self, plugin: "CommunityPlugin") -> None:
        self.p = plugin

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("⚠️ Debug-команды доступны только на сервере.", ephemeral=True)
            return False
        if self.p.ctx.services.get("permissions").owner_ids and interaction.user.id in self.p.ctx.services.get("permissions").owner_ids:
            return True
        if bool(self.p.root_debug.get("require_manage_guild", True)) and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("⛔ Нужны права «Управлять сервером».", ephemeral=True)
            return False
        return True

    @community.command(name="status", description="Показать состояние Community")
    async def status(self, interaction: discord.Interaction) -> None:
        cfg = self.p.ctx.config
        ar = cfg.get("autoroles", {})
        text = (
            "**Quiddy Community**\n"
            f"Welcome: `{cfg.get('welcome', {}).get('enabled', False)}` • DM: `{cfg.get('welcome', {}).get('dm_enabled', False)}`\n"
            f"Farewell: `{cfg.get('farewell', {}).get('enabled', False)}` • Boost: `{cfg.get('boost', {}).get('enabled', False)}`\n"
            f"Role restore: `{cfg.get('role_restore', {}).get('enabled', False)}` • milestones: `{cfg.get('milestones', {}).get('enabled', False)}`\n"
            f"Autoroles: humans=`{len(ar.get('human_role_ids', []))}` bots=`{len(ar.get('bot_role_ids', []))}`"
        )
        await interaction.response.send_message(text, ephemeral=True)

    @community.command(name="test", description="Отправить тестовое Community-сообщение")
    @app_commands.choices(kind=[
        app_commands.Choice(name="Welcome", value="welcome"),
        app_commands.Choice(name="Farewell", value="farewell"),
        app_commands.Choice(name="Boost", value="boost"),
    ])
    async def test(self, interaction: discord.Interaction, kind: app_commands.Choice[str]) -> None:
        if not bool(self.p.ctx.config.get("debug", {}).get("allow_test_messages", True)):
            await interaction.response.send_message("⛔ Тестовые сообщения выключены в config.yml модуля.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        if kind.value == "boost":
            await self.p.send_boost(interaction.user, force_channel=interaction.channel)
        elif kind.value == "farewell":
            await self.p.send_farewell(interaction.user, force_channel=interaction.channel)
        else:
            await self.p.send_welcome(interaction.user, force_channel=interaction.channel, send_dm=False)
        await interaction.followup.send("✅ Тест отправлен.", ephemeral=True)


class CommunityPlugin(BasePlugin):
    def __init__(self, ctx: PluginContext) -> None:
        super().__init__(ctx)
        self.store = CommunityStore(ctx.root / str(ctx.config.get("storage", "data/community.json")))
        self._presence_index = 0
        self.root_debug: dict = {}

    async def start(self) -> None:
        await self.store.load()
        # debug — глобальный рубильник. Когда он off, debug-команд физически нет в tree.
        app_config = self.ctx.bot.config
        self.root_debug = app_config.section("debug")
        if bool(self.root_debug.get("enabled", True)) and bool(self.root_debug.get("discord_commands", True)):
            await self.add_cog(CommunityDebugCog(self))

        self.ctx.bot.add_listener(self.on_member_join, "on_member_join")
        self.ctx.bot.add_listener(self.on_member_remove, "on_member_remove")
        self.ctx.bot.add_listener(self.on_member_update, "on_member_update")
        self.ctx.bot.add_listener(self.on_ready, "on_ready")
        interval = max(30, int(self.ctx.config.get("presence", {}).get("interval_seconds", 60)))
        self.tasks.interval(self.rotate_presence, interval, name="presence-rotation")
        self.add_console_command("community", self.console, "Community runtime summary", usage="community [status]")

    async def stop(self) -> None:
        for fn, name in [
            (self.on_member_join, "on_member_join"),
            (self.on_member_remove, "on_member_remove"),
            (self.on_member_update, "on_member_update"),
            (self.on_ready, "on_ready"),
        ]:
            self.ctx.bot.remove_listener(fn, name)

    async def console(self, _args: list[str]) -> None:
        snapshots = sum(len(x) for x in self.store.data.get("roles", {}).values())
        log.info("Community: role snapshots=%d • config-only mode", snapshots)

    def channel(self, guild: discord.Guild, channel_id) -> discord.TextChannel | None:
        if channel_id:
            channel = guild.get_channel(int(channel_id))
            if isinstance(channel, discord.TextChannel):
                return channel
        return guild.system_channel

    async def send_welcome(self, member: discord.Member, force_channel=None, send_dm: bool = True) -> None:
        cfg = self.ctx.config.get("welcome", {})
        channel = force_channel or self.channel(member.guild, cfg.get("channel_id"))
        if bool(cfg.get("enabled", True)) and channel:
            text = render(str(cfg.get("message", "Добро пожаловать, {mention}!")), member)
            try:
                if bool(cfg.get("embed", True)):
                    embed = discord.Embed(description=text, color=int(cfg.get("color", 16753920)))
                    embed.set_author(name=f"Добро пожаловать • {member.guild.name}", icon_url=member.display_avatar.url)
                    if bool(cfg.get("show_avatar", True)):
                        embed.set_thumbnail(url=member.display_avatar.url)
                    if bool(cfg.get("show_member_number", True)):
                        embed.set_footer(text=f"ID: {member.id} • участник #{member.guild.member_count or len(member.guild.members)}")
                    await channel.send(embed=embed)
                else:
                    await channel.send(text)
            except discord.HTTPException:
                log.exception("Welcome send failed guild=%s", member.guild.id)
        if send_dm and bool(cfg.get("dm_enabled", True)):
            try:
                await member.send(render(str(cfg.get("dm_message", "Добро пожаловать на {server}!")), member))
            except (discord.Forbidden, discord.HTTPException):
                log.info("Welcome DM unavailable user=%s", member.id)

    async def send_farewell(self, member: discord.Member, force_channel=None) -> None:
        cfg = self.ctx.config.get("farewell", {})
        if not bool(cfg.get("enabled", True)):
            return
        channel = force_channel or self.channel(member.guild, cfg.get("channel_id"))
        if not channel:
            return
        text = render(str(cfg.get("message", "{name} покинул сервер.")), member)
        try:
            if bool(cfg.get("embed", True)):
                await channel.send(embed=discord.Embed(description=text, color=int(cfg.get("color", 8421504))))
            else:
                await channel.send(text)
        except discord.HTTPException:
            log.exception("Farewell send failed guild=%s", member.guild.id)

    async def send_boost(self, member: discord.Member, force_channel=None) -> None:
        cfg = self.ctx.config.get("boost", {})
        if not bool(cfg.get("enabled", True)):
            return
        channel = force_channel or self.channel(member.guild, cfg.get("channel_id"))
        if not channel:
            return
        text = render(str(cfg.get("message", "🚀 {mention} бустит сервер!")), member)
        try:
            if bool(cfg.get("embed", True)):
                await channel.send(embed=discord.Embed(description=text, color=int(cfg.get("color", 16738740))))
            else:
                await channel.send(text)
        except discord.HTTPException:
            log.exception("Boost send failed guild=%s", member.guild.id)

    async def on_member_join(self, member: discord.Member) -> None:
        await self.send_welcome(member)
        ar = self.ctx.config.get("autoroles", {})
        if bool(ar.get("enabled", True)):
            await asyncio.sleep(max(0, min(30, float(ar.get("delay_seconds", 1)))))
            ids = ar.get("bot_role_ids" if member.bot else "human_role_ids", []) or []
            roles = [r for rid in ids if (r := member.guild.get_role(int(rid))) and self._assignable(member.guild, r)]
            if roles:
                try:
                    await member.add_roles(*roles, reason="Quiddy Community autorole")
                except discord.HTTPException:
                    log.exception("Autorole failed guild=%s user=%s", member.guild.id, member.id)

        restore = self.ctx.config.get("role_restore", {})
        if bool(restore.get("enabled", True)):
            ignored = {int(x) for x in restore.get("ignore_role_ids", []) or []}
            limit = max(0, min(100, int(restore.get("max_roles", 25))))
            saved = self.store.get_roles(member.guild.id, member.id)[:limit]
            deny_privileged = bool(self.ctx.config.get("safety", {}).get("exclude_privileged_role_restore", True))
            def safe_restore(role: discord.Role) -> bool:
                if not self._assignable(member.guild, role):
                    return False
                if not deny_privileged:
                    return True
                perms = role.permissions
                return not (perms.administrator or perms.manage_guild or perms.manage_roles or perms.manage_channels or perms.ban_members or perms.kick_members)
            roles = [r for rid in saved if rid not in ignored and (r := member.guild.get_role(int(rid))) and safe_restore(r)]
            if roles:
                try:
                    await member.add_roles(*roles, reason="Quiddy Community role restore")
                except discord.HTTPException:
                    log.exception("Role restore failed guild=%s user=%s", member.guild.id, member.id)

        safety = self.ctx.config.get("safety", {})
        if bool(safety.get("enabled", True)) and not (member.bot and bool(safety.get("ignore_bots", True))):
            days = max(0, int(safety.get("account_age_warning_days", 3)))
            age = datetime.now(timezone.utc) - member.created_at
            if age.days < days:
                channel = self.channel(member.guild, safety.get("mod_channel_id")) if safety.get("mod_channel_id") else None
                if channel:
                    try:
                        await channel.send(f"⚠️ Новый аккаунт: {member.mention} • возраст ≈ {age.days} дн.")
                    except discord.HTTPException:
                        pass

        milestone = self.ctx.config.get("milestones", {})
        every = max(0, int(milestone.get("every", 100)))
        count = member.guild.member_count or len(member.guild.members)
        if bool(milestone.get("enabled", True)) and every and count and count % every == 0:
            channel = self.channel(member.guild, milestone.get("channel_id"))
            if channel:
                try:
                    await channel.send(render(str(milestone.get("message", "🎉 Нас уже **{member_count}**!")), member))
                except discord.HTTPException:
                    pass

    async def on_member_remove(self, member: discord.Member) -> None:
        restore = self.ctx.config.get("role_restore", {})
        if bool(restore.get("enabled", True)):
            ignored = {int(x) for x in restore.get("ignore_role_ids", []) or []}
            max_roles = max(0, min(100, int(restore.get("max_roles", 25))))
            role_ids = [r.id for r in member.roles if not r.is_default() and not r.managed and r.id not in ignored][:max_roles]
            await self.store.set_roles(member.guild.id, member.id, role_ids)
        await self.send_farewell(member)

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.premium_since is None and after.premium_since is not None:
            await self.send_boost(after)

    async def on_ready(self) -> None:
        await self.rotate_presence()

    def _assignable(self, guild: discord.Guild, role: discord.Role) -> bool:
        me = guild.me
        return bool(me and me.guild_permissions.manage_roles and not role.is_default() and not role.managed and role < me.top_role)

    async def rotate_presence(self) -> None:
        cfg = self.ctx.config.get("presence", {})
        if not bool(cfg.get("enabled", True)):
            return
        static = str(cfg.get("static_text") or "").strip()
        statuses = [str(x) for x in cfg.get("statuses", []) if str(x).strip()]
        text = static or (statuses[self._presence_index % len(statuses)] if statuses else "QuiddyNetwork")
        self._presence_index += 1
        users = sum(g.member_count or 0 for g in self.ctx.bot.guilds)
        text = text.format(users=users, guilds=len(self.ctx.bot.guilds))[:128]
        kind = str(cfg.get("type", "watching")).lower()
        activity_type = {
            "playing": discord.ActivityType.playing,
            "listening": discord.ActivityType.listening,
            "watching": discord.ActivityType.watching,
            "competing": discord.ActivityType.competing,
        }.get(kind, discord.ActivityType.watching)
        try:
            await self.ctx.bot.change_presence(activity=discord.Activity(type=activity_type, name=text))
        except discord.HTTPException:
            log.exception("Presence update failed")


async def setup(ctx: PluginContext) -> BasePlugin:
    return CommunityPlugin(ctx)
