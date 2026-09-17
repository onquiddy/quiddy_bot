from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum

import wavelink


class LoopMode(StrEnum):
    OFF = "off"
    TRACK = "track"
    QUEUE = "queue"


@dataclass(slots=True)
class QueueEntry:
    track: wavelink.Playable
    requester_id: int
    requester_name: str


@dataclass(slots=True)
class GuildMusicSession:
    guild_id: int
    player: wavelink.Player | None = None
    queue: deque[QueueEntry] = field(default_factory=deque)
    history: deque[QueueEntry] = field(default_factory=lambda: deque(maxlen=50))
    current: QueueEntry | None = None
    loop_mode: LoopMode = LoopMode.OFF
    volume: int = 70
    autoplay: bool = False
    home_channel_id: int | None = None
    controller_message_id: int | None = None
    controller_locale: str = "ru"
    suppress_advance: bool = False
    force_next: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    controller_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    controller_revision: int = 0
    controller_rendered_revision: int = -1
    playback_started: bool = False
    loading_track: bool = False
