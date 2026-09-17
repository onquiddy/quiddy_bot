# Changelog

## 1.6.3 — GitHub Ready

- Cleaned Windows shutdown path for embedded Lavalink.
- Lavalink process output now uses a runtime log file instead of an asyncio stdout pipe.
- Finished Russian console/shutdown wording and removed a leftover Ukrainian error message.
- Refreshed project metadata and README for public Git hosting.
- Kept deployment configuration out of the update package.

## 1.6.2 — Clean Shutdown

- Added bounded graceful shutdown for embedded Lavalink with Windows process-tree fallback.
- Prevented the console prompt from being redrawn after shutdown starts.

## 1.6.1 — Pulse & Support Hotfix

- Fixed async plugin setup contracts for Leveling and Tickets.
- Added regression coverage for plugin entry points.

## 1.6.0 — Pulse & Support

- Added Leveling and Tickets plugins.
- Expanded moderation and activity systems.
