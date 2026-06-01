# Hermes Discord Rich Presence

Hermes plugin that sets the Discord bot's Rich Presence from recent Hermes activity in `~/.hermes/state.db`.

It does not patch Hermes core. It registers `pre_gateway_dispatch`, finds the running Discord adapter, and updates the bot status in the background.

## Presence rotation

Examples:

```text
Today: 7 sessions / 484 messages
Open: 6 sessions on discord
Latest: Discord Bot Rich Presence
Model: deepseek-v4-flash
Last Discord msg 4m ago
```

There is no "online" label. Discord already shows that. The status text is for useful runtime context.

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

Set this before starting Hermes to disable updates without uninstalling the plugin:

```bash
DISCORD_PRESENCE_ENABLED=false
```

## Behavior notes

- The presence loop starts after the first inbound Discord gateway message because Hermes currently exposes this plugin through `pre_gateway_dispatch`.
- Startup presence before the first message is not implemented because Hermes does not expose an official Discord-ready plugin hook.
- The plugin avoids monkey-patching the Discord adapter or Hermes runtime internals.
- Stats come from `~/.hermes/state.db`, so today's totals survive plugin restarts.
- Session stats are cached for 5 minutes. Discord message deltas are counted in memory between cache refreshes.
- Labels stay under Discord's custom status length.

## Development check

```bash
python -m py_compile __init__.py
hermes plugins list --plain --no-bundled
```
