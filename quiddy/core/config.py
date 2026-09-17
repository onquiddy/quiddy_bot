from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .exceptions import ConfigurationError


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _parse_env_value(raw: str) -> Any:
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw


def _apply_env_overrides(data: dict[str, Any], prefix: str = "QUIDDY__") -> None:
    for env_key, raw in os.environ.items():
        if not env_key.startswith(prefix):
            continue
        path = env_key[len(prefix):].lower().split("__")
        cursor = data
        for part in path[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[path[-1]] = _parse_env_value(raw)


@dataclass(slots=True, frozen=True)
class Secrets:
    discord_token: str
    api_url: str
    api_client_id: str
    api_secret: str


class Config:
    def __init__(self, data: dict[str, Any], secrets: Secrets, root: Path):
        self._data = data
        self.secrets = secrets
        self.root = root

    @classmethod
    def load(cls, root: Path) -> "Config":
        load_dotenv(root / ".env")
        main_path = root / "config.yml"
        if not main_path.exists():
            raise ConfigurationError(f"Missing configuration: {main_path}")

        data = yaml.safe_load(main_path.read_text("utf-8")) or {}
        local = root / "config.local.yml"
        if local.exists():
            data = _deep_merge(data, yaml.safe_load(local.read_text("utf-8")) or {})
        _apply_env_overrides(data)

        token = os.getenv("DISCORD_TOKEN", "").strip()
        api_url = os.getenv("QUIDDY_API_URL", "https://api.quiddy.net").strip().rstrip("/")
        client_id = os.getenv("QUIDDY_API_CLIENT_ID", "").strip()
        api_secret = os.getenv("QUIDDY_API_SECRET", "").strip()
        required = {
            "DISCORD_TOKEN": token,
            "QUIDDY_API_CLIENT_ID": client_id,
            "QUIDDY_API_SECRET": api_secret,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ConfigurationError("Missing required secrets: " + ", ".join(missing))
        if not api_url.startswith("https://") and not bool(data.get("api", {}).get("allow_insecure_http", False)):
            raise ConfigurationError("QUIDDY_API_URL must use https:// (or explicitly enable api.allow_insecure_http)")

        cls._validate(data)
        return cls(data, Secrets(token, api_url, client_id, api_secret), root)

    @staticmethod
    def _validate(data: dict[str, Any]) -> None:
        for section in ("core", "discord", "api", "plugins", "audit", "console", "logging"):
            if section not in data or not isinstance(data[section], dict):
                raise ConfigurationError(f"Missing or invalid config section: {section}")
        directory = data["plugins"].get("directory")
        if not isinstance(directory, str) or not directory.strip():
            raise ConfigurationError("plugins.directory must be a non-empty string")

    def reload(self) -> list[str]:
        """Reload non-secret YAML configuration and return changed dotted keys."""
        main_path = self.root / "config.yml"
        data = yaml.safe_load(main_path.read_text("utf-8")) or {}
        local = self.root / "config.local.yml"
        if local.exists():
            data = _deep_merge(data, yaml.safe_load(local.read_text("utf-8")) or {})
        _apply_env_overrides(data)
        self._validate(data)
        changed = _diff_paths(self._data, data)
        self._data = data
        return changed

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def get(self, path: str, default: Any = None) -> Any:
        cursor: Any = self._data
        for part in path.split("."):
            if not isinstance(cursor, dict) or part not in cursor:
                return default
            cursor = cursor[part]
        return cursor

    def section(self, name: str) -> dict[str, Any]:
        value = self._data.get(name, {})
        return copy.deepcopy(value) if isinstance(value, dict) else {}

    def plugin_config(self, plugin_name: str) -> dict[str, Any]:
        path = self.root / self.get("plugins.directory", "plugins") / plugin_name / "config.yml"
        data = yaml.safe_load(path.read_text("utf-8")) or {} if path.exists() else {}
        prefix = f"QUIDDY_PLUGIN_{plugin_name.upper().replace('-', '_')}__"
        _apply_env_overrides(data, prefix)
        return data


def _diff_paths(old: Any, new: Any, prefix: str = "") -> list[str]:
    if isinstance(old, dict) and isinstance(new, dict):
        out: list[str] = []
        for key in sorted(set(old) | set(new)):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in old or key not in new:
                out.append(path)
            else:
                out.extend(_diff_paths(old[key], new[key], path))
        return out
    return [] if old == new else [prefix or "<root>"]
