# Architecture

## Overview

`claude-open` is a single-file Discord bot (`bot.py`) that lets you launch and manage
[Claude Code](https://claude.ai/code) remote-control sessions from your phone. You
message it in a private Discord channel; it starts `claude --remote-control` inside a
detached tmux session on the host machine, then replies with the `claude.ai/code/session_…`
URL so you can tap straight into the session.

```
Phone / Discord app
       │
       │  !repos / 0 / !status / …
       ▼
Discord Gateway (WebSocket)
       │
       ▼
  bot.py (asyncio event loop)
       │
       ├─ list_repos()          ← scans GIT_ROOT for .git dirs
       │
       ├─ launch_remote_control()
       │       │
       │       └─ tmux new-session … claude --remote-control <session>
       │                              │
       │                              └─ detached tmux pane (on-host)
       │
       └─ idle_reaper_loop()   ← background asyncio.Task
               │
               └─ kills sessions idle ≥ SESSION_IDLE_TIMEOUT_HOURS
```

## Components

### `bot.py`

All runtime logic lives in one file, organised into four layers.

| Layer | Functions | Responsibility |
|---|---|---|
| **Pure helpers** | `list_repos`, `sanitize_session_name`, `_repo_session_name`, `format_*`, `_format_idle` | No I/O; easy to unit-test |
| **tmux interface** | `tmux_session_exists`, `list_active_sessions`, `kill_session`, `_update_pane_snapshot`, `_claude_project_mtime`, `list_session_idle_hours`, `reap_idle_sessions`, `_session_has_live_claude` | Subprocess calls to tmux / pgrep / git |
| **Session lifecycle** | `launch_remote_control`, `create_and_launch`, `clone_and_launch`, `_extract_session_url`, `_extract_auth_prompt` | Orchestrates tmux + async polling |
| **Discord event handlers** | `on_ready`, `on_message`, `idle_reaper_loop` | Drives the bot; routes commands |

### External dependencies

| Dependency | Role |
|---|---|
| `discord.py` | WebSocket connection to Discord; `on_message` event dispatch |
| `python-dotenv` | Loads `.env` at startup |
| `tmux` | Hosts detached Claude Code sessions |
| `claude` CLI | Started inside tmux as `claude --remote-control <session-name>` |
| `git` | Used by `create_and_launch` / `clone_and_launch` to init / clone repos |

### Configuration (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `DISCORD_BOT_TOKEN` | _(required)_ | Bot auth token |
| `ALLOWED_USER_ID` | _(required)_ | Only this Discord user is obeyed |
| `ALLOWED_CHANNEL_ID` | _(required)_ | Only this channel is watched |
| `GIT_ROOT` | `~/git` | Root directory scanned for repos |
| `SESSION_IDLE_TIMEOUT_HOURS` | `72` | Sessions idle this long are auto-killed |
| `BASH_TIMEOUT_SECONDS` | `60` | Timeout for `!bash` commands |
| `KNOWN_DEVICES` | _(hostname)_ | Comma-separated hostnames of all machines sharing the channel |

### `claude-discord-bot.service`

A systemd unit file for running the bot on Linux / Raspberry Pi. It:
- Waits for `network-online.target` before starting.
- Runs as the `pi` user from `WorkingDirectory=/home/pi/git/claude-open`.
- Loads `.env` via `EnvironmentFile=`.
- Restarts automatically on failure with a 5-second back-off.

## Data flow

### Launching a session

```
user: "!repos"
  → on_message receives message
  → list_repos() scans GIT_ROOT/*.git
  → pending_listings[channel_id] = [list of Paths]
  → bot replies with numbered code block

user: "2"
  → on_message sees a digit, looks up pending_listings[channel_id]
  → calls launch_remote_control(repo_path)
      ├─ if tmux session exists:
      │     poll pane for SESSION_URL_RE (3s) → reply with existing URL
      │     or check _session_has_live_claude → reply "already running"
      │     or kill dead session and fall through
      └─ tmux new-session -d -s <name> -c <repo> "claude --remote-control <name>"
            ├─ poll pane 15s for SESSION_URL_RE → reply with URL
            └─ if no URL: poll 5s for auth prompt → reply "Authorization required"
```

### Session naming

Session names are derived deterministically:

```
_repo_session_name("my-repo")  →  "<hostname>-MyRepo"
```

This prevents duplicate sessions and lets the bot locate an existing session by
name without scanning all tmux sessions.

### Activity tracking

The bot tracks session activity from three independent sources and takes the
most recent timestamp:

1. **tmux's own `session_activity`** — updated on any keypress in the pane.
2. **Pane content hash** (`_pane_snapshots`) — updated whenever the visible
   output changes; catches Claude streaming output without user keypresses.
3. **`~/.claude/projects/` mtime** — updated when Claude Code writes to its
   JSONL conversation log; the most reliable signal that Claude is doing work.

`_update_pane_snapshot` must be called before reading `_pane_snapshots`; the
reaper loop and `!activity` handler both call it first.

### Idle reaper

`idle_reaper_loop` runs as a background `asyncio.Task`, waking every
`IDLE_CHECK_INTERVAL_SECONDS` (1 hour). It:

1. Updates pane snapshots for all running sessions.
2. Calls `reap_idle_sessions`, which kills any session whose most-recent
   activity (across all three sources) is older than `IDLE_TIMEOUT_HOURS`.
3. Skips sessions where `_session_has_live_claude` is True (a Claude process
   is still running inside the pane).
4. Posts a cleanup notice to the Discord channel with any killed sessions.

## Multi-device routing

Multiple machines (Pis, Macs, etc.) can share one Discord channel. Each
machine runs its own bot process with its own `DISCORD_BOT_TOKEN`.

### State

Per-channel activation is tracked in `active_until: dict[int, float]` (keyed
by channel ID). A device is "active" when `time.monotonic() < active_until[channel_id]`.
Activation is persistent — `active_until` is set to `float("inf")` — until
another device is addressed.

### Routing rules (in order)

1. **Bare hostname** (`"raspberrypi"`) → this device calls `activate(channel_id)`
   and replies "Ready". Other devices see a known hostname as `first_word` and
   clear their own `active_until`.
2. **Prefixed command** (`"raspberrypi !repos"`) → this device activates and
   strips the prefix; other devices ignore it.
3. **Another known device addressed** → this device clears its `active_until`
   and returns silently.
4. **Not active, not addressed** → only `!help` / `!devices` produce a response
   (each bot announces itself as available).
5. **Active, no prefix** → command is processed normally.

`KNOWN_DEVICES` must list all hostnames sharing the channel so step 3 fires
correctly. A device not in `KNOWN_DEVICES` is never silenced by address.

## In-memory state

| Variable | Type | Purpose |
|---|---|---|
| `pending_listings` | `dict[int, list[Path]]` | Repo list from last `!repos` per channel, so a bare number reply knows what it refers to |
| `pending_sessions` | `dict[int, list[str]]` | Session list from last `!status` per channel, so `!kill <n>` knows what it refers to |
| `active_until` | `dict[int, float]` | Monotonic deadline for this device's active window per channel |
| `_pane_snapshots` | `dict[str, tuple[str, float]]` | `(md5_hash, unix_timestamp)` of last observed pane content per session |

All state is in-process and non-persistent. A bot restart resets it.

## Security model

The bot is intentionally minimal and personal:

- **One allowed user.** `ALLOWED_USER_ID` is checked on every message; all
  others are silently dropped.
- **One allowed channel.** `ALLOWED_CHANNEL_ID` prevents the bot responding
  in unintended servers.
- **`!bash` runs as the bot's system user.** Treat the Discord channel as
  equivalent to SSH access to the machine. `BASH_TIMEOUT_SECONDS` limits
  runaway commands; output over 2000 characters is truncated to the tail.
- **`.env` is gitignored.** The bot token and IDs are never committed.

## File layout

```
claude-open/
├── bot.py                      # All bot logic
├── claude-discord-bot.service  # systemd unit (Linux/Pi)
├── .env.example                # Config template
├── .env                        # Real config (gitignored)
├── requirements.txt            # Runtime: discord.py, python-dotenv
├── requirements-dev.txt        # + pytest
├── pytest.ini                  # Test config
├── tests/
│   ├── conftest.py
│   └── test_bot.py             # Unit tests (pure helpers + on_message)
├── README.md
└── ARCHITECTURE.md             # This file
```

## Testing

Tests cover the pure-logic layer and the `on_message` handler with subprocess
and Discord objects mocked out — nothing touches a real tmux session or Discord
server.

```
.venv/bin/python -m pytest
```

The `on_message` tests use `AsyncMock` for Discord's send methods and
`monkeypatch` / `patch` for subprocess calls, so they run without any external
services.
