"""Discord Rich Presence updates from recent Hermes activity."""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from utils import is_truthy_value

logger = logging.getLogger(__name__)

_controller: "PresenceController | None" = None

_DEFAULT_PRESENCE_INTERVAL = 30
_MIN_PRESENCE_INTERVAL = 15
_SESSION_STATS_TTL = 300
_SESSION_STATS_FAILURE_BACKOFF = 30
_MAX_LABEL_LEN = 128
_WINDOW_NAMES = ("day", "week", "month")
_SUMMARY_LABELS = {
    "day": "Today",
    "week": "This week",
    "month": "This month",
}
_WINDOW_LABELS = {
    "day": "today",
    "week": "this week",
    "month": "this month",
}
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


def _format_compact(value: int) -> str:
    number = int(value or 0)
    for threshold, suffix in ((1_000_000, "m"), (1_000, "k")):
        if abs(number) >= threshold:
            compact = f"{number / threshold:.1f}".rstrip("0").rstrip(".")
            return f"{compact}{suffix}"
    return str(number)


def _plural(value: int, singular: str, plural: str | None = None) -> str:
    if value == 1:
        return singular
    return plural or f"{singular}s"


def _parse_interval(value: str | None) -> int:
    if not value:
        return _DEFAULT_PRESENCE_INTERVAL
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_PRESENCE_INTERVAL
    return max(_MIN_PRESENCE_INTERVAL, parsed)


_PRESENCE_INTERVAL = _parse_interval(os.getenv("DISCORD_PRESENCE_INTERVAL"))


@dataclass(frozen=True)
class WindowStats:
    sessions: int = 0
    messages: int = 0
    open_sessions: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


def _empty_windows() -> dict[str, WindowStats]:
    return {name: WindowStats() for name in _WINDOW_NAMES}


@dataclass(frozen=True)
class SessionStats:
    windows: dict[str, WindowStats] = field(default_factory=_empty_windows)
    model: str = ""
    title: str = ""
    last_seen: float | None = None


@dataclass(frozen=True)
class CachedSessionStats:
    stats: SessionStats
    refreshed: bool


def _window_starts(now: float) -> dict[str, float]:
    day_start = datetime.datetime.fromtimestamp(now).astimezone().replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    week_start = day_start - datetime.timedelta(days=day_start.weekday())
    month_start = day_start.replace(day=1)
    return {
        "day": day_start.timestamp(),
        "week": week_start.timestamp(),
        "month": month_start.timestamp(),
    }


def _row_int(row: sqlite3.Row | None, key: str) -> int:
    if row is None:
        return 0
    return int(row[key] or 0)


