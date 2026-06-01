"""Discord Rich Presence updates from recent Hermes activity."""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from utils import is_truthy_value

logger = logging.getLogger(__name__)

_controller: "PresenceController | None" = None

_PRESENCE_INTERVAL = 30
_SESSION_STATS_TTL = 300
_SESSION_STATS_FAILURE_BACKOFF = 30
_MAX_LABEL_LEN = 128
_PRESENCE_ENABLED = is_truthy_value(
    os.getenv("DISCORD_PRESENCE_ENABLED", "true"),
    default=True,
)


def _truncate(value: str, max_len: int = _MAX_LABEL_LEN) -> str:
    if len(value) <= max_len:
        return value
    return value[: max_len - 1].rstrip() + "…"


def _format_age(now: float, timestamp: float | None) -> str:
    if not timestamp:
        return "unknown"

    seconds = max(0, int(now - timestamp))
    if seconds < 90:
        return "just now"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 36:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


@dataclass(frozen=True)
class SessionStats:
    sessions: int = 0
    messages: int = 0
    open_sessions: int = 0
    source: str = ""
    model: str = ""
    title: str = ""
    last_seen: float | None = None


@dataclass(frozen=True)
class CachedSessionStats:
    stats: SessionStats
    refreshed: bool


class SessionStatsCache:
    """TTL-backed reader for compact signals from Hermes state.db."""

    def __init__(self, ttl_seconds: int = _SESSION_STATS_TTL) -> None:
        self._ttl_seconds = ttl_seconds
        self._stats = SessionStats()
        self._loaded_at = 0.0
        self._retry_after = 0.0

    def get(self, now: float) -> CachedSessionStats:
        if self._loaded_at and now - self._loaded_at < self._ttl_seconds:
            return CachedSessionStats(self._stats, refreshed=False)
        if now < self._retry_after:
            return CachedSessionStats(self._stats, refreshed=False)

        stats = self._read(now)
        if stats is None:
            self._retry_after = now + _SESSION_STATS_FAILURE_BACKOFF
            return CachedSessionStats(self._stats, refreshed=False)

        self._stats = stats
        self._loaded_at = now
        self._retry_after = 0.0
        return CachedSessionStats(self._stats, refreshed=True)

    def _read(self, now: float) -> SessionStats | None:
        try:
            from hermes_constants import get_hermes_home

            db_path = get_hermes_home() / "state.db"
            today_start = datetime.datetime.fromtimestamp(now).astimezone().replace(
                hour=0, minute=0, second=0, microsecond=0
            ).timestamp()

            conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=2)
            conn.row_factory = sqlite3.Row
            try:
                totals = conn.execute(
                    """
                    SELECT
                        COUNT(*) AS sessions,
                        COALESCE(SUM(message_count), 0) AS messages,
                        COALESCE(SUM(CASE WHEN ended_at IS NULL THEN 1 ELSE 0 END), 0)
                            AS open_sessions
                    FROM sessions
                    WHERE started_at >= ?
                    """,
                    (today_start,),
                ).fetchone()

                latest = conn.execute(
                    """
                    SELECT source, model, title, started_at, ended_at, message_count
                    FROM sessions
                    ORDER BY COALESCE(ended_at, started_at) DESC, started_at DESC
                    LIMIT 1
                    """
                ).fetchone()
            finally:
                conn.close()

            return SessionStats(
                sessions=int(totals["sessions"] or 0) if totals else 0,
                messages=int(totals["messages"] or 0) if totals else 0,
                open_sessions=int(totals["open_sessions"] or 0) if totals else 0,
                source=latest["source"] if latest else "",
                model=latest["model"] if latest else "",
                title=latest["title"] if latest else "",
                last_seen=(
                    float(latest["ended_at"] or latest["started_at"])
                    if latest and (latest["ended_at"] or latest["started_at"])
                    else None
                ),
            )
        except (ImportError, OSError, sqlite3.Error, TypeError, ValueError):
            logger.debug("discord-rich-presence: failed to read session stats", exc_info=True)
            return None

_MODES = ["today", "open", "latest", "model", "activity"]


