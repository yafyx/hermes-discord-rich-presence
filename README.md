# Hermes Discord Rich Presence

Hermes plugin that rotates the Discord bot's custom Rich Presence through compact runtime signals from `~/.hermes/state.db`.

It does not patch Hermes core. It registers the `pre_gateway_dispatch` hook, discovers the running Discord adapter lazily, and updates the bot status in the background.

## Presence Rotation

Examples:

```text
Today: 7 sessions / 484 messages
Open: 6 sessions on discord
Latest: Discord Bot Rich Presence
Model: deepseek-v4-flash
Last Discord msg 4m ago
```

The plugin intentionally avoids decorative "online" labels. If the bot is visible in Discord, online state is already obvious; the presence text should carry operational information.

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

Set this environment variable before starting Hermes to disable updates without uninstalling the plugin:

```bash
DISCORD_PRESENCE_ENABLED=false
```

## Behavior Notes

- The presence loop starts after the first inbound Discord gateway message because Hermes currently exposes this plugin through `pre_gateway_dispatch`.
- Stats are read from `~/.hermes/state.db`, so today's session/message totals survive plugin restarts.
- The plugin only uses Discord custom activity text and keeps labels under Discord's custom status length.

## Development Check

```bash
python -m py_compile __init__.py
hermes plugins list --plain --no-bundled
```

