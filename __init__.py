"""discord-rich-presence — periodic Discord Rich Presence updates.

Hooks into ``pre_gateway_dispatch`` to:

1. Lazily discover the running Discord adapter on first message
2. Start a background asyncio task that rotates the bot's activity
   status every 30 seconds through current Hermes workload signals:

     Today: N sessions / M messages
     Open: N sessions on <source>
     Latest: <session title>
     Model: <active model>
     Last Discord msg Xm ago

3. Track incoming Discord message volume for the stats

No Hermes internal code is touched — the plugin monkey-patches nothing
and lives entirely in ``~/.hermes/plugins/discord-rich-presence/``.

Toggle with env var ``DISCORD_PRESENCE_ENABLED=false``.
"""

from __future__ import annotations

import logging
import os
import time
import asyncio
from typing import Any

logger = logging.getLogger(__name__)

# ── Internal state (module-level, persists across hook calls) ─────────

_msg_count: int = 0
_last_msg_time: float = 0.0
_adapter: Any = None           # DiscordAdapter instance
_presence_task: Any = None     # asyncio.Task
_initialized: bool = False

_PRESENCE_INTERVAL = 30
_MAX_LABEL_LEN = 128
_PRESENCE_ENABLED = (
    os.getenv("DISCORD_PRESENCE_ENABLED", "true").lower() in {"true", "1", "yes"}
)


# ── Helpers ───────────────────────────────────────────────────────────

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


def _read_session_stats(now: float) -> dict[str, Any]:
    """Read compact session signals from Hermes state.db."""
    try:
        import datetime
        import sqlite3
        from hermes_constants import get_hermes_home

        db_path = get_hermes_home() / "state.db"
        if not db_path.exists():
            return {}

        today_start = datetime.datetime.fromtimestamp(now).astimezone().replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()

        conn = sqlite3.connect(str(db_path), timeout=2)
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

        return {
            "sessions": int(totals["sessions"] or 0) if totals else 0,
            "messages": int(totals["messages"] or 0) if totals else 0,
            "open_sessions": int(totals["open_sessions"] or 0) if totals else 0,
            "source": latest["source"] if latest else "",
            "model": latest["model"] if latest else "",
            "title": latest["title"] if latest else "",
            "last_seen": (
                float(latest["ended_at"] or latest["started_at"])
                if latest and (latest["ended_at"] or latest["started_at"])
                else None
            ),
        }
    except Exception:
        logger.debug("discord-rich-presence: failed to read session stats", exc_info=True)
        return {}


def _build_label(mode: str, now: float) -> str:
    """Construct a presence label for the given mode."""
    stats = _read_session_stats(now)

    if mode == "today":
        sessions = stats.get("sessions", 0)
        messages = max(int(stats.get("messages", 0)), _msg_count)
        return f"Today: {sessions} sessions / {messages} messages"

    if mode == "open":
        open_sessions = stats.get("open_sessions", 0)
        source = stats.get("source") or "Hermes"
        noun = "session" if open_sessions == 1 else "sessions"
        return f"Open: {open_sessions} {noun} on {source}"

    if mode == "latest":
        title = (stats.get("title") or "").strip()
        if title:
            return _truncate(f"Latest: {title}")
        return "Latest: untitled session"

    if mode == "model":
        model = (stats.get("model") or "").strip()
        if model:
            return _truncate(f"Model: {model}")
        return "Model: unavailable"

    if mode == "activity":
        if _last_msg_time:
            return f"Last Discord msg {_format_age(now, _last_msg_time)}"
        return f"Last session {_format_age(now, stats.get('last_seen'))}"

    return "Hermes status unavailable"


# ── Presence loop ─────────────────────────────────────────────────────

_MODES = ["today", "open", "latest", "model", "activity"]


async def _presence_loop() -> None:
    """Background task: rotate the bot's Rich Presence every 30s."""
    global _adapter

    if _adapter is None:
        return

    try:
        import discord
    except ImportError:
        logger.debug("discord-rich-presence: discord.py not available")
        return

    client = getattr(_adapter, "_client", None)
    if client is None:
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
            if not client.is_ready():
                await asyncio.sleep(60)
                continue

            label = _build_label(_MODES[idx % len(_MODES)], time.time())
            await client.change_presence(
                activity=discord.CustomActivity(name=label)
            )

            idx += 1
            await asyncio.sleep(_PRESENCE_INTERVAL)

        except asyncio.CancelledError:
            break
        except Exception:
            logger.debug("discord-rich-presence: update error", exc_info=True)
            await asyncio.sleep(60)


# ── Initialisation ────────────────────────────────────────────────────

def _ensure_initialized(gateway) -> None:
    """Lazily discover the Discord adapter and start the presence loop.

    Safe to call on every message — runs once.
    """
    global _adapter, _presence_task, _initialized

    if _initialized or not _PRESENCE_ENABLED:
        return

    try:
        from gateway.config import Platform

        adapter = gateway.adapters.get(Platform.DISCORD)
        if adapter is None:
            return  # not connected yet — retry on next message

        _adapter = adapter
        _initialized = True

        loop = asyncio.get_running_loop()
        _presence_task = loop.create_task(_presence_loop())

        bot_user = getattr(getattr(adapter, "_client", None), "user", None)
        logger.info("discord-rich-presence: activated for %s", bot_user)

    except Exception as exc:
        logger.debug("discord-rich-presence: init failed: %s", exc)


# ── Hook handler ──────────────────────────────────────────────────────

def _on_pre_gateway_dispatch(**kwargs: Any) -> None:
    """pre_gateway_dispatch hook: init tracking + count Discord messages."""
    global _msg_count, _last_msg_time

    gateway = kwargs.get("gateway")
    event = kwargs.get("event")

    if gateway is None:
        return

    # Lazy init on the first message through the gateway
    _ensure_initialized(gateway)

    # Count incoming Discord messages for presence stats
    if event is not None:
        source = getattr(event, "source", None)
        if source is not None:
            platform = getattr(source, "platform", None)
            if platform is not None and getattr(platform, "value", None) == "discord":
                _msg_count += 1
                _last_msg_time = time.time()


# ── Plugin entry point ────────────────────────────────────────────────

def register(ctx) -> None:
    """Register the pre_gateway_dispatch hook."""
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
    logger.debug("discord-rich-presence: registered pre_gateway_dispatch hook")
