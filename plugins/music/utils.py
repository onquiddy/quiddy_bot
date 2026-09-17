from __future__ import annotations

from urllib.parse import urlparse


def fmt_ms(value: int | None) -> str:
    if value is None or value < 0:
        return "LIVE"
    total = int(value // 1000)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def progress_bar(position: int, length: int, width: int = 22) -> str:
    if length <= 0:
        return "━" * width
    ratio = min(1.0, max(0.0, position / length))
    dot = min(width - 1, int(ratio * (width - 1)))
    return "━" * dot + "●" + "━" * (width - dot - 1)


def source_label(track) -> str:
    source = str(getattr(track, "source", "unknown") or "unknown").lower()
    uri = str(getattr(track, "uri", "") or "")
    host = urlparse(uri).netloc.lower()
    if "youtube" in source or "youtu" in host:
        return "YouTube"
    if source and source != "unknown":
        return source.title()
    return "Lavalink"


def safe_title(text: str, limit: int = 80) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
