from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("quiddy.i18n")


class _SafeMap(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class I18nService:
    """Tiny UTF-8 .properties based localization service.

    User preferences are intentionally stored locally for now. The public API of
    this service is stable enough to swap the storage backend to Quiddy API later.
    """

    def __init__(self, root: Path, cfg: dict[str, Any] | None = None) -> None:
        self.root = root
        self.cfg = cfg or {}
        self.default_locale = str(self.cfg.get("default_language", "ru")).lower()
        self.allowed = tuple(str(x).lower() for x in self.cfg.get("allowed", ["ru"]))
        self.translation_dir = root / str(self.cfg.get("directory", "translations"))
        self.storage_path = root / str(self.cfg.get("storage", "data/languages.json"))
        self._bundles: dict[str, dict[str, str]] = {}
        self._users: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self.reload()
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        if self.storage_path.exists():
            try:
                raw = json.loads(self.storage_path.read_text("utf-8"))
                if isinstance(raw, dict):
                    self._users = {
                        str(k): str(v).lower()
                        for k, v in raw.items()
                        if str(v).lower() in self.allowed
                    }
            except Exception:
                log.exception("Failed to load language preferences from %s", self.storage_path)

    async def stop(self) -> None:
        return None

    def reload(self) -> None:
        bundles: dict[str, dict[str, str]] = {}
        for locale in self.allowed:
            path = self.translation_dir / f"{locale}.properties"
            if not path.exists():
                log.warning("Missing translation bundle: %s", path)
                bundles[locale] = {}
                continue
            bundles[locale] = self._load_properties(path)
        # Все языки обязаны иметь одинаковый контракт. Иначе одна команда после
        # обновления внезапно говорит на fallback-языке, а другая уже переведена.
        if bundles:
            reference = set(bundles.get(self.default_locale, {}))
            problems: list[str] = []
            placeholder_re = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
            for locale, bundle in bundles.items():
                keys = set(bundle)
                missing = sorted(reference - keys)
                extra = sorted(keys - reference)
                if missing:
                    problems.append(f"{locale}: missing {missing}")
                if extra:
                    problems.append(f"{locale}: extra {extra}")
                for key in reference & keys:
                    expected = set(placeholder_re.findall(bundles[self.default_locale][key]))
                    actual = set(placeholder_re.findall(bundle[key]))
                    if expected != actual:
                        problems.append(f"{locale}:{key}: placeholders {sorted(actual)} != {sorted(expected)}")
            if problems:
                message = "Translation contract mismatch: " + "; ".join(problems[:20])
                if bool(self.cfg.get("strict", True)):
                    raise RuntimeError(message)
                log.error(message)
        self._bundles = bundles

    @staticmethod
    def _load_properties(path: Path) -> dict[str, str]:
        result: dict[str, str] = {}
        for raw in path.read_text("utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("!"):
                continue
            sep = "=" if "=" in line else (":" if ":" in line else None)
            if not sep:
                continue
            key, value = line.split(sep, 1)
            value = (
                value.strip()
                .replace(r"\n", "\n")
                .replace(r"\t", "\t")
                .replace(r"\=", "=")
                .replace(r"\\", "\\")
            )
            result[key.strip()] = value
        return result

    def normalize(self, locale: str | None) -> str:
        value = (locale or self.default_locale).lower()
        return value if value in self.allowed else self.default_locale

    def get_locale(self, user_id: int | str | None) -> str:
        if user_id is None:
            return self.default_locale
        return self.normalize(self._users.get(str(user_id)))

    async def set_locale(self, user_id: int | str, locale: str) -> str:
        normalized = self.normalize(locale)
        async with self._lock:
            self._users[str(user_id)] = normalized
            tmp = self.storage_path.with_suffix(self.storage_path.suffix + ".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._users, ensure_ascii=False, indent=2), "utf-8")
            os.replace(tmp, self.storage_path)
        return normalized

    def t(self, locale: str | None, key: str, **kwargs: Any) -> str:
        lang = self.normalize(locale)
        bundle = self._bundles.get(lang, {})
        fallback = self._bundles.get(self.default_locale, {})
        value = bundle.get(key) or fallback.get(key) or key
        try:
            return value.format_map(_SafeMap(kwargs))
        except Exception:
            log.exception("Failed to format translation key=%s locale=%s", key, lang)
            return value

    def user(self, user_id: int | str | None, key: str, **kwargs: Any) -> str:
        return self.t(self.get_locale(user_id), key, **kwargs)

    async def health(self) -> dict[str, Any]:
        return {
            "status": "up",
            "default": self.default_locale,
            "languages": list(self.allowed),
            "users": len(self._users),
        }


class LanguageCog(commands.Cog):
    """Я оставляю /lang как совместимую команду: интерфейс Quiddy теперь только русский."""
    def __init__(self, i18n: I18nService) -> None:
        self.i18n = i18n

    @app_commands.command(name="lang", description="Язык интерфейса Quiddy")
    async def lang(self, interaction: discord.Interaction) -> None:
        await self.i18n.set_locale(interaction.user.id, "ru")
        await interaction.response.send_message("🇷🇺 Интерфейс Quiddy работает только на русском языке.", ephemeral=True)
