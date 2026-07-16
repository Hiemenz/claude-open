"""
Discord bot that lets you pick a git repo (from a folder on this Pi) and
launches a `claude --remote-control` session in it, from your phone.

Flow:
  you:  !repos
  bot:  0: local-code
        1: code-quality
        2: terminal-display
        3: venture-zero
  you:  2
  bot:  Started remote-control session 'terminal-display' ...
        -> pick it up in the Claude app / claude.ai/code

Only responds to one Discord user in one Discord channel (see .env).
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import discord
from dotenv import load_dotenv

load_dotenv()


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(
            f"Missing required environment variable {name}. "
            "Copy .env.example to .env and fill it in."
        )
    return value


DISCORD_TOKEN = require_env("DISCORD_BOT_TOKEN")
ALLOWED_USER_ID = int(require_env("ALLOWED_USER_ID"))
ALLOWED_CHANNEL_ID = int(require_env("ALLOWED_CHANNEL_ID"))
GIT_ROOT = Path(os.environ.get("GIT_ROOT", str(Path.home() / "git"))).expanduser().resolve()

CLAUDE_BIN = shutil.which("claude") or "claude"
TMUX_BIN = shutil.which("tmux") or "tmux"

LIST_COMMANDS = {"!repos", "!repo", "!ls", "!claude"}
STATUS_COMMANDS = {"!status", "!sessions", "!ps"}
KILL_COMMANDS = {"!kill", "!stop"}

# Discord hard-caps message content at 2000 characters.
DISCORD_MESSAGE_LIMIT = 2000

# Per-channel: repo list from the most recent !repos, so a bare number reply
# knows what it refers to.
pending_listings: dict[int, list[Path]] = {}

# Per-channel: session-name list from the most recent !status, so
# `!kill <number>` knows what it refers to.
pending_sessions: dict[int, list[str]] = {}


def list_repos() -> list[Path]:
    if not GIT_ROOT.is_dir():
        return []
    repos = [p for p in GIT_ROOT.iterdir() if p.is_dir() and (p / ".git").exists()]
    return sorted(repos, key=lambda p: p.name.lower())


def format_numbered_listing(names: list[str], footer: str) -> list[str]:
    """Render numbered names as one or more Discord-sized code blocks."""
    lines = [f"{i}: {name}" for i, name in enumerate(names)]

    chunks = []
    current: list[str] = []
    current_len = 0
    overhead = len("```\n") + len("\n```")
    for line in lines:
        added = len(line) + 1
        if current and current_len + added + overhead > DISCORD_MESSAGE_LIMIT:
            chunks.append("```\n" + "\n".join(current) + "\n```")
            current, current_len = [], 0
        current.append(line)
        current_len += added
    if current:
        chunks.append("```\n" + "\n".join(current) + "\n```")

    chunks[-1] += f"\n{footer}"
    return chunks


def format_repo_listing(repos: list[Path]) -> list[str]:
    return format_numbered_listing([repo.name for repo in repos], "Reply with a number.")


def format_session_listing(sessions: list[str]) -> list[str]:
    return format_numbered_listing(
        sessions, "Reply with `!kill <number>` to stop one, or `!kill <name>`."
    )


def sanitize_session_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "-", name)
    return cleaned or "claude-session"


def tmux_session_exists(session_name: str) -> bool:
    result = subprocess.run(
        [TMUX_BIN, "has-session", "-t", session_name],
        capture_output=True,
    )
    return result.returncode == 0


def list_active_sessions() -> list[str]:
    """List running tmux session names (empty if none, or no tmux server)."""
    result = subprocess.run(
        [TMUX_BIN, "list-sessions", "-F", "#{session_name}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def kill_session(session_name: str) -> str:
    if not tmux_session_exists(session_name):
        return f"No running session named **{session_name}**."

    subprocess.run([TMUX_BIN, "kill-session", "-t", session_name], check=True)
    return f"Stopped session **{session_name}**."


def launch_remote_control(repo_path: Path) -> str:
    session_name = sanitize_session_name(repo_path.name)

    if tmux_session_exists(session_name):
        return (
            f"Session **{session_name}** is already running in `{repo_path}` — "
            "pick it up in the Claude app / claude.ai/code."
        )

    subprocess.run(
        [
            TMUX_BIN,
            "new-session",
            "-d",
            "-s",
            session_name,
            "-c",
            str(repo_path),
            f"{CLAUDE_BIN} --remote-control {session_name}",
        ],
        check=True,
    )

    return (
        f"Started remote-control session **{session_name}** in `{repo_path}` — "
        "pick it up in the Claude app / claude.ai/code."
    )


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"Logged in as {client.user} (id={client.user.id})")
    print(f"Watching channel {ALLOWED_CHANNEL_ID} for user {ALLOWED_USER_ID}")
    print(f"GIT_ROOT = {GIT_ROOT}")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.author.id != ALLOWED_USER_ID or message.channel.id != ALLOWED_CHANNEL_ID:
        return

    content = message.content.strip()

    if content.lower() in LIST_COMMANDS:
        repos = list_repos()
        pending_listings[message.channel.id] = repos

        if not repos:
            await message.channel.send(f"No git repos found under `{GIT_ROOT}`.")
            return

        for chunk in format_repo_listing(repos):
            await message.channel.send(chunk)
        return

    if content.lower() in STATUS_COMMANDS:
        sessions = list_active_sessions()
        pending_sessions[message.channel.id] = sessions

        if not sessions:
            await message.channel.send("No sessions running.")
            return

        for chunk in format_session_listing(sessions):
            await message.channel.send(chunk)
        return

    command, _, rest = content.partition(" ")
    if command.lower() in KILL_COMMANDS:
        arg = rest.strip()
        if not arg:
            await message.channel.send(
                "Usage: `!kill <number>` (from `!status`) or `!kill <name>`."
            )
            return

        if arg.isdigit():
            sessions = pending_sessions.get(message.channel.id)
            if not sessions:
                await message.channel.send("No status list yet — send `!status` first.")
                return
            idx = int(arg)
            if not (0 <= idx < len(sessions)):
                await message.channel.send(f"Pick a number between 0 and {len(sessions) - 1}.")
                return
            session_name = sessions[idx]
        else:
            session_name = sanitize_session_name(arg)

        try:
            reply = kill_session(session_name)
        except subprocess.CalledProcessError as exc:
            reply = f"Failed to stop session: {exc}"
        await message.channel.send(reply)
        return

    if content.isdigit():
        repos = pending_listings.get(message.channel.id)
        if not repos:
            await message.channel.send("No repo list yet — send `!repos` first.")
            return

        idx = int(content)
        if not (0 <= idx < len(repos)):
            await message.channel.send(f"Pick a number between 0 and {len(repos) - 1}.")
            return

        repo_path = repos[idx]
        try:
            reply = launch_remote_control(repo_path)
        except subprocess.CalledProcessError as exc:
            reply = f"Failed to start session: {exc}"
        await message.channel.send(reply)
        return


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
