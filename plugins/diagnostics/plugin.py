from __future__ import annotations

import asyncio
import json
import platform
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from quiddy import __version__
from quiddy.core.plugin import BasePlugin, PluginContext


def _duration(seconds: int) -> str:
    d = timedelta(seconds=max(0, seconds))
    days = d.days
    h, rem = divmod(d.seconds, 3600)
    m, s = divmod(rem, 60)
    return (f"{days}d " if days else "") + f"{h:02}:{m:02}:{s:02}"


def _bar(value: float, maximum: float = 100.0, width: int = 10) -> str:
    ratio = max(0.0, min(1.0, value / maximum if maximum else 0.0))
    fill = round(ratio * width)
    return "▰" * fill + "▱" * (width - fill)


class StatusCog(commands.Cog):
    def __init__(self, plugin: "DiagnosticsPlugin") -> None:
        self.plugin = plugin

    @app_commands.command(name="status", description="Live Quiddy status / Состояние Quiddy")
    @app_commands.checks.cooldown(1, 8.0, key=lambda i: (i.guild_id, i.user.id))
    async def status(self, interaction: discord.Interaction) -> None:
        # Сначала отвечаю Discord, а уже потом собираю API/monitoring. Так команда не ощущается тормозной.
        await interaction.response.defer(thinking=True)
        app = self.plugin.app
        bot = self.plugin.ctx.bot
        monitor = app.monitor.snapshot()
        api_task = asyncio.create_task(app.api.health())
        plugins_task = asyncio.create_task(app.plugins.health())
        api, plugins = await asyncio.gather(api_task, plugins_task)

        guilds = len(bot.guilds)
        users = sum(g.member_count or 0 for g in bot.guilds)
        latency = bot.latency * 1000 if bot.is_ready() else 0.0
        running = sum(1 for x in app.plugins.runtimes.values() if x.state.value == "running")
        total_plugins = len(app.plugins.runtimes)
        plugin_names = " • ".join(
            f"{'🟢' if rt.state.value == 'running' else '🔴'} {name} {rt.manifest.version}"
            for name, rt in sorted(app.plugins.runtimes.items())
        ) or "—"

        music_line = "Не загружен"
        music = app.plugins.runtimes.get("music")
        if music and music.instance and hasattr(music.instance, "manager"):
            manager = music.instance.manager
            sessions = len(manager.sessions)
            playing = sum(1 for x in manager.sessions.values() if x.player and getattr(x.player, "playing", False))
            node = manager.node
            music_line = f"Node: {'🟢 online' if node else '🔴 offline'} • sessions {sessions} • playing {playing}"

        api_ms = api.get("latency_ms", "—") if isinstance(api, dict) else "—"
        api_ok = isinstance(api, dict) and api.get("status") == "up"
        status = "Operational"
        if not api_ok or running != total_plugins or monitor.get("loop_lag_ms", 0) >= 150:
            status = "Degraded"

        embed = discord.Embed(
            title="⚡ Quiddy • Live Status",
            description=f"**{status}**  •  live telemetry\n`Core {__version__}`",
            color=0xFFB000 if status == "Operational" else 0xFF7A00,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="🌐 Discord",
            value=(f"**{guilds}** servers • **≈{users}** users\n"
                   f"Gateway **{latency:.0f} ms** • shards **{bot.shard_count or 1}**"),
            inline=True,
        )
        embed.add_field(
            name="🧠 Runtime",
            value=(f"Uptime **{_duration(int(monitor['uptime_seconds']))}**\n"
                   f"Loop lag **{monitor['loop_lag_ms']:.1f} ms** • tasks **{monitor['tasks']}**"),
            inline=True,
        )
        embed.add_field(
            name="🔌 Backend",
            value=f"API {'🟢' if api_ok else '🔴'} **{api_ms} ms**\nPlugins **{running}/{total_plugins}** running",
            inline=True,
        )
        ram_pct = float(monitor["system_ram_percent"])
        embed.add_field(
            name="💾 Memory",
            value=(f"Quiddy **{monitor['process_ram_mb']:.1f} MB** • children **{monitor['children_ram_mb']:.1f} MB**\n"
                   f"`{_bar(ram_pct)}` **{ram_pct:.0f}%** system • {monitor['system_ram_free_mb']:.0f} MB free"),
            inline=False,
        )
        cpu = float(monitor["process_cpu_percent"])
        embed.add_field(
            name="⚙️ Performance",
            value=(f"`{_bar(cpu)}` **{cpu:.1f}%** process CPU\n"
                   f"Threads **{monitor['threads']}** • workers **{app.config.get('performance.worker_threads', 6)}** • CPUs **{monitor['logical_cpus']}**"),
            inline=False,
        )
        embed.add_field(name="🎵 Music", value=music_line, inline=False)
        embed.add_field(name="🧩 Modules", value=plugin_names[:1024], inline=False)
        embed.set_footer(text=f"QuiddyNetwork • Python {platform.python_version()} • live sample")
        await interaction.edit_original_response(embed=embed)


class DiagnosticsPlugin(BasePlugin):
    def __init__(self, ctx: PluginContext) -> None:
        super().__init__(ctx)
        self.app = ctx.services.get("monitoring").app

    async def start(self) -> None:
        await self.add_cog(StatusCog(self))
        self.add_console_command("diag", self.console_diag, "Print compact diagnostics", usage="diag")
        self.add_console_command("monitor", self.console_monitor, "Live runtime telemetry", usage="monitor")

    async def console_diag(self, _args: list[str]) -> None:
        snapshot = await self.ctx.services.health_snapshot()
        self.ctx.logger.info("Diagnostics: %s", json.dumps(snapshot, ensure_ascii=False, default=str))

    async def console_monitor(self, _args: list[str]) -> None:
        m = self.app.monitor.snapshot()
        self.ctx.logger.info(
            "Live • cpu=%.1f%% ram=%.1fMB tree=%.1fMB system=%.1f%% loop=%.2fms tasks=%s threads=%s uptime=%s",
            m["process_cpu_percent"], m["process_ram_mb"], m["total_tree_ram_mb"], m["system_ram_percent"],
            m["loop_lag_ms"], m["tasks"], m["threads"], _duration(m["uptime_seconds"]),
        )


async def setup(ctx: PluginContext) -> BasePlugin:
    return DiagnosticsPlugin(ctx)
