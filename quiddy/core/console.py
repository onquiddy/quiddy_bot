from __future__ import annotations

import asyncio
import inspect
import logging
import os
import platform
import shlex
import sys
import time
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aioconsole
from rich.table import Table

from quiddy import __version__
from .logging import console, done, loading

log = logging.getLogger("quiddy.console")
ConsoleCallback = Callable[[list[str]], Awaitable[None] | None]


@dataclass(slots=True)
class ConsoleCommand:
    name: str
    callback: ConsoleCallback
    description: str
    usage: str = ""
    owner: str = "core"
    aliases: tuple[str, ...] = ()


class ConsoleService:
    def __init__(self, app: Any, cfg: dict[str, Any]) -> None:
        self.app = app
        self.enabled = bool(cfg.get("enabled", True))
        self.prompt = str(cfg.get("prompt", "quiddy> "))
        self.slow_warning = float(cfg.get("slow_command_warning_seconds", 3))
        self.task_list_limit = max(10, int(cfg.get("task_list_limit", 100)))
        self._commands: dict[str, ConsoleCommand] = {}
        self._aliases: dict[str, str] = {}
        self._task: asyncio.Task | None = None
        self._running = False
        # Отдельный gate надёжнее wait_until_ready(): консоль сама знает, когда startup закончен.
        self._input_ready = asyncio.Event()
        self._register_builtin()

    async def start(self) -> None:
        if not self.enabled:
            return
        self._running = True
        self._task = asyncio.create_task(self._listen(), name="console-listener")
        loading(log, 'Консоль подготовлена • ввод команд откроется после подключения Discord')

    async def stop(self) -> None:
        self._running = False
        if self._task and self._task is not asyncio.current_task():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def health(self) -> dict[str, Any]:
        return {"status": "up" if self.enabled and self._running else "disabled", "commands": len(self._commands)}

    def register(
        self,
        name: str,
        callback: ConsoleCallback,
        description: str,
        *,
        usage: str = "",
        aliases: tuple[str, ...] = (),
        owner: str = "core",
    ) -> None:
        key = name.lower().strip()
        if not key:
            raise ValueError("Console command name cannot be empty")
        if key in self._commands or key in self._aliases:
            raise ValueError(f"Console command already registered: {key}")
        command = ConsoleCommand(key, callback, description, usage, owner, tuple(a.lower() for a in aliases))
        self._commands[key] = command
        for alias in command.aliases:
            if alias in self._commands or alias in self._aliases:
                raise ValueError(f"Console alias already registered: {alias}")
            self._aliases[alias] = key

    def unregister(self, name: str) -> None:
        key = self._aliases.get(name.lower(), name.lower())
        command = self._commands.pop(key, None)
        if command:
            for alias in command.aliases:
                self._aliases.pop(alias, None)

    def unregister_owner(self, owner: str) -> None:
        for name in [n for n, cmd in self._commands.items() if cmd.owner == owner]:
            self.unregister(name)

    async def execute(self, raw: str) -> None:
        try:
            args = shlex.split(raw, posix=os.name != "nt")
        except ValueError as exc:
            log.warning("Cannot parse command: %s", exc)
            return
        if not args:
            return
        name = args.pop(0).lower()
        name = self._aliases.get(name, name)
        command = self._commands.get(name)
        if not command:
            log.warning('Unknown console command "%s". Type "help".', name)
            return
        started = time.perf_counter()
        try:
            result = command.callback(args)
            if inspect.isawaitable(result):
                await result
            elapsed = time.perf_counter() - started
            if elapsed >= self.slow_warning:
                log.warning('Console command "%s" took %.2fs', name, elapsed)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Console command "%s" failed', name)

    async def _listen(self) -> None:
        # stdin открываю только после on_ready. Сам listener при этом живёт с самого старта.
        try:
            await self._input_ready.wait()
        except asyncio.CancelledError:
            return
        if not self._running:
            return
        self._print_ready_console()
        while self._running:
            try:
                raw = await aioconsole.ainput(self.prompt)
            except (EOFError, KeyboardInterrupt):
                await self.app.request_shutdown("console-eof")
                return
            except asyncio.CancelledError:
                return
            await self.execute(raw)
            # После stop/exit новый prompt уже не рисую: shutdown начинается сразу.
            if self.app._shutdown_event.is_set():
                self._running = False
                return


    def mark_ready(self) -> None:
        """Открывает stdin после полного startup. Повторный on_ready ничего не ломает."""
        self._input_ready.set()

    def _print_ready_console(self) -> None:
        bot = self.app.bot
        m = self.app.monitor.snapshot()
        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column(style="dim")
        table.add_column(style="bright_white")
        table.add_row("Discord", f"[green]●[/green] {bot.user} • серверов: {len(bot.guilds)} • {bot.latency*1000:.0f} мс")
        table.add_row("Модули", f"{sum(rt.state.value == 'running' for rt in self.app.plugins.runtimes.values())}/{len(self.app.plugins.runtimes)} запущено")
        table.add_row("Память", f"{m['process_ram_mb']:.0f} МБ Quiddy + {m['children_ram_mb']:.0f} МБ дочерние процессы")
        table.add_row("Консоль", "help — список команд  •  status — состояние  •  stop — остановка")
        from rich.panel import Panel
        console.print(Panel(table, title="[bold #ffb000]✓ Quiddy полностью запущен[/bold #ffb000]", border_style="green"))

    def _register_builtin(self) -> None:
        self.register("help", self._help, "Показать команды консоли", aliases=("?",))
        self.register("status", self._status, "Состояние Quiddy в реальном времени")
        self.register("health", self._health, "Проверить здоровье сервисов")
        self.register("services", self._services, "Показать сервисы ядра")
        self.register("plugins", self._plugins, "Управление модулями", usage="plugins [list|info|load|unload|reload] [name]", aliases=("modules",))
        self.register("tasks", self._tasks, "Показать фоновые задачи")
        self.register("guilds", self._guilds, "Показать подключённые серверы")
        self.register("sync", self._sync, "Синхронизировать slash-команды")
        self.register("api", self._api, "Проверить Internal API")
        self.register("version", self._version, "Версия и окружение", aliases=("ver",))
        self.register("config", self._config, "Живое обновление конфигурации", usage="config [show|get|validate|update] [path]")
        self.register("debug", self._debug, "Состояние debug-режима", usage="debug status")
        self.register("security", self._security, "Проверить настройки безопасности", usage="security")
        self.register("loglevel", self._loglevel, "Изменить уровень логов без перезапуска", usage="loglevel [DEBUG|INFO|WARNING|ERROR]")
        self.register("clear", self._clear, "Очистить консоль", aliases=("cls",))
        self.register("stop", self._stop, "Корректно остановить Quiddy", aliases=("shutdown", "exit"))

    async def _help(self, _args: list[str]) -> None:
        table = Table(title="Quiddy • Команды консоли", header_style="bold #ffb000")
        table.add_column("Команда", style="bright_cyan", no_wrap=True)
        table.add_column("Использование")
        table.add_column("Описание")
        table.add_column("Модуль", style="dim")
        for cmd in sorted(self._commands.values(), key=lambda c: c.name):
            table.add_row(cmd.name, cmd.usage or cmd.name, cmd.description, cmd.owner)
        console.print(table)

    async def _status(self, _args: list[str]) -> None:
        bot = self.app.bot
        api_health = await self.app.api.health()
        table = Table(title="Quiddy Runtime", header_style="bold #ffb000")
        table.add_column("Field", style="bright_cyan")
        table.add_column("Value")
        table.add_row("Version", __version__)
        table.add_row("Environment", str(self.app.config.get("core.environment", "unknown")))
        table.add_row("Discord", "connected" if bot.is_ready() else "connecting/offline")
        table.add_row("Identity", str(bot.user) if bot.user else "—")
        table.add_row("Guilds", str(len(bot.guilds)))
        table.add_row("Latency", f"{bot.latency * 1000:.1f} ms" if bot.is_ready() else "—")
        table.add_row("API", f"{api_health.get('status')} • {api_health.get('latency_ms', '—')} ms")
        table.add_row("Plugins", f"{sum(rt.state.value == 'running' for rt in self.app.plugins.runtimes.values())}/{len(self.app.plugins.runtimes)} running")
        m = self.app.monitor.snapshot()
        table.add_row("CPU / RAM", f"{m['process_cpu_percent']:.1f}% • {m['process_ram_mb']:.1f} MB (+ {m['children_ram_mb']:.1f} MB children)")
        table.add_row("Event loop", f"{m['loop_lag_ms']:.2f} ms lag • {m['tasks']} tasks • {m['threads']} threads")
        console.print(table)

    async def _health(self, _args: list[str]) -> None:
        snapshot = await self.app.services.health_snapshot()
        table = Table(title="Service Health", header_style="bold #ffb000")
        table.add_column("Service", style="bright_cyan")
        table.add_column("Status")
        table.add_column("Details")
        for name, info in snapshot.items():
            status = str(info.get("status", "unknown")) if isinstance(info, dict) else str(info)
            style = "bright_green" if status == "up" else "yellow" if status in {"disabled", "degraded"} else "bright_red"
            details = ", ".join(f"{k}={v}" for k, v in info.items() if k != "status") if isinstance(info, dict) else ""
            table.add_row(name, f"[{style}]{status}[/{style}]", details)
        console.print(table)

    async def _services(self, _args: list[str]) -> None:
        console.print("[bright_cyan]Services:[/bright_cyan] " + ", ".join(self.app.services.names()))

    async def _plugins(self, args: list[str]) -> None:
        manager = self.app.plugins
        action = args[0].lower() if args else "list"
        if action == "list":
            table = Table(title="Quiddy Plugins", header_style="bold #ffb000")
            table.add_column("Plugin", style="bright_cyan")
            table.add_column("Version")
            table.add_column("State")
            table.add_column("Описание")
            for name, rt in sorted(manager.runtimes.items()):
                table.add_row(name, rt.manifest.version, rt.state.value, rt.manifest.description)
            console.print(table)
            return
        if len(args) < 2:
            log.warning("Usage: plugins %s <name>", action)
            return
        name = args[1]
        if action == "info":
            rt = manager.runtimes.get(name)
            if not rt:
                log.warning("Unknown plugin: %s", name)
                return
            console.print({
                "name": name,
                "version": rt.manifest.version,
                "state": rt.state.value,
                "depends": rt.manifest.depends,
                "required_services": rt.manifest.required_services,
                "error": rt.error,
            })
            return
        loading(log, "%s plugin %s…", action.capitalize(), name)
        if action == "load":
            await manager.load(name)
        elif action == "unload":
            await manager.unload(name)
        elif action == "reload":
            await manager.reload(name)
        else:
            log.warning("Unknown plugins action: %s", action)
            return
        done(log, "Plugin %s %sed", name, action)

    async def _tasks(self, _args: list[str]) -> None:
        tasks = sorted(asyncio.all_tasks(), key=lambda t: t.get_name())[: self.task_list_limit]
        table = Table(title=f"Asyncio Tasks ({len(tasks)})", header_style="bold #ffb000")
        table.add_column("Name", style="bright_cyan")
        table.add_column("State")
        for task in tasks:
            state = "done" if task.done() else "cancelling" if task.cancelling() else "running"
            table.add_row(task.get_name(), state)
        console.print(table)

    async def _guilds(self, _args: list[str]) -> None:
        table = Table(title=f"Discord Guilds ({len(self.app.bot.guilds)})", header_style="bold #ffb000")
        table.add_column("ID", style="dim")
        table.add_column("Name", style="bright_cyan")
        table.add_column("Members", justify="right")
        for guild in self.app.bot.guilds:
            table.add_row(str(guild.id), guild.name, str(guild.member_count or 0))
        console.print(table)

    async def _sync(self, _args: list[str]) -> None:
        loading(log, "Synchronizing application commands…")
        synced = await self.app.bot.tree.sync()
        done(log, "Synchronized %d application commands", len(synced))

    async def _api(self, _args: list[str]) -> None:
        health = await self.app.api.health()
        if health.get("status") == "up":
            done(log, "API is up • %s ms • %s", health.get("latency_ms"), health.get("endpoint"))
        else:
            log.error("API is down: %s", health.get("error", "unknown"))

    async def _version(self, _args: list[str]) -> None:
        console.print(
            f"[bold #ffb000]Quiddy Core {__version__}[/bold #ffb000]\n"
            f"Python {platform.python_version()} • {platform.system()} {platform.release()} • PID {os.getpid()}\n"
            f"Executable: {sys.executable}"
        )

    async def _config(self, args: list[str]) -> None:
        action = args[0].lower() if args else "show"
        if action == "show":
            # Секреты сюда специально не попадают: Config.snapshot() содержит только YAML.
            console.print_json(data=self.app.config.snapshot())
            return
        if action == "get":
            if len(args) < 2:
                log.warning("Usage: config get <dotted.path>")
                return
            value = self.app.config.get(args[1], None)
            console.print_json(data={args[1]: value})
            return
        if action == "validate":
            try:
                from .config import Config
                candidate = Config.load(self.app.root)
                # Не печатаю candidate.secrets даже в debug-режиме.
                Config._validate(candidate.snapshot())
                done(log, "Configuration is valid")
            except Exception as exc:
                log.error("Configuration is invalid: %s: %s", type(exc).__name__, exc)
            return
        if action != "update":
            log.warning("Usage: config [show|get|validate|update] [path]")
            return

        old_level = str(self.app.config.get("core.log_level", "INFO"))
        try:
            changed = self.app.config.reload()
        except Exception as exc:
            log.error("Config update rejected; current runtime config kept: %s: %s", type(exc).__name__, exc)
            return

        new_level = str(self.app.config.get("core.log_level", old_level)).upper()
        if new_level != old_level.upper():
            logging.getLogger().setLevel(getattr(logging, new_level, logging.INFO))

        # Консольные мелочи можно менять сразу, без перезапуска listener-а.
        ccfg = self.app.config.section("console")
        self.prompt = str(ccfg.get("prompt", self.prompt))
        self.slow_warning = float(ccfg.get("slow_command_warning_seconds", self.slow_warning))
        self.task_list_limit = max(10, int(ccfg.get("task_list_limit", self.task_list_limit)))

        plugin_changes = await self.app.plugins.apply_config_update()
        # debug.* живёт в основном config.yml, поэтому Community нужно перечитать даже если его plugin config не менялся.
        if any(path.startswith("debug.") for path in changed):
            rt = self.app.plugins.runtimes.get("community")
            if rt and rt.state.value == "running" and "community" not in plugin_changes["reloaded"]:
                await self.app.plugins.reload("community")
                plugin_changes["reloaded"].append("community")

        # После add/remove cog Discord tree меняется локально сразу, а здесь дожимаю серверную регистрацию.
        if self.app.bot.is_ready() and (any(plugin_changes.values()) or any(p.startswith("debug.") for p in changed)):
            try:
                await self.app.bot.tree.sync()
            except Exception:
                log.exception("Config applied, but Discord command sync failed")

        restart_prefixes = ("discord.intents.", "plugins.directory", "http.", "api.", "core.event_", "performance.", "monitoring.")
        restart_required = [path for path in changed if path.startswith(restart_prefixes)]
        done(log, "Live config applied • changed=%d • plugins=%s", len(changed), plugin_changes)
        if changed:
            console.print("[dim]" + "\n".join(f"• {x}" for x in changed[:50]) + "[/dim]")
        if restart_required:
            log.warning("These settings are valid but need a process restart to rebuild their service: %s", ", ".join(restart_required))

    async def _debug(self, _args: list[str]) -> None:
        cfg = self.app.config.section("debug")
        console.print({
            "enabled": bool(cfg.get("enabled", True)),
            "discord_commands": bool(cfg.get("discord_commands", True)),
            "require_manage_guild": bool(cfg.get("require_manage_guild", True)),
        })

    async def _security(self, _args: list[str]) -> None:
        cfg = self.app.config
        table = Table(title="Security Posture", header_style="bold #ffb000")
        table.add_column("Control", style="bright_cyan")
        table.add_column("State")
        table.add_row("API transport", "HTTPS" if cfg.secrets.api_url.startswith("https://") else "[red]INSECURE HTTP[/red]")
        table.add_row("API signing", "HMAC-SHA256 + nonce")
        table.add_row("Mass mentions", "blocked" if not cfg.get("discord.allowed_mentions.everyone", False) else "[red]enabled[/red]")
        table.add_row("Message content intent", "off" if not cfg.get("discord.intents.message_content", False) else "enabled")
        table.add_row("Debug Discord commands", "enabled" if cfg.get("debug.enabled", True) and cfg.get("debug.discord_commands", True) else "off")
        table.add_row("Secrets in YAML", "no — environment only")
        console.print(table)

    async def _loglevel(self, args: list[str]) -> None:
        if not args:
            console.print(f"Root log level: {logging.getLevelName(logging.getLogger().level)}")
            return
        level = args[0].upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            log.warning("Allowed levels: DEBUG INFO WARNING ERROR CRITICAL")
            return
        logging.getLogger().setLevel(getattr(logging, level))
        done(log, "Log level changed to %s for this process", level)

    async def _clear(self, _args: list[str]) -> None:
        console.clear()

    async def _stop(self, _args: list[str]) -> None:
        loading(log, "Graceful shutdown requested from console…")
        await self.app.request_shutdown("console")
