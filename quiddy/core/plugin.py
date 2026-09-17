from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import ModuleType
from typing import Any

import discord
import yaml
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from quiddy import CORE_API_VERSION
from .events import EventBus, PluginStateChanged
from .exceptions import PluginDependencyError, PluginLoadError
from .services import ServiceContainer
from .logging import done, loading
from .tasks import TaskSupervisor

log = logging.getLogger("quiddy.plugins")


class PluginState(StrEnum):
    DISCOVERED = "discovered"
    LOADING = "loading"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(slots=True, frozen=True)
class PluginManifest:
    name: str
    version: str
    api: str
    entrypoint: str = "plugin.py"
    enabled: bool = True
    depends: tuple[str, ...] = ()
    required_services: tuple[str, ...] = ()
    description: str = ""

    @classmethod
    def load(cls, path: Path) -> "PluginManifest":
        data = yaml.safe_load(path.read_text("utf-8")) or {}
        try:
            return cls(
                name=str(data["name"]),
                version=str(data["version"]),
                api=str(data.get("api", ">=1.0,<2.0")),
                entrypoint=str(data.get("entrypoint", "plugin.py")),
                enabled=bool(data.get("enabled", True)),
                depends=tuple(data.get("depends", []) or []),
                required_services=tuple(data.get("required_services", []) or []),
                description=str(data.get("description", "")),
            )
        except KeyError as exc:
            raise PluginLoadError(f"Manifest {path} missing key: {exc.args[0]}") from exc


@dataclass(slots=True)
class PluginRuntime:
    manifest: PluginManifest
    path: Path
    state: PluginState = PluginState.DISCOVERED
    module: ModuleType | None = None
    instance: "BasePlugin | None" = None
    error: str | None = None
    config_snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PluginContext:
    name: str
    bot: discord.Client
    services: ServiceContainer
    events: EventBus
    config: dict[str, Any]
    root: Path
    logger: logging.Logger


