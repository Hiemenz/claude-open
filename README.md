# claude-open

Discord bot for this Pi: DM/message it in one private channel, it lists the
git repos under `~/git`, you reply with a number, it launches
`claude --remote-control` inside that repo in a detached tmux session so you
can pick the session up from the Claude app / claude.ai/code.

## 1. Install tmux (one-time, needs sudo)

```
sudo apt-get install -y tmux
```

## 2. Create the Discord bot application

1. Go to https://discord.com/developers/applications -> **New Application**.
2. **Bot** tab -> **Reset Token** -> copy it (this is `DISCORD_BOT_TOKEN`).
3. Same **Bot** tab -> under **Privileged Gateway Intents**, enable
   **Message Content Intent**. Save changes.
4. **OAuth2 -> URL Generator** -> scope `bot` -> permissions
   `Send Messages` + `Read Message History` -> open the generated URL and
   invite the bot to a server only you can see (e.g. a private server with
   one channel).
5. In Discord, enable Developer Mode (User Settings -> Advanced), then:
   - Right-click your own name -> **Copy User ID** -> `ALLOWED_USER_ID`.
   - Right-click the private channel -> **Copy Channel ID** -> `ALLOWED_CHANNEL_ID`.

## 3. Configure

```
cp .env.example .env
# edit .env and fill in DISCORD_BOT_TOKEN, ALLOWED_USER_ID, ALLOWED_CHANNEL_ID
```

## 4. Run it manually to test

```
.venv/bin/python bot.py
```

In the private channel, send `!repos`. You should get a numbered list of
folders under `~/git` that contain a `.git` directory. Reply with a number
and it should start a session.

## 5. Run it as a service (auto-start on boot)

```
sudo cp claude-discord-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now claude-discord-bot
sudo systemctl status claude-discord-bot
```

Logs: `journalctl -u claude-discord-bot -f`

## Usage

- `!repos` (also `!repo`, `!ls`, `!claude`) — list repos under `GIT_ROOT`.
- Reply with the number — starts (or resumes, if already running)
  `claude --remote-control <repo-name>` in that repo's directory, as a
  detached tmux session named after the repo.

## Debugging a session directly on the Pi

Each launched session lives in its own tmux session, named after the repo:

```
tmux ls                    # see what's running
tmux attach -t <repo-name> # attach directly (Ctrl-b d to detach again)
```

## Notes

- The bot only ever responds to `ALLOWED_USER_ID` in `ALLOWED_CHANNEL_ID` —
  everything else is silently ignored.
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
- **No session management commands.** You can start/resume a session, but
  there's no `!kill <repo>` or `!status` to list/stop running tmux sessions
  from Discord — you have to SSH in and use `tmux kill-session`.
- **No repo search/filter.** `!repos` always lists everything one level
  under `GIT_ROOT`; fine for a handful of repos, less so for dozens.
- **No logging beyond `print`.** Systemd captures it via journald, but
  there's no log level control or structured logging if this ever needs to
  be debugged remotely.
- **Not yet a git repo itself.** There's a `.gitignore` but no `.git` —
  worth `git init`-ing so changes to the bot are tracked like everything
  else it manages.
- **No CI.** Tests exist now, but nothing runs them automatically on push
  (relevant once this is in git/GitHub).