class PresenceController:
    """Owns Rich Presence state for one Discord adapter instance."""

    def __init__(
        self,
        adapter: Any,
        platform: Any,
        *,
        interval: int = _PRESENCE_INTERVAL,
        stats_cache: SessionStatsCache | None = None,
    ) -> None:
        self._adapter = adapter
        self._platform = platform
        self._interval = interval
        self._stats_cache = stats_cache or SessionStatsCache()
        self._task: asyncio.Task | None = None
        self._message_delta = 0
        self._last_msg_time = 0.0

    @property
    def adapter(self) -> Any:
        return self._adapter

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stats_cache.get(time.time())
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._presence_loop())

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    def track_event(self, event: Any, now: float | None = None) -> None:
        if not _is_platform_event(event, self._platform):
            return
        self._message_delta += 1
        self._last_msg_time = now if now is not None else time.time()

    def build_label(self, mode: str, now: float) -> str:
        cached = self._stats_cache.get(now)
        if cached.refreshed:
            self._message_delta = 0
        stats = cached.stats

        if mode == "today":
            messages = stats.messages + self._message_delta
            return f"Today: {stats.sessions} sessions / {messages} messages"

        if mode == "open":
            source = stats.source or "Hermes"
            noun = "session" if stats.open_sessions == 1 else "sessions"
            return f"Open: {stats.open_sessions} {noun} on {source}"

        if mode == "latest":
            title = stats.title.strip()
            if title:
                return _truncate(f"Latest: {title}")
            return "Latest: untitled session"

        if mode == "model":
            model = stats.model.strip()
            if model:
                return _truncate(f"Model: {model}")
            return "Model: unavailable"

        if mode == "activity":
            if self._last_msg_time:
                return f"Last Discord msg {_format_age(now, self._last_msg_time)}"
            return f"Last session {_format_age(now, stats.last_seen)}"

        return "Hermes status unavailable"

    async def _presence_loop(self) -> None:
        try:
            import discord
        except ImportError:
            logger.debug("discord-rich-presence: discord.py not available")
            return

        if not hasattr(discord, "CustomActivity"):
            logger.debug(
                "discord-rich-presence: discord.CustomActivity not available "
                "(upgrade discord.py to >=2.0)"
            )
            return

        idx = 0
        while True:
            try:
                client = getattr(self._adapter, "_client", None)
                if client is None or not client.is_ready():
                    await asyncio.sleep(60)
                    continue

                label = self.build_label(_MODES[idx % len(_MODES)], time.time())
                await client.change_presence(
                    activity=discord.CustomActivity(name=label)
                )

                idx += 1
                await asyncio.sleep(self._interval)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug("discord-rich-presence: update error", exc_info=True)
                await asyncio.sleep(60)


def _get_controller(gateway: Any) -> PresenceController | None:
    """Return the active controller, starting or replacing it when needed."""
    global _controller

    if not _PRESENCE_ENABLED:
        return None

    try:
        from gateway.config import Platform
    except ImportError as exc:
        logger.warning("discord-rich-presence: cannot import Hermes Platform: %s", exc)
        return None

    adapters = getattr(gateway, "adapters", None)
    if adapters is None:
        logger.warning("discord-rich-presence: gateway has no adapters mapping")
        return None

    adapter = adapters.get(Platform.DISCORD)
    if adapter is None:
        return None

    if _controller is not None and _controller.adapter is not adapter:
        _controller.stop()
        _controller = None

    if _controller is None:
        _controller = PresenceController(adapter, Platform.DISCORD)
        bot_user = getattr(getattr(adapter, "_client", None), "user", None)
        logger.info("discord-rich-presence: activated for %s", bot_user)

    _controller.start()
    return _controller


def _is_platform_event(event: Any, platform: Any) -> bool:
    source = getattr(event, "source", None)
    if source is None:
        return False
    return getattr(source, "platform", None) == platform


def _on_pre_gateway_dispatch(**kwargs: Any) -> None:
    """pre_gateway_dispatch hook: init tracking + count Discord messages."""
    gateway = kwargs.get("gateway")
    event = kwargs.get("event")

    if gateway is None:
        return

    controller = _get_controller(gateway)
    if controller is not None:
        controller.track_event(event)


def register(ctx) -> None:
    """Register the pre_gateway_dispatch hook."""
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
    logger.debug("discord-rich-presence: registered pre_gateway_dispatch hook")
