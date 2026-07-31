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

import asyncio
import hashlib
import os
import re
import shutil
import socket
import subprocess
import time
from datetime import datetime
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

# Idle tmux sessions (no pane activity for this long) get auto-killed.
IDLE_TIMEOUT_HOURS = float(os.environ.get("SESSION_IDLE_TIMEOUT_HOURS", "72"))
IDLE_CHECK_INTERVAL_SECONDS = 60 * 60

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
BASH_COMMANDS = {"!bash", "!sh", "!exec"}
HELP_COMMANDS = {"!help", "!h", "!?"}
DEVICE_COMMANDS = {"!devices", "!device", "!pis"}
ACTIVITY_COMMANDS = {"!activity", "!idle", "!when"}

# Hostnames of all Pis sharing this channel (comma-separated in .env).
# Used to detect when another device is being addressed so this bot stays silent.
KNOWN_DEVICES = frozenset(
    d.strip().lower()
    for d in os.environ.get("KNOWN_DEVICES", DEVICE_NAME).split(",")
    if d.strip()
)


BASH_TIMEOUT_SECONDS = float(os.environ.get("BASH_TIMEOUT_SECONDS", "60"))

# Discord hard-caps message content at 2000 characters.
DISCORD_MESSAGE_LIMIT = 2000

# Per-channel: repo list from the most recent !repos, so a bare number reply
# knows what it refers to.
pending_listings: dict[int, list[Path]] = {}

# Per-channel: session-name list from the most recent !status, so
# `!kill <number>` knows what it refers to.
pending_sessions: dict[int, list[str]] = {}

# Per-channel: when this device's active window expires (monotonic seconds).
active_until: dict[int, float] = {}

# Per-session: (pane_content_hash, unix_timestamp_of_last_change)
_pane_snapshots: dict[str, tuple[str, float]] = {}


def is_active(channel_id: int) -> bool:
    return time.monotonic() < active_until.get(channel_id, 0.0)


def activate(channel_id: int) -> None:
    active_until[channel_id] = float("inf")


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