def _window_stats_from_row(row: sqlite3.Row | None) -> WindowStats:
    return WindowStats(
        sessions=_row_int(row, "sessions"),
        messages=_row_int(row, "messages"),
        open_sessions=_row_int(row, "open_sessions"),
        tool_calls=_row_int(row, "tool_calls"),
        input_tokens=_row_int(row, "input_tokens"),
        output_tokens=_row_int(row, "output_tokens"),
        cache_read_tokens=_row_int(row, "cache_read_tokens"),
        cache_write_tokens=_row_int(row, "cache_write_tokens"),
    )


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
            conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=2)
            conn.row_factory = sqlite3.Row
            try:
                windows = {
                    name: self._read_window(conn, started_at)
                    for name, started_at in _window_starts(now).items()
                }
                latest = conn.execute(
                    """
                    SELECT model, title, started_at, ended_at
                    FROM sessions
                    ORDER BY COALESCE(ended_at, started_at) DESC, started_at DESC
                    LIMIT 1
                    """
                ).fetchone()
            finally:
                conn.close()

            return SessionStats(
                windows=windows,
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

    def _read_window(self, conn: sqlite3.Connection, started_at: float) -> WindowStats:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS sessions,
                COALESCE(SUM(message_count), 0) AS messages,
                COALESCE(SUM(CASE WHEN ended_at IS NULL THEN 1 ELSE 0 END), 0)
                    AS open_sessions,
                COALESCE(SUM(tool_call_count), 0) AS tool_calls,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens
            FROM sessions
            WHERE started_at >= ?
            """,
            (started_at,),
        ).fetchone()
        return _window_stats_from_row(row)


_DEFAULT_MODE_SPECS = (
    "summary:day",
    "summary:week",
    "summary:month",
    "active",
    "latest",
    "model",
    "tools:day",
    "tokens:day",
    "cache:day",
    "activity",
)
_MODE_ALIASES = {
    "today": "summary:day",
    "week": "summary:week",
    "month": "summary:month",
    "summary": "summary:day",
    "tools": "tools:day",
    "tokens": "tokens:day",
    "cache": "cache:day",
}
_FIXED_MODES = {"active", "latest", "model", "activity"}
_WINDOWED_MODE_KINDS = {"summary", "tools", "tokens", "cache"}


def _normalize_mode(value: str) -> str | None:
    mode = value.strip().lower().replace(" ", "")
    if not mode:
        return None

    mode = _MODE_ALIASES.get(mode, mode)
    if mode in _FIXED_MODES:
        return mode

    if ":" not in mode:
        return None
    kind, window = mode.split(":", 1)
    if kind in _WINDOWED_MODE_KINDS and window in _WINDOW_NAMES:
        return f"{kind}:{window}"
    return None


def _parse_modes(value: str | None) -> list[str]:
    raw_modes = value or ",".join(_DEFAULT_MODE_SPECS)
    modes: list[str] = []
    for raw_mode in raw_modes.split(","):
        mode = _normalize_mode(raw_mode)
        if mode is not None and mode not in modes:
            modes.append(mode)
    return modes or list(_DEFAULT_MODE_SPECS)


def _split_mode(mode: str) -> tuple[str, str | None]:
    if ":" not in mode:
        return mode, None
    kind, window = mode.split(":", 1)
    return kind, window


def _window(stats: SessionStats, name: str | None) -> WindowStats:
    return stats.windows.get(name or "day", WindowStats())


def _summary_title(name: str | None) -> str:
    return _SUMMARY_LABELS.get(name or "day", "Today")


def _window_label(name: str | None) -> str:
    return _WINDOW_LABELS.get(name or "day", "today")


def _summary_label(
    stats: SessionStats,
    window_name: str | None,
    message_delta: int,
    _last_msg_time: float,
    _now: float,
) -> str:
    window_name = window_name or "day"
    window = _window(stats, window_name)
    messages = window.messages + (message_delta if window_name == "day" else 0)
    return (
        f"{_summary_title(window_name)}: {_format_compact(window.sessions)} sessions"
        f" · {_format_compact(messages)} msgs"
    )


def _active_label(
    stats: SessionStats,
    _window_name: str | None,
    _message_delta: int,
    _last_msg_time: float,
    _now: float,
) -> str:
    open_sessions = _window(stats, "day").open_sessions
    noun = _plural(open_sessions, "session")
    return f"Active: {_format_compact(open_sessions)} live {noun}"


def _latest_label(
    stats: SessionStats,
    _window_name: str | None,
    _message_delta: int,
    _last_msg_time: float,
    _now: float,
) -> str:
    title = stats.title.strip()
    if title:
        return f"Latest: {title}"
    return "Latest: untitled session"


def _model_label(
    stats: SessionStats,
    _window_name: str | None,
    _message_delta: int,
    _last_msg_time: float,
    _now: float,
) -> str:
    model = stats.model.strip()
    if model:
        return f"Model: {model}"
    return "Model: unavailable"


def _tools_label(
    stats: SessionStats,
    window_name: str | None,
    _message_delta: int,
    _last_msg_time: float,
    _now: float,
) -> str:
    window = _window(stats, window_name)
    noun = _plural(window.tool_calls, "call")
    return (
        f"Tools {_window_label(window_name)}: "
        f"{_format_compact(window.tool_calls)} {noun}"
    )


def _tokens_label(
    stats: SessionStats,
    window_name: str | None,
    _message_delta: int,
    _last_msg_time: float,
    _now: float,
) -> str:
    window = _window(stats, window_name)
    return (
        f"Tokens {_window_label(window_name)}: "
        f"{_format_compact(window.input_tokens)} in"
        f" · {_format_compact(window.output_tokens)} out"
    )


def _cache_label(
    stats: SessionStats,
    window_name: str | None,
    _message_delta: int,
    _last_msg_time: float,
    _now: float,
) -> str:
    window = _window(stats, window_name)
    return (
        f"Cache {_window_label(window_name)}: "
        f"{_format_compact(window.cache_read_tokens)} reused"
    )


def _activity_label(
    stats: SessionStats,
    _window_name: str | None,
    _message_delta: int,
    last_msg_time: float,
    now: float,
) -> str:
    if last_msg_time:
        return f"Last msg {_format_age(now, last_msg_time)}"
    return f"Last session {_format_age(now, stats.last_seen)}"


LabelBuilder = Callable[[SessionStats, str | None, int, float, float], str]

_MODE_BUILDERS: dict[str, LabelBuilder] = {
    "summary": _summary_label,
    "active": _active_label,
    "latest": _latest_label,
    "model": _model_label,
    "tools": _tools_label,
    "tokens": _tokens_label,
    "cache": _cache_label,
    "activity": _activity_label,
}
_MODES = _parse_modes(os.getenv("DISCORD_PRESENCE_MODES"))


class PresenceController:
    """Owns Rich Presence state for one Discord adapter instance."""

    def __init__(
        self,
        adapter: Any,
        platform: Any,
        *,
        interval: int = _PRESENCE_INTERVAL,
        modes: list[str] | None = None,
        stats_cache: SessionStatsCache | None = None,
    ) -> None:
        self._adapter = adapter
        self._platform = platform
        self._interval = interval
        self._modes = modes or list(_MODES)
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

        kind, window = _split_mode(mode)
        builder = _MODE_BUILDERS.get(kind)
        if builder is None:
            return "Hermes status unavailable"

        return _truncate(
            builder(stats, window, self._message_delta, self._last_msg_time, now)
        )

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

                label = self.build_label(
                    self._modes[idx % len(self._modes)],
                    time.time(),
                )
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
