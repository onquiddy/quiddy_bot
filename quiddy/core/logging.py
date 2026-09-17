from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.logging import RichHandler
from rich.text import Text
from rich.panel import Panel
from rich.table import Table

LOADING = 25
DONE = 26
SYSTEM = 24
logging.addLevelName(LOADING, "LOAD")
logging.addLevelName(DONE, "DONE")
logging.addLevelName(SYSTEM, "SYSTEM")

_COLOR_CODE_RE = re.compile(r"&[0-9a-fArR]")


def loading(logger: logging.Logger, message: str, *args: Any, **kwargs: Any) -> None:
    logger.log(LOADING, message, *args, **kwargs)


def done(logger: logging.Logger, message: str, *args: Any, **kwargs: Any) -> None:
    logger.log(DONE, message, *args, **kwargs)


def system(logger: logging.Logger, message: str, *args: Any, **kwargs: Any) -> None:
    logger.log(SYSTEM, message, *args, **kwargs)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("plugin", "guild_id", "user_id", "event", "request_id"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = "".join(traceback.format_exception(*record.exc_info))
        return json.dumps(payload, ensure_ascii=False)




class _NoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage().lower()
        noisy = (
            "logging in using static token",
            "privileged message content intent is missing",
            "experimental request caching has been toggled on",
        )
        return not any(x in msg for x in noisy)


class PlainFileFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        return _COLOR_CODE_RE.sub("", message)


class QuiddyRichHandler(RichHandler):
    LEVEL_STYLES = {
        "DEBUG": "dim cyan",
        "INFO": "white",
        "SYSTEM": "bright_cyan",
        "LOAD": "yellow",
        "DONE": "bright_green",
        "WARNING": "bright_yellow",
        "ERROR": "bright_red",
        "CRITICAL": "bold white on red",
    }

    def get_level_text(self, record: logging.LogRecord) -> Text:
        level = record.levelname
        label = f"{level:^8}"
        return Text.styled(label, self.LEVEL_STYLES.get(level, "white"))

    def render_message(self, record: logging.LogRecord, message: str) -> Text:
        # Keep messages readable and intentionally do not interpret arbitrary Rich markup.
        component = _COMPONENTS.get(record.name)
        message = _human_message(message)
        out = Text()
        if component:
            out.append(f"{component:<11}", style="bold #ffb000")
            out.append(" │ ", style="dim")
        out.append(message)
        return out


console = Console(highlight=False, soft_wrap=False)

_COMPONENTS = {
    "quiddy.bootstrap": "Ядро", "quiddy.discord": "Discord", "quiddy.plugins": "Модули",
    "quiddy.console": "Консоль", "quiddy.music": "Музыка", "quiddy.music.runtime": "Музыка",
    "quiddy.community": "Сообщество", "quiddy.moderation": "Модерация", "quiddy.audit": "Аудит",
    "quiddy.api": "API", "quiddy.repository": "Хранилище",
}

_RU_REPLACEMENTS = (
    ("Starting Quiddy Core", "Запускаю ядро Quiddy"), ("Starting service ", "Запускаю сервис "),
    ("Service ", "Сервис "), (" ready", " готов"), ("Loading enabled plugins", "Загружаю включённые модули"),
    ("Loading plugin ", "Загружаю модуль "), ("Plugin ", "Модуль "), (" loaded", " загружен"),
    ("Synchronizing application commands", "Синхронизирую slash-команды"),
    ("Application commands synchronized", "Slash-команды синхронизированы"),
    ("Connected as ", "Discord подключён: "), ("Starting embedded node", "Запускаю локальный аудио-узел"),
    ("Connecting Wavelink to ", "Подключаю музыкальный клиент к "), ("Node ready", "Аудио-узел подключён"),
    ("Stopped guild=", "Музыка остановлена • сервер="), ("Connected guild=", "Голосовой канал подключён • сервер="),
)

def _human_message(message: str) -> str:
    for a,b in _RU_REPLACEMENTS:
        message = message.replace(a,b)
    return message



def configure_logging(
    level: str = "INFO",
    json_logs: bool = False,
    *,
    root_dir: Path | None = None,
    save_files: bool = True,
    max_file_mb: int = 10,
    backups: int = 7,
) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    if json_logs:
        stream: logging.Handler = logging.StreamHandler(sys.stdout)
        stream.setFormatter(JsonFormatter())
    else:
        stream = QuiddyRichHandler(
            console=console,
            rich_tracebacks=True,
            show_time=True,
            omit_repeated_times=False,
            show_level=True,
            show_path=False,
            markup=False,
            log_time_format="[%H:%M:%S]",
        )
        stream.setFormatter(logging.Formatter("%(message)s"))
    stream.addFilter(_NoiseFilter())
    root.addHandler(stream)

    if save_files and root_dir is not None:
        logs_dir = root_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            logs_dir / "quiddy.log",
            maxBytes=max(1, max_file_mb) * 1024 * 1024,
            backupCount=max(1, backups),
            encoding="utf-8",
        )
        file_handler.setFormatter(PlainFileFormatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"))
        root.addHandler(file_handler)

    logging.getLogger("discord").setLevel(logging.INFO)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def print_banner(version: str, environment: str) -> None:
    title = Text()
    title.append("QUIDDY", style="bold #ffb000")
    title.append("  /  DISCORD PLATFORM", style="bold bright_cyan")
    subtitle = Text(f"v{version}  •  {environment}  •  QuiddyNetwork", style="dim")
    console.print()
    console.print(Panel.fit(Text.assemble(title, "\n", subtitle), border_style="#ffb000", padding=(0, 2)))
