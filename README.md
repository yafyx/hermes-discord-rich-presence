# Hermes Discord Rich Presence

Hermes plugin that sets the Discord bot's Rich Presence from recent Hermes activity in `~/.hermes/state.db`.

It does not patch Hermes core. It registers `pre_gateway_dispatch`, finds the running Discord adapter, and updates the bot status in the background.

## Presence rotation

Examples:

```text
Today: 7 sessions · 484 msgs
This week: 31 sessions · 2.1k msgs
This month: 122 sessions · 8.4k msgs
Active: 2 live sessions
Latest: Rich Presence Plugin Cleanup
Model: deepseek-v4-flash
Tools today: 38 calls
Tokens today: 42k in · 18k out
Cache today: 120k reused
Last msg 4m ago
```

There is no "online" label. Discord already shows that. The status text is for useful runtime context.
The labels omit the platform name because the status is already displayed inside Discord.

## Install

Clone into the Hermes plugin directory:

```bash
git clone https://github.com/yafyx/hermes-discord-rich-presence.git \
  ~/.hermes/plugins/discord-rich-presence
```

Enable the plugin:

```bash
hermes plugins enable discord-rich-presence
hermes gateway restart
```

Hermes loads plugins at process startup, so a gateway restart is required after install or update.

## Configuration

Set these before starting Hermes:

```bash
DISCORD_PRESENCE_ENABLED=false
DISCORD_PRESENCE_INTERVAL=30
DISCORD_PRESENCE_MODES=today,week,month,active,latest,model,tools,tokens,cache,activity
```

`DISCORD_PRESENCE_INTERVAL` has a 15-second minimum. `DISCORD_PRESENCE_MODES`
is a comma-separated rotation list; unknown modes are ignored.

Useful mode presets:

```bash
# minimal
DISCORD_PRESENCE_MODES=today,latest,model,activity

# workload-heavy
DISCORD_PRESENCE_MODES=today,week,month,tools:week,tokens:week,cache:week

# quiet
DISCORD_PRESENCE_MODES=active,latest,activity
```

Supported modes:

```text
today          alias for summary:day
week           alias for summary:week
month          alias for summary:month
tools          alias for tools:day
tokens         alias for tokens:day
cache          alias for cache:day
summary:day    summary:week    summary:month
tools:day      tools:week      tools:month
tokens:day     tokens:week     tokens:month
cache:day      cache:week      cache:month
active         latest          model          activity
```

## Behavior notes

- The presence loop starts after the first inbound Discord gateway message because Hermes currently exposes this plugin through `pre_gateway_dispatch`.
- Startup presence before the first message is not implemented because Hermes does not expose an official Discord-ready plugin hook.
- The plugin avoids monkey-patching the Discord adapter or Hermes runtime internals.
- Stats come from `~/.hermes/state.db`, so today's totals survive plugin restarts.
- Session stats are cached for 5 minutes. Message deltas are counted in memory between cache refreshes.
- If the database is temporarily unavailable, the plugin keeps the last stats and retries after 30 seconds.
- Labels stay under Discord's custom status length.
- The plugin does not show raw message text, prompts, cwd, user IDs, API keys, or filesystem paths.

## Development check

```bash
python -m py_compile __init__.py
hermes plugins list --plain --no-bundled
```
