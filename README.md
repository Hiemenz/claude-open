# claude-open

Discord bot for controlling Claude Code sessions from your phone. Message it
in a private Discord channel, it lists the git repos under `~/git`, you reply
with a number, it launches `claude --remote-control` in a detached tmux
session so you can pick it up from the Claude app / claude.ai/code.

Supports multiple machines (Pis, Macs, etc.) sharing one channel — each
machine only responds when you address it by hostname.

See [ARCHITECTURE.md](ARCHITECTURE.md) for a detailed description of the
system design, data flow, and component breakdown.

## 1. Install tmux (one-time, needs sudo)

**Linux / Raspberry Pi**
```
sudo apt-get install -y tmux
```

**macOS**
```
brew install tmux
```

## 2. Create a Discord bot application (one per machine)

Each machine needs its own bot token. For each one:

1. Go to https://discord.com/developers/applications -> **New Application**.
2. **Bot** tab -> **Reset Token** -> copy it (this is `DISCORD_BOT_TOKEN`).
3. Same **Bot** tab -> under **Privileged Gateway Intents**, enable
   **Message Content Intent**. Save changes.
4. **OAuth2 -> URL Generator** -> scope `bot` -> permissions
   `Send Messages` + `Read Message History` -> open the generated URL and
   invite the bot to **the same private server/channel on every machine**.
5. In Discord, enable Developer Mode (User Settings -> Advanced), then:
   - Right-click your own name -> **Copy User ID** -> `ALLOWED_USER_ID`.
   - Right-click the private channel -> **Copy Channel ID** -> `ALLOWED_CHANNEL_ID`
     (same value on every machine).

## 3. Configure

```
cp .env.example .env
# edit .env — each machine gets its own DISCORD_BOT_TOKEN;
# ALLOWED_USER_ID and ALLOWED_CHANNEL_ID are the same on every machine
```

If you have multiple machines, also set `KNOWN_DEVICES` (see
[Multi-machine setup](#multi-machine-setup) below).

## 4. Run it manually to test

```
.venv/bin/python bot.py
```

If running a single machine, send `!repos` in the channel. With multiple
machines, type the device's hostname first (e.g. `raspberrypi`) to activate
it, then `!repos`.

## 5. Run it as a service (auto-start on boot)

**Linux / Raspberry Pi (systemd)**

```
sudo cp claude-discord-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now claude-discord-bot
sudo systemctl status claude-discord-bot
```

Logs: `journalctl -u claude-discord-bot -f`

**macOS — run in a tmux session**

```
tmux new-session -d -s discord-bot '.venv/bin/python bot.py'
```

Attach to check on it: `tmux attach -t discord-bot`. No service file needed.

## Multi-machine setup

Multiple machines (Pis, Macs, etc.) can share a single Discord channel. Each
machine runs its own bot with its own token but the same `ALLOWED_CHANNEL_ID`.

Add `KNOWN_DEVICES` to every machine's `.env` — a comma-separated list of all
hostnames sharing the channel:

```
KNOWN_DEVICES=raspberrypi,macbook,macmini
```

**How it works:**

- Type a machine's hostname alone to activate it:
  ```
  raspberrypi
  ```
  That bot replies: `Ready — raspberrypi active.`
- Commands then go to that machine without a prefix:
  ```
  !repos
  0
  ```
- Type another hostname to switch machines — the active one steps back silently.
- You can also one-shot a command without prior activation:
  ```
  macbook !stats
  ```
- `!help` with no machine active shows all online bots (each announces itself),
  acting as a live device list.

The active device stays selected until you type another device name to switch.

## Usage

Send `!help` in the channel to get the full command list. Quick reference:

| Command | What it does |
|---|---|
| `!repos` | List git repos under `GIT_ROOT` |
| `<number>` | Launch a session for that repo (from last `!repos`) |
| `<github url>` | Clone the repo to `~/git/` and launch a session |
| `!new <name>` | Create a new repo at `~/git/<name>` and launch a session |
| `!status` | List running sessions |
| `!kill <number\|name>` | Stop a session |
| `!activity` | Show last activity time per session |
| `!stats` | Show Pi CPU / RAM / disk / temp / uptime |
| `!bash <command>` | Run a raw shell command on the Pi (aliases: `!sh`, `!exec`) |
| `!help` | Show command list |

When a session starts, the bot replies with the direct `claude.ai/code/session_…`
URL — tap it from Discord to open the session on your phone.

## Idle session cleanup

Archiving a session in the Claude app only affects claude.ai — it does not
touch the tmux session running on the Pi. To avoid sessions piling up, the
bot checks hourly for tmux sessions with no activity (tmux's own
`session_activity` timestamp) for `SESSION_IDLE_TIMEOUT_HOURS` (default 72)
and kills them automatically, posting a note in the Discord channel when it
does. Set `SESSION_IDLE_TIMEOUT_HOURS` in `.env` to change the threshold.

## Debugging a session directly on the Pi

Each launched session lives in its own tmux session, named after the repo:

```
tmux ls                    # see what's running
tmux attach -t <repo-name> # attach directly (Ctrl-b d to detach again)
```

## Notes

- The bot only ever responds to `ALLOWED_USER_ID` in `ALLOWED_CHANNEL_ID` —
  everything else is silently ignored. `!bash` runs with whatever permissions
  the bot's system user has, so treat `ALLOWED_USER_ID`/`ALLOWED_CHANNEL_ID`
  as the only thing standing between Discord and a shell on this machine.
  Commands run with `cwd=GIT_ROOT` and a `BASH_TIMEOUT_SECONDS` (default 60)
  timeout; output over Discord's 2000-char limit is truncated to the tail.
- Picking the same repo twice reuses the existing tmux/remote-control
  session instead of starting a duplicate.
- `.env` holds your bot token — it's gitignored; never commit it.

## Development

Install dev dependencies (adds `pytest` on top of the runtime requirements):

```
.venv/bin/pip install -r requirements-dev.txt
```

Run the tests:

```
.venv/bin/python -m pytest
```

The tests cover the pure logic (repo discovery/sorting, session-name
sanitizing, Discord message chunking) and the `on_message` handler (auth
filtering, `!repos` / numeric-reply flow, error reporting), with `subprocess`
and Discord objects mocked out — nothing touches a real tmux session or
Discord server.

## Known gaps / possible improvements

Not blocking for personal use, but worth knowing about:

- **Single hardcoded user/channel.** By design (see Notes above), but means
  sharing this with anyone else means editing `.env`, not adding a role.
- **No repo search/filter.** `!repos` always lists everything one level
  under `GIT_ROOT`; fine for a handful of repos, less so for dozens.
- **No logging beyond `print`.** Systemd captures it via journald, but
  there's no log level control or structured logging if this ever needs to
  be debugged remotely.
- **No CI.** Tests exist but nothing runs them automatically on push.
