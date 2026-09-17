from __future__ import annotations

import asyncio
import logging
import platform
import signal
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from quiddy import __version__
from .core.api import QuiddyAPIClient
from .core.audit import AuditService
from .core.bot import QuiddyBot
from .core.config import Config
from .core.console import ConsoleService
from .core.events import EventBus
from .core.http import HttpClient
from .core.i18n import I18nService
from .core.logging import configure_logging, done, loading, print_banner, system
from .core.monitoring import RuntimeMonitor
from .core.permissions import PermissionEngine
from .core.plugin import PluginManager
from .core.repository import CoreRepository
from .core.services import ServiceContainer

log = logging.getLogger("quiddy.bootstrap")


class Application:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.config = Config.load(root)
        lcfg = self.config.section("logging")
        configure_logging(
            self.config.get("core.log_level", "INFO"),
            bool(self.config.get("core.json_logs", False)),
            root_dir=root,
            save_files=bool(lcfg.get("save_files", True)),
            max_file_mb=int(lcfg.get("max_file_mb", 10)),
            backups=int(lcfg.get("backups", 7)),
        )
        print_banner(__version__, str(self.config.get("core.environment", "production")))
        system(log, "Python %s • %s %s", platform.python_version(), platform.system(), platform.release())
        system(log, "API endpoint: %s", self.config.secrets.api_url)

        # Blocking file work and small CPU jobs never belong on Discord's event loop.
        workers = max(2, int(self.config.get("performance.worker_threads", min(8, (os.cpu_count() or 2) + 2))))
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="quiddy-worker")

        self.events = EventBus(
            handler_timeout=float(self.config.get("core.event_handler_timeout_seconds", 15)),
            max_concurrency=int(self.config.get("core.event_concurrency", 64)),
        )
        self.services = ServiceContainer()
        self._shutdown_event = asyncio.Event()
        self._shutdown_reason = "unknown"
        self._shutting_down = False

        self.http = HttpClient(self.config.section("http"))
        self.api = QuiddyAPIClient(
            self.http,
            base_url=self.config.secrets.api_url,
            client_id=self.config.secrets.api_client_id,
            secret=self.config.secrets.api_secret,
            cfg=self.config.section("api"),
        )
        self.audit = AuditService(self.api, self.config.section("audit"))
        self.permissions = PermissionEngine(set(map(int, self.config.get("core.owner_ids", []) or [])))
        self.repository = CoreRepository(self.api)
        self.i18n = I18nService(root, self.config.section("i18n"))

        self.services.register("http", self.http)
        self.services.register("api", self.api, dependencies=("http",))
        self.services.register("audit", self.audit, dependencies=("api",))
        self.services.register("permissions", self.permissions)
        self.services.register("repository", self.repository, dependencies=("api",))
        self.services.register("i18n", self.i18n)

        self.monitor = RuntimeMonitor(self, self.config.section("monitoring"))
        self.services.register("monitoring", self.monitor)

        self.bot = QuiddyBot(config=self.config, services=self.services, events=self.events)
        self.plugins = PluginManager(root=root, bot=self.bot, services=self.services, events=self.events, config=self.config)
        self.bot.plugin_manager = self.plugins
        self.services.register("plugins", self.plugins)

        self.console = ConsoleService(self, self.config.section("console"))
        self.services.register("console", self.console, dependencies=("api", "plugins"))

    async def request_shutdown(self, reason: str = "requested") -> None:
        if not self._shutdown_event.is_set():
            self._shutdown_reason = reason
            self._shutdown_event.set()

    async def run(self) -> None:
        self._install_signal_handlers()
        asyncio.get_running_loop().set_default_executor(self.executor)
        loading(log, "Запускаю ядро Quiddy…")
        await self.services.start_all()

        bot_task = asyncio.create_task(self.bot.start(self.config.secrets.discord_token), name="discord-gateway")
        stop_task = asyncio.create_task(self._shutdown_event.wait(), name="shutdown-signal")
        done_set, _ = await asyncio.wait({bot_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)

        if bot_task in done_set:
            exc = bot_task.exception()
            if exc:
                log.error("Discord gateway stopped unexpectedly", exc_info=(type(exc), exc, exc.__traceback__))
                self._shutdown_reason = "discord-gateway-error"
        await self.shutdown()
        if not bot_task.done():
            bot_task.cancel()
            try:
                await bot_task
            except asyncio.CancelledError:
                pass

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda s=sig: self._shutdown_event.set())
            except (NotImplementedError, RuntimeError):
                # Windows Proactor loop does not expose add_signal_handler.
                pass

    async def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        loading(log, "Завершаю работу Quiddy (%s)…", self._shutdown_reason)
        timeout = float(self.config.get("core.shutdown_timeout_seconds", 15))
        try:
            await asyncio.wait_for(self.plugins.stop_all(), timeout=timeout)
        except TimeoutError:
            log.error("Модули не успели завершиться в отведённое время")
        if not self.bot.is_closed():
            await self.bot.close()
        await self.services.stop_all()
        self.executor.shutdown(wait=False, cancel_futures=True)
        done(log, "Quiddy остановлен корректно")
