<div align="center">

# 🦆 Quiddy

### Production-minded Discord platform core for QuiddyNetwork

[![Core](https://img.shields.io/badge/Quiddy_Core-1.6.3-ff9f1c?style=for-the-badge)](#)
[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![discord.py](https://img.shields.io/badge/discord.py-2.7%2B-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://discordpy.readthedocs.io/)
[![Architecture](https://img.shields.io/badge/architecture-plugin--first-111827?style=for-the-badge)](#architecture)
[![License](https://img.shields.io/badge/license-NO_LICENSE-dc2626?style=for-the-badge)](#copyright--usage)

**Plugin-first · API-first · fault-isolated · built for QuiddyNetwork**

`discord.py` · `Wavelink` · `Lavalink` · `aiohttp` · `Rich` · `Pillow`

</div>

---

## ✨ What is Quiddy?

Quiddy is the Discord platform core behind **QuiddyNetwork**. It is designed as a small gateway layer surrounded by isolated feature plugins, supervised background work and a signed Internal API boundary.

The goal is simple: features may be complicated; the core should stay predictable. A broken optional module should not turn into a broken bot.

```text
Discord Gateway
      │
      ▼
┌─────────────────────────────────────────────┐
│                 Quiddy Core                 │
│                                             │
│  ServiceContainer  •  EventBus              │
│  PluginManager     •  TaskSupervisor        │
│  PermissionEngine  •  Audit                 │
│  RuntimeMonitor    •  Rich Console          │
└──────────────┬──────────────────────────────┘
               │ HMAC signed requests
               ▼
        ┌──────────────┐
        │ Internal API │ ───► PostgreSQL / Redis
        └──────────────┘

Plugins
├── 🏠 Community
├── 🛡️ Moderation / AutoMod
├── ⚡ Leveling
├── 🎫 Tickets
├── 🎵 Music ─► Wavelink ─► embedded Lavalink
└── 🩺 Diagnostics
```

## 🚀 Highlights

- **Fault-isolated plugin runtime** — plugin startup failures are contained instead of taking down the whole application.
- **Operational core** — structured logging, runtime monitoring, health information, supervised tasks and controlled shutdown.
- **Moderation & AutoMod** — moderation commands, warnings, anti-spam foundations, raid protection and configurable language policy.
- **Activity leveling** — message, voice and reaction activity with anti-farm logic, streaks, rankings and generated profile cards.
- **Support tickets** — persistent panel, private channels, staff workflow, transcripts, ratings, limits and SLA controls.
- **Music runtime** — Wavelink with locally managed Lavalink, YouTube search/playback flow and persistent controller UI.
- **Community automation** — welcome/farewell, boosts, autoroles, role restore, milestones and presence rotation.
- **Security boundaries** — HMAC-signed Internal API calls, replay protection, bounded concurrency and secrets kept outside source control.

## 🧩 Architecture

```text
quiddy/
├── __main__.py          # application entry point
├── bootstrap.py         # startup / shutdown orchestration
└── core/
    ├── api.py           # signed Internal API client
    ├── audit.py         # audit pipeline
    ├── bot.py           # Discord gateway
    ├── config.py        # layered configuration
    ├── console.py       # operational console
    ├── events.py        # internal event bus
    ├── monitoring.py    # runtime health / metrics
    ├── permissions.py   # permission engine
    ├── plugin.py        # isolated plugin runtime
    ├── resilience.py    # retry / circuit-breaker helpers
    ├── security.py      # signing / security primitives
    ├── services.py      # service container
    └── tasks.py         # supervised background tasks

plugins/
├── community/
├── diagnostics/
├── leveling/
├── moderation/
├── music/
└── tickets/
```

Every plugin exposes an asynchronous setup contract:

```python
async def setup(ctx: PluginContext) -> BasePlugin:
    return MyPlugin(ctx)
```

Plugins own their Discord cogs, listeners, console commands and background tasks and are responsible for releasing them cleanly during shutdown.

## 🖥️ Console

Quiddy includes a small operational console intended for running the bot rather than flooding the terminal with internals.

```text
quiddy> help
quiddy> status
quiddy> monitor
quiddy> plugins
quiddy> health
quiddy> security
quiddy> stop
```

Startup ends with a compact status panel containing the connected bot, guild count, latency, loaded modules and memory usage.

## 🎵 Music

The music plugin manages a local Lavalink runtime and talks to it through Wavelink. On Windows, raw Lavalink output is written to a runtime log instead of an `asyncio.PIPE`, allowing the Java process and its handles to be cleaned up before the event loop closes.

Runtime files, generated credentials and logs are deliberately excluded from Git.

## 🔐 Configuration & secrets

**Deployment configuration is intentionally not part of this repository snapshot.** Local deployments provide their own root and plugin configuration files plus environment secrets.

Never commit Discord tokens, OpenAI API keys, HMAC secrets, browser/YouTube cookies, generated Lavalink credentials, database credentials or production runtime data.

The repository `.gitignore` excludes the common local configuration and runtime paths by default.

## 🧪 Development

```bash
python -m pip install -e ".[dev]"
pytest -q
python -m quiddy
```

Project metadata targets **Python 3.12+**. Development is also tested on modern Python versions, including Python 3.14.

## 🛡️ Security

Do not post live credentials, cookies, private user data or production secrets in public issues. A secret that has appeared in a public commit or log should be treated as compromised and rotated.

See [`SECURITY.md`](SECURITY.md) for the repository security policy.

## 📜 Copyright & usage

**This repository is source-available for viewing only. It is not open source.**

No software license is granted with this repository. Unless you have explicit prior written permission from the copyright holder, you may **not use, copy, modify, redistribute, publish, sublicense, sell, deploy, or create derivative works from this code**, except where applicable law independently permits otherwise.

GitHub functionality and GitHub's Terms of Service may provide platform-level abilities such as viewing or forking a public repository; those platform mechanics do not constitute a software license from the copyright holder.

See [`COPYRIGHT.md`](COPYRIGHT.md).

---

<div align="center">

### QuiddyNetwork

**Build features. Isolate failures. Keep production boring.**

<sub>Copyright © 2026 QuiddyNetwork. All rights reserved.</sub>

</div>