class BasePlugin:
    def __init__(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.tasks = TaskSupervisor(f"plugin:{ctx.name}")
        self._cogs: list[str] = []
        self._subscriptions: list[int] = []
        self._console_commands: list[str] = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def add_cog(self, cog: discord.ext.commands.Cog) -> None:  # type: ignore[attr-defined]
        await self.ctx.bot.add_cog(cog)  # type: ignore[attr-defined]
        self._cogs.append(cog.qualified_name)

    def subscribe(self, event_type: type, handler, *, priority: int = 0) -> int:
        token = self.ctx.events.subscribe(event_type, handler, priority=priority, owner=self.ctx.name)
        self._subscriptions.append(token)
        return token

    def add_console_command(self, name: str, callback, description: str, *, usage: str = "", aliases: tuple[str, ...] = ()) -> None:
        console_service = self.ctx.services.get("console")
        console_service.register(name, callback, description, usage=usage, aliases=aliases, owner=self.ctx.name)
        self._console_commands.append(name)

    async def cleanup(self) -> None:
        await self.tasks.close()
        self.ctx.events.unsubscribe_owner(self.ctx.name)
        if "console" in self.ctx.services.names():
            self.ctx.services.get("console").unregister_owner(self.ctx.name)
        self._console_commands.clear()
        for name in reversed(self._cogs):
            try:
                await self.ctx.bot.remove_cog(name)  # type: ignore[attr-defined]
            except Exception:
                self.ctx.logger.exception("Failed to remove cog %s", name)
        self._cogs.clear()


class PluginManager:
    def __init__(self, *, root: Path, bot: discord.Client, services: ServiceContainer, events: EventBus, config) -> None:
        self.root = root
        self.bot = bot
        self.services = services
        self.events = events
        self.config = config
        self.runtimes: dict[str, PluginRuntime] = {}
        self._lock = asyncio.Lock()

    def discover(self) -> None:
        directory = self.root / self.config.get("plugins.directory", "plugins")
        directory.mkdir(parents=True, exist_ok=True)
        self.runtimes.clear()
        for manifest_path in sorted(directory.glob("*/plugin.yml")):
            manifest = PluginManifest.load(manifest_path)
            if manifest.name in self.runtimes:
                raise PluginLoadError(f"Duplicate plugin name: {manifest.name}")
            Version(manifest.version)
            if Version(CORE_API_VERSION) not in SpecifierSet(manifest.api):
                raise PluginLoadError(f"Plugin {manifest.name} requires core API {manifest.api}; current={CORE_API_VERSION}")
            self.runtimes[manifest.name] = PluginRuntime(manifest, manifest_path.parent)

    def _resolve_order(self, enabled: set[str]) -> list[str]:
        temporary: set[str] = set()
        permanent: set[str] = set()
        order: list[str] = []

        def visit(name: str) -> None:
            if name in permanent:
                return
            if name in temporary:
                raise PluginDependencyError(f"Plugin dependency cycle at {name}")
            runtime = self.runtimes.get(name)
            if not runtime:
                raise PluginDependencyError(f"Missing plugin dependency: {name}")
            temporary.add(name)
            for dep in runtime.manifest.depends:
                if dep not in enabled:
                    raise PluginDependencyError(f"Plugin {name} depends on disabled plugin {dep}")
                visit(dep)
            temporary.remove(name)
            permanent.add(name)
            order.append(name)

        for name in sorted(enabled):
            visit(name)
        return order

    async def load_enabled(self) -> None:
        self.discover()
        configured = set(self.config.get("plugins.enabled", []) or [])
        enabled = {n for n, r in self.runtimes.items() if r.manifest.enabled and (not configured or n in configured)}
        fail_fast = bool(self.config.get("plugins.fail_fast", False))
        for name in self._resolve_order(enabled):
            try:
                await self.load(name)
            except Exception:
                # Plugin failures degrade the platform; they do not take Discord
                # offline unless fail_fast is explicitly requested.
                if fail_fast:
                    raise
                log.error("Plugin %s is FAILED; core startup continues", name)

    async def load(self, name: str) -> None:
        async with self._lock:
            runtime = self.runtimes.get(name)
            if not runtime:
                raise PluginLoadError(f"Unknown plugin: {name}")
            if runtime.state == PluginState.RUNNING:
                return
            for service in runtime.manifest.required_services:
                if service not in self.services.names():
                    raise PluginDependencyError(f"Plugin {name} requires service {service}")
            for dep in runtime.manifest.depends:
                if self.runtimes.get(dep, PluginRuntime(runtime.manifest, runtime.path)).state != PluginState.RUNNING:
                    raise PluginDependencyError(f"Plugin {name} dependency is not running: {dep}")
            await self._state(runtime, PluginState.LOADING)
            loading(log, "Loading plugin %s v%s…", name, runtime.manifest.version)
            package_name = f"quiddy_ext.{name}"
            module_name = f"{package_name}.plugin"
            try:
                # Give every plugin its own import namespace so relative imports work
                # without putting the plugin directory on global sys.path.
                if "quiddy_ext" not in sys.modules:
                    root_pkg = ModuleType("quiddy_ext")
                    root_pkg.__path__ = []  # type: ignore[attr-defined]
                    sys.modules["quiddy_ext"] = root_pkg
                package = ModuleType(package_name)
                package.__path__ = [str(runtime.path)]  # type: ignore[attr-defined]
                package.__package__ = package_name
                sys.modules[package_name] = package

                entry = runtime.path / runtime.manifest.entrypoint
                spec = importlib.util.spec_from_file_location(module_name, entry)
                if not spec or not spec.loader:
                    raise PluginLoadError(f"Cannot create module spec for {entry}")
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                setup = getattr(module, "setup", None)
                if not setup:
                    raise PluginLoadError(f"Plugin {name} has no async setup(ctx)")
                ctx = PluginContext(
                    name=name,
                    bot=self.bot,
                    services=self.services,
                    events=self.events,
                    config=self.config.plugin_config(name),
                    root=runtime.path,
                    logger=logging.getLogger(f"quiddy.plugin.{name}"),
                )
                instance = await setup(ctx)
                if not isinstance(instance, BasePlugin):
                    raise PluginLoadError(f"Plugin {name} setup() must return BasePlugin")
                runtime.module = module
                runtime.instance = instance
                runtime.config_snapshot = self.config.plugin_config(name)
                await asyncio.wait_for(instance.start(), timeout=float(self.config.get("plugins.startup_timeout_seconds", 20)))
                runtime.error = None
                await self._state(runtime, PluginState.RUNNING)
                done(log, "Plugin %s v%s loaded", name, runtime.manifest.version)
            except Exception as exc:
                runtime.error = f"{type(exc).__name__}: {exc}"
                if runtime.instance:
                    # start() may have created subprocesses, sockets or tasks before
                    # failing. Give the plugin its normal stop path first, then run
                    # generic cleanup. This makes plugin loading transactional.
                    try:
                        await asyncio.wait_for(
                            runtime.instance.stop(),
                            timeout=float(self.config.get("plugins.shutdown_timeout_seconds", 10)),
                        )
                    except Exception:
                        log.exception("Plugin %s rollback stop failed", name)
                    try:
                        await runtime.instance.cleanup()
                    except Exception:
                        log.exception("Plugin %s rollback cleanup failed", name)
                runtime.instance = None
                runtime.module = None
                for loaded_name in list(sys.modules):
                    if loaded_name == package_name or loaded_name.startswith(package_name + "."):
                        sys.modules.pop(loaded_name, None)
                await self._state(runtime, PluginState.FAILED)
                log.exception("Plugin %s failed to load", name)
                raise

    async def unload(self, name: str) -> None:
        async with self._lock:
            runtime = self.runtimes.get(name)
            if not runtime or runtime.state not in {PluginState.RUNNING, PluginState.FAILED}:
                return
            dependants = [n for n, r in self.runtimes.items() if name in r.manifest.depends and r.state == PluginState.RUNNING]
            if dependants:
                raise PluginDependencyError(f"Cannot unload {name}; active dependants: {dependants}")
            await self._state(runtime, PluginState.STOPPING)
            try:
                if runtime.instance:
                    try:
                        await asyncio.wait_for(runtime.instance.stop(), timeout=float(self.config.get("plugins.shutdown_timeout_seconds", 10)))
                    finally:
                        await runtime.instance.cleanup()
            finally:
                package_name = f"quiddy_ext.{name}"
                for loaded_name in list(sys.modules):
                    if loaded_name == package_name or loaded_name.startswith(package_name + "."):
                        sys.modules.pop(loaded_name, None)
                runtime.instance = None
                runtime.module = None
                await self._state(runtime, PluginState.STOPPED)
                log.info("Модуль %s остановлен", name)

    async def reload(self, name: str) -> None:
        await self.unload(name)
        await self.load(name)


    async def apply_config_update(self) -> dict[str, list[str]]:
        """Apply plugin enable/disable/config changes without restarting Discord."""
        result: dict[str, list[str]] = {"loaded": [], "unloaded": [], "reloaded": []}
        configured = set(self.config.get("plugins.enabled", []) or [])
        wanted = {n for n, rt in self.runtimes.items() if rt.manifest.enabled and (not configured or n in configured)}

        # Сначала снимаю лишние плагины, чтобы их команды и фоновые задачи не висели до рестарта.
        for name, rt in list(self.runtimes.items()):
            if rt.state == PluginState.RUNNING and name not in wanted:
                await self.unload(name)
                result["unloaded"].append(name)

        for name in self._resolve_order(wanted):
            rt = self.runtimes[name]
            fresh = self.config.plugin_config(name)
            if rt.state != PluginState.RUNNING:
                await self.load(name)
                result["loaded"].append(name)
            elif fresh != rt.config_snapshot:
                await self.reload(name)
                result["reloaded"].append(name)
        return result

    async def stop_all(self) -> None:
        order = [name for name, rt in self.runtimes.items() if rt.state == PluginState.RUNNING]
        # Dependants need to stop before dependencies.
        resolved = self._resolve_order(set(order)) if order else []
        for name in reversed(resolved):
            try:
                await self.unload(name)
            except Exception:
                log.exception("Failed to unload plugin %s", name)

    async def _state(self, runtime: PluginRuntime, new: PluginState) -> None:
        old = runtime.state
        runtime.state = new
        await self.events.publish(PluginStateChanged(plugin=runtime.manifest.name, old_state=str(old), new_state=str(new)))

    async def health(self) -> dict[str, Any]:
        return {
            "status": "up" if all(r.state != PluginState.FAILED for r in self.runtimes.values()) else "degraded",
            "plugins": {name: {"state": rt.state, "version": rt.manifest.version, "error": rt.error} for name, rt in self.runtimes.items()},
        }
