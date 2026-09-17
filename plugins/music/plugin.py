from __future__ import annotations

from quiddy.core.plugin import BasePlugin, PluginContext

from .cog import MusicCog
from .manager import MusicManager
from .views import MusicControls


class MusicPlugin(BasePlugin):
    def __init__(self, ctx: PluginContext) -> None:
        super().__init__(ctx)
        self.manager = MusicManager(self)

    async def start(self) -> None:
        await self.manager.start()
        self.ctx.bot.add_view(MusicControls(self.manager))
        interval = max(5, int(self.ctx.config.get("ui", {}).get("controller_refresh_seconds", 10)))
        self.tasks.interval(self.manager.refresh_controllers, interval, name="controller-refresh")
        await self.add_cog(MusicCog(self))
        self.add_console_command(
            "music",
            self.manager.console,
            "Music/Lavalink status and player control",
            usage="music [status|nodes|doctor|logs [n]|cipher [status|logs [n]|restart|update]|restart|players|disconnect <guild_id>]",
        )

    async def stop(self) -> None:
        await self.manager.shutdown()


async def setup(ctx: PluginContext) -> BasePlugin:
    return MusicPlugin(ctx)