def _format_idle(hours: float) -> str:
    if hours < 1 / 60:
        return "just now"
    if hours < 1:
        return f"{int(hours * 60)}m ago"
    if hours < 24:
        h, m = int(hours), int((hours % 1) * 60)
        return f"{h}h {m}m ago" if m else f"{h}h ago"
    d, h = int(hours // 24), int(hours % 24)
    return f"{d}d {h}h ago" if h else f"{d}d ago"


def session_activity_report() -> str:
    """Build a Discord-ready report of last activity per session (updates pane snapshots first)."""
    sessions = list_active_sessions()
    if not sessions:
        return "No sessions running."
    for name in sessions:
        _update_pane_snapshot(name)
    idle = list_session_idle_hours()
    now = time.time()
    lines = []
    for name in sessions:
        hours = idle.get(name, 0)
        overall_dt = datetime.fromtimestamp(now - hours * 3600).strftime("%Y-%m-%d %H:%M")
        claude_mtime = _claude_project_mtime(name)
        if claude_mtime:
            claude_str = datetime.fromtimestamp(claude_mtime).strftime("%Y-%m-%d %H:%M")
        else:
            claude_str = "—"
        lines.append(
            f"{name:<32} {_format_idle(hours):<16} ({overall_dt})  claude: {claude_str}"
        )
    return "```\n" + "\n".join(lines) + "\n```"


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


def _update_pane_snapshot(session_name: str) -> float:
    """Capture pane content; update stored timestamp if it changed. Returns last-changed time."""
    result = subprocess.run(
        [TMUX_BIN, "capture-pane", "-t", session_name, "-p"],
        capture_output=True,
        text=True,
    )
    old_hash, ts = _pane_snapshots.get(session_name, ("", 0.0))
    if result.returncode != 0:
        return ts
    new_hash = hashlib.md5(result.stdout.encode()).hexdigest()
    now = time.time()
    if new_hash != old_hash:
        ts = now
    _pane_snapshots[session_name] = (new_hash, ts)
    return ts


def _claude_project_mtime(session_name: str) -> float | None:
    """Return the most recent JSONL mtime under ~/.claude/projects/ for this session's working dir."""
    pane = subprocess.run(
        [TMUX_BIN, "display-message", "-t", session_name, "-p", "#{pane_current_path}"],
        capture_output=True,
        text=True,
    )
    if pane.returncode != 0 or not pane.stdout.strip():
        return None

    work_dir = pane.stdout.strip()
    project_key = work_dir.replace("/", "-")
    project_dir = Path.home() / ".claude" / "projects" / project_key

    if not project_dir.is_dir():
        return None

    latest: float | None = None
    for f in project_dir.iterdir():
        if f.suffix == ".jsonl":
            try:
                mtime = f.stat().st_mtime
                if latest is None or mtime > latest:
                    latest = mtime
            except OSError:
                pass
    return latest


def list_session_idle_hours() -> dict[str, float]:
    """Map session name -> hours since last detected activity (pane change, ~/.claude writes, or tmux activity)."""
    result = subprocess.run(
        [TMUX_BIN, "list-sessions", "-F", "#{session_name} #{session_activity}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {}

    now = time.time()
    idle_hours = {}
    for line in result.stdout.splitlines():
        name, _, ts = line.rpartition(" ")
        if not name or not ts.isdigit():
            continue

        candidates = [int(ts)]

        _, pane_ts = _pane_snapshots.get(name, ("", 0.0))
        if pane_ts:
            candidates.append(pane_ts)

        file_mtime = _claude_project_mtime(name)
        if file_mtime:
            candidates.append(file_mtime)

        idle_hours[name] = (now - max(candidates)) / 3600
    return idle_hours


def reap_idle_sessions(timeout_hours: float = IDLE_TIMEOUT_HOURS) -> list[str]:
    """Kill tmux sessions idle for at least timeout_hours. Returns names killed."""
    killed = []
    for name, hours in list_session_idle_hours().items():
        if hours >= timeout_hours and not _session_has_live_claude(name):
            subprocess.run([TMUX_BIN, "kill-session", "-t", name], capture_output=True)
            killed.append(name)
    return killed


def _session_has_live_claude(session_name: str) -> bool:
    """Return True if a claude process is still running inside the tmux session."""
    pane = subprocess.run(
        [TMUX_BIN, "list-panes", "-t", session_name, "-F", "#{pane_pid}"],
        capture_output=True,
        text=True,
    )
    if pane.returncode != 0 or not pane.stdout.strip():
        return False
    pane_pid = pane.stdout.strip().split()[0]
    check = subprocess.run(
        ["pgrep", "-P", pane_pid],
        capture_output=True,
    )
    return check.returncode == 0


AUTH_URL_RE = re.compile(r"https?://\S*(auth|login|oauth|authorize)\S*", re.IGNORECASE)
SESSION_URL_RE = re.compile(r"https://claude\.ai/code/session_\S+")

TRUST_PROMPT = "Is this a project you created or one you trust"


async def _extract_session_url(session_name: str, timeout: float = 15.0) -> str | None:
    """Poll tmux pane until the remote-control URL appears, then return it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            [TMUX_BIN, "capture-pane", "-t", session_name, "-p"],
            capture_output=True,
            text=True,
        )
        pane = result.stdout
        match = SESSION_URL_RE.search(pane)
        if match:
            return match.group(0)
        if TRUST_PROMPT in pane:
            subprocess.run([TMUX_BIN, "send-keys", "-t", session_name, "Enter"])
        await asyncio.sleep(0.5)
    return None


async def _extract_auth_prompt(session_name: str, timeout: float = 5.0) -> str | None:
    """Check tmux pane for an authorization URL or prompt from Claude."""
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
                ln = next(
                    (ln.strip() for ln in text.splitlines() if keyword in ln.lower()), None
                )
                if ln:
                    return ln
        await asyncio.sleep(0.5)
    return None


async def launch_remote_control(
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
        url = await _extract_session_url(session_name, timeout=3.0)
        if url:
            return (
                f"Session **{session_name}** is already running in `{repo_path}` "
                f"({source}, {device_tag})\n{url}",
                None,
            )
        # Session exists but no URL — check if claude is still alive.
        if _session_has_live_claude(session_name):
            return (
                f"Session **{session_name}** is running ({source}, {device_tag}) "
                "— pick it up in the Claude app / claude.ai/code.",
                None,
            )
        # Dead session: kill it and fall through to restart.
        subprocess.run([TMUX_BIN, "kill-session", "-t", session_name], capture_output=True)

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

    url = await _extract_session_url(session_name)
    if url:
        suffix = f"\n{url}"
        auth = None
    else:
        auth = await _extract_auth_prompt(session_name)
        suffix = " — pick it up in the Claude app / claude.ai/code."

    return (
        f"Started remote-control session **{session_name}** in `{repo_path}` "
        f"({source}, {device_tag}){suffix}",
        auth,
    )


def run_bash_command(command: str, timeout: float = BASH_TIMEOUT_SECONDS) -> str:
    """Run a raw shell command on the Pi (cwd=GIT_ROOT) and format the result for Discord."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(GIT_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"Timed out after {timeout:.0f}s: `{command}`"

    output = (result.stdout + result.stderr).strip() or "(no output)"
    prefix = "" if result.returncode == 0 else f"[exit {result.returncode}] "
    budget = DISCORD_MESSAGE_LIMIT - len(prefix) - len("```\n…(truncated)…\n\n```")
    if len(output) > budget:
        output = "…(truncated)…\n" + output[-budget:]

    return f"{prefix}```\n{output}\n```"


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


async def create_and_launch(
    name: str, channel_name: str = "", guild_name: str = ""
) -> tuple[str, str | None]:
    safe = sanitize_session_name(name)
    dest = GIT_ROOT / safe

    if dest.exists():
        return await launch_remote_control(dest, channel_name, guild_name)

    dest.mkdir(parents=True)
    subprocess.run([GIT_BIN, "init", str(dest)], capture_output=True, check=True)
    return await launch_remote_control(dest, channel_name, guild_name)


async def clone_and_launch(
    github_url: str, channel_name: str = "", guild_name: str = ""
) -> tuple[str, str | None]:
    m = GITHUB_URL_RE.match(github_url)
    if not m:
        return ("That doesn't look like a valid GitHub URL.", None)

    repo_name = m.group("repo")
    dest = GIT_ROOT / repo_name

    if dest.exists():
        return await launch_remote_control(dest, channel_name, guild_name)

    result = await asyncio.to_thread(
        subprocess.run,
        [GIT_BIN, "clone", github_url, str(dest)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return (f"Clone failed:\n```\n{result.stderr.strip()}\n```", None)

    return await launch_remote_control(dest, channel_name, guild_name)


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

_reaper_task: asyncio.Task | None = None


async def idle_reaper_loop():
    """Periodically kill tmux sessions idle for IDLE_TIMEOUT_HOURS+ and report it."""
    while not client.is_closed():
        for name in await asyncio.to_thread(list_active_sessions):
            await asyncio.to_thread(_update_pane_snapshot, name)
        killed = await asyncio.to_thread(reap_idle_sessions)
        if killed:
            channel = client.get_channel(ALLOWED_CHANNEL_ID)
            if channel:
                names = ", ".join(f"**{name}**" for name in killed)
                await channel.send(
                    f"Cleaned up {len(killed)} session(s) idle {IDLE_TIMEOUT_HOURS:.0f}+ "
                    f"hours: {names}"
                )
                remaining = await asyncio.to_thread(list_active_sessions)
                if remaining:
                    for chunk in format_session_listing(remaining):
                        await channel.send(chunk)
                else:
                    await channel.send("No sessions still running.")
        await asyncio.sleep(IDLE_CHECK_INTERVAL_SECONDS)


@client.event
async def on_ready():
    global _reaper_task
    print(f"Logged in as {client.user} (id={client.user.id})")
    print(f"Watching channel {ALLOWED_CHANNEL_ID} for user {ALLOWED_USER_ID}")
    print(f"GIT_ROOT = {GIT_ROOT}")
    print(f"Device name: {DEVICE_NAME}  Known devices: {sorted(KNOWN_DEVICES)}")
    print(f"Idle session timeout: {IDLE_TIMEOUT_HOURS:.0f}h")
    if _reaper_task is None or _reaper_task.done():
        _reaper_task = client.loop.create_task(idle_reaper_loop())


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.author.id != ALLOWED_USER_ID or message.channel.id != ALLOWED_CHANNEL_ID:
        return

    raw = message.content.strip()
    channel_id = message.channel.id
    raw_lower = raw.lower()
    first_word = raw_lower.split()[0] if raw_lower.split() else ""

    # Bare device name → activate this Pi and wait for commands.
    if raw_lower == DEVICE_NAME.lower():
        activate(channel_id)
        await message.channel.send(f"Ready — **{DEVICE_NAME}** active. Type another device name to switch.")
        return

    # Prefixed command (e.g. "pi2 !repos") → activate and strip prefix.
    if raw_lower.startswith(DEVICE_NAME.lower() + " "):
        activate(channel_id)
        content = raw[len(DEVICE_NAME):].strip()

    # Another known device was addressed → step back silently.
    elif first_word in KNOWN_DEVICES:
        active_until.pop(channel_id, None)
        return

    # Not active and not addressed → only announce on help/devices.
    elif not is_active(channel_id):
        if raw_lower in HELP_COMMANDS | DEVICE_COMMANDS:
            await message.channel.send(
                f"**{DEVICE_NAME}** available — type `{DEVICE_NAME}` to activate."
            )
        return

    else:
        content = raw

    if content.lower() in HELP_COMMANDS:
        await message.channel.send(
            f"**{DEVICE_NAME}**\n"
            "```\n"
            "!repos              List available git repos\n"
            "<number>            Launch a session for that repo\n"
            "<github url>        Clone repo and launch a session\n"
            "!new <name>         Create a new repo and launch a session\n"
            "!status             List running sessions\n"
            "!kill <number>      Kill a session by number (from !status)\n"
            "!kill <name>        Kill a session by name\n"
            "!activity           Show last activity time per session\n"
            "!stats              Show Pi CPU / RAM / disk / temp / uptime\n"
            "!bash <command>     Run a raw shell command on the Pi\n"
            "!help               Show this message\n"
            "<device-name>       Switch to a different device\n"
            "```\n"
            f"Sessions idle {IDLE_TIMEOUT_HOURS:.0f}+ hours are auto-killed.\n"
            "Active device stays selected until you type another device name."
        )
        return

    if content.lower() in ACTIVITY_COMMANDS:
        await message.channel.send(await asyncio.to_thread(session_activity_report))
        return

    if content.lower() in STATS_COMMANDS:
        await message.channel.send(await asyncio.to_thread(pi_stats))
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
    if command.lower() in BASH_COMMANDS:
        raw = rest.strip()
        if not raw:
            await message.channel.send("Usage: `!bash <command>`")
            return
        reply = await asyncio.to_thread(run_bash_command, raw)
        await message.channel.send(reply)
        return

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
            reply, auth = await create_and_launch(name, ch_name, guild_name)
        except subprocess.CalledProcessError as exc:
            reply, auth = f"Failed to create project: {exc}", None
        await message.channel.send(reply)
        if auth:
            await message.channel.send(f"**Authorization required:** {auth}")
        return

    if GITHUB_URL_RE.match(content):
        await message.channel.send("Cloning…")
        try:
            reply, auth = await clone_and_launch(content, ch_name, guild_name)
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
            reply, auth = await launch_remote_control(repo_path, ch_name, guild_name)
        except subprocess.CalledProcessError as exc:
            reply, auth = f"Failed to start session: {exc}", None
        await message.channel.send(reply)
        if auth:
            await message.channel.send(f"**Authorization required:** {auth}")
        return


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
