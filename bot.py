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
import socket
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

DEVICE_NAME = socket.gethostname()

CLAUDE_BIN = shutil.which("claude") or "claude"
TMUX_BIN = shutil.which("tmux") or "tmux"
GIT_BIN = shutil.which("git") or "git"

GITHUB_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?$"
)

LIST_COMMANDS = {"!repos", "!repo", "!ls", "!claude"}
STATUS_COMMANDS = {"!status", "!sessions", "!ps"}
KILL_COMMANDS = {"!kill", "!stop"}
NEW_COMMANDS = {"!new", "!create", "!init"}
STATS_COMMANDS = {"!stats", "!stat", "!pi", "!sys"}
HELP_COMMANDS = {"!help", "!h", "!?"}

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


def _repo_session_name(repo_name: str) -> str:
    """Return a session name like raspberrypiZero from device name + repo name 'zero'."""
    pascal = "".join(word.capitalize() for word in re.split(r"[-_\s]+", repo_name) if word)
    return DEVICE_NAME + "-" + pascal


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


AUTH_URL_RE = re.compile(r"https?://\S*(auth|login|oauth|authorize)\S*", re.IGNORECASE)
SESSION_URL_RE = re.compile(r"https://claude\.ai/code/session_\S+")


def _extract_session_url(session_name: str, timeout: float = 15.0) -> str | None:
    """Poll tmux pane until the remote-control URL appears, then return it."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            [TMUX_BIN, "capture-pane", "-t", session_name, "-p"],
            capture_output=True,
            text=True,
        )
        match = SESSION_URL_RE.search(result.stdout)
        if match:
            return match.group(0)
        time.sleep(0.5)
    return None


def _extract_auth_prompt(session_name: str, timeout: float = 5.0) -> str | None:
    """Check tmux pane for an authorization URL or prompt from Claude."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            [TMUX_BIN, "capture-pane", "-t", session_name, "-p"],
            capture_output=True,
            text=True,
        )
        text = result.stdout
        auth_match = AUTH_URL_RE.search(text)
        if auth_match:
            return auth_match.group(0)
        for keyword in ("authorize", "log in", "sign in", "authentication required"):
            if keyword in text.lower():
                line = next(
                    (l.strip() for l in text.splitlines() if keyword in l.lower()), None
                )
                if line:
                    return line
        time.sleep(0.5)
    return None


def launch_remote_control(
    repo_path: Path,
    channel_name: str = "",
    guild_name: str = "",
) -> tuple[str, str | None]:
    """Returns (reply_text, auth_prompt_or_None)."""
    session_name = _repo_session_name(repo_path.name)
    source = f"#{channel_name}" if channel_name else "Discord"
    if guild_name:
        source = f"{guild_name} / {source}"
    device_tag = f"on **{DEVICE_NAME}**"

    if tmux_session_exists(session_name):
        url = _extract_session_url(session_name, timeout=3.0)
        suffix = f"\n{url}" if url else " — pick it up in the Claude app / claude.ai/code."
        return (
            f"Session **{session_name}** is already running in `{repo_path}` "
            f"({source}, {device_tag}){suffix}",
            None,
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

    url = _extract_session_url(session_name)
    if url:
        suffix = f"\n{url}"
        auth = None
    else:
        auth = _extract_auth_prompt(session_name)
        suffix = " — pick it up in the Claude app / claude.ai/code."

    return (
        f"Started remote-control session **{session_name}** in `{repo_path}` "
        f"({source}, {device_tag}){suffix}",
        auth,
    )


def pi_stats() -> str:
    lines = [f"Host:  {DEVICE_NAME}"]

    # CPU
    cpu = subprocess.run(
        ["top", "-bn1"], capture_output=True, text=True
    ).stdout
    for line in cpu.splitlines():
        if "Cpu(s)" in line or "cpu(s)" in line.lower():
            idle = re.search(r"([\d.]+)\s*id", line)
            if idle:
                used = 100.0 - float(idle.group(1))
                lines.append(f"CPU:   {used:.1f}%")
            break

    # Memory
    mem = subprocess.run(["free", "-h"], capture_output=True, text=True).stdout
    for line in mem.splitlines():
        if line.startswith("Mem:"):
            parts = line.split()
            lines.append(f"RAM:   {parts[2]} / {parts[1]} used")
            break

    # Disk
    disk = subprocess.run(["df", "-h", "/"], capture_output=True, text=True).stdout
    for line in disk.splitlines()[1:]:
        parts = line.split()
        lines.append(f"Disk:  {parts[2]} / {parts[1]} used ({parts[4]})")
        break

    # Temperature
    temp = subprocess.run(["vcgencmd", "measure_temp"], capture_output=True, text=True)
    if temp.returncode == 0:
        lines.append(f"Temp:  {temp.stdout.strip().replace('temp=', '')}")

    # Uptime / load
    up = subprocess.run(["uptime", "-p"], capture_output=True, text=True)
    load = subprocess.run(["uptime"], capture_output=True, text=True)
    if up.returncode == 0:
        lines.append(f"Up:    {up.stdout.strip()}")
    m = re.search(r"load average[s]?:\s*([\d.]+)", load.stdout)
    if m:
        lines.append(f"Load:  {m.group(1)} (1m avg)")

    return "```\n" + "\n".join(lines) + "\n```"


def create_and_launch(
    name: str, channel_name: str = "", guild_name: str = ""
) -> tuple[str, str | None]:
    safe = sanitize_session_name(name)
    dest = GIT_ROOT / safe

    if dest.exists():
        return launch_remote_control(dest, channel_name, guild_name)

    dest.mkdir(parents=True)
    subprocess.run([GIT_BIN, "init", str(dest)], capture_output=True, check=True)
    return launch_remote_control(dest, channel_name, guild_name)


def clone_and_launch(
    github_url: str, channel_name: str = "", guild_name: str = ""
) -> tuple[str, str | None]:
    m = GITHUB_URL_RE.match(github_url)
    if not m:
        return ("That doesn't look like a valid GitHub URL.", None)

    repo_name = m.group("repo")
    dest = GIT_ROOT / repo_name

    if dest.exists():
        return launch_remote_control(dest, channel_name, guild_name)

    result = subprocess.run(
        [GIT_BIN, "clone", github_url, str(dest)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return (f"Clone failed:\n```\n{result.stderr.strip()}\n```", None)

    return launch_remote_control(dest, channel_name, guild_name)


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

    if content.lower() in HELP_COMMANDS:
        await message.channel.send(
            "```\n"
            "!repos              List available git repos\n"
            "<number>            Launch a session for that repo\n"
            "<github url>        Clone repo and launch a session\n"
            "!new <name>         Create a new repo and launch a session\n"
            "!status             List running sessions\n"
            "!kill <number>      Kill a session by number (from !status)\n"
            "!kill <name>        Kill a session by name\n"
            "!stats              Show Pi CPU / RAM / disk / temp / uptime\n"
            "!help               Show this message\n"
            "```"
        )
        return

    if content.lower() in STATS_COMMANDS:
        await message.channel.send(pi_stats())
        return

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

    ch_name = getattr(message.channel, "name", str(message.channel.id))
    guild_name = message.guild.name if message.guild else ""

    if command.lower() in NEW_COMMANDS:
        name = rest.strip()
        if not name:
            await message.channel.send("Usage: `!new <project-name>`")
            return
        try:
            reply, auth = create_and_launch(name, ch_name, guild_name)
        except subprocess.CalledProcessError as exc:
            reply, auth = f"Failed to create project: {exc}", None
        await message.channel.send(reply)
        if auth:
            await message.channel.send(f"**Authorization required:** {auth}")
        return

    if GITHUB_URL_RE.match(content):
        await message.channel.send("Cloning…")
        try:
            reply, auth = clone_and_launch(content, ch_name, guild_name)
        except subprocess.CalledProcessError as exc:
            reply, auth = f"Failed to start session: {exc}", None
        await message.channel.send(reply)
        if auth:
            await message.channel.send(f"**Authorization required:** {auth}")
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
            reply, auth = launch_remote_control(repo_path, ch_name, guild_name)
        except subprocess.CalledProcessError as exc:
            reply, auth = f"Failed to start session: {exc}", None
        await message.channel.send(reply)
        if auth:
            await message.channel.send(f"**Authorization required:** {auth}")
        return


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
