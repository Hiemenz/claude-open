"""
Slack bot for managing programs and Claude sessions on this Pi.

Panel (@mention the bot):
  - Per-program status with Start / Stop / Restart / Logs buttons
  - Claude sessions with Kill buttons
  - Repo launcher (dropdown + Launch, Clone GitHub modal)
  - Stats and Bash modal

Text commands (fallback for everything in the panel):
  !status              List managed programs + state
  !start <name>        Start a program
  !stop  <name>        Stop a program
  !restart <name>      Restart a program
  !logs  <name>        Last 20 lines of logs
  !repos               List git repos
  <number>             Launch Claude session for that repo
  <github url>         Clone repo and launch session
  !new <name>          Create new repo and launch session
  !sessions            List running Claude tmux sessions
  !kill <n|name>       Kill a Claude session
  !activity            Last-active times per session
  !stats               Pi CPU / RAM / disk / temp / uptime
  !bash <command>      Run a raw shell command
  !help                Show this message

Configure programs in programs.toml (see programs.toml.example).
Only responds to one Slack user in one Slack channel (see .env).
"""

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from hiemenz_utils.slack_notify import notify_error as _slack_notify_error

load_dotenv()


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(
            f"Missing required environment variable {name}. "
            "Copy .env.example to .env and fill it in."
        )
    return value


SLACK_BOT_TOKEN = require_env("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = require_env("SLACK_APP_TOKEN")
ALLOWED_USER_ID = os.environ.get("SLACK_ALLOWED_USER_ID", "")  # blank = anyone in the channel
ALLOWED_CHANNEL_ID = require_env("SLACK_ALLOWED_CHANNEL_ID")
GIT_ROOT = Path(os.environ.get("GIT_ROOT", str(Path.home() / "git"))).expanduser().resolve()
PROGRAMS_FILE = Path(
    os.environ.get("PROGRAMS_FILE", str(Path(__file__).parent / "programs.toml"))
)
LOG_LINES = 20

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
SESSION_COMMANDS = {"!sessions", "!ps"}
KILL_COMMANDS = {"!kill"}
PROGRAMS_COMMANDS = {"!status", "!programs"}
START_COMMANDS = {"!start"}
STOP_COMMANDS = {"!stop"}
RESTART_COMMANDS = {"!restart"}
LOGS_COMMANDS = {"!logs"}
NEW_COMMANDS = {"!new", "!create", "!init"}
STATS_COMMANDS = {"!stats", "!stat", "!pi", "!sys"}
BASH_COMMANDS = {"!bash", "!sh", "!exec"}
HELP_COMMANDS = {"!help", "!h", "!?"}
ACTIVITY_COMMANDS = {"!activity", "!idle", "!when"}

BASH_TIMEOUT_SECONDS = float(os.environ.get("BASH_TIMEOUT_SECONDS", "60"))

_pane_snapshots: dict[str, tuple[str, float]] = {}
_login_alert_sent = False


def _post_error(message: str, exc: BaseException | None = None) -> None:
    _slack_notify_error(message, exc, error_key=f"{DEVICE_NAME}:{message[:60]}")


# ---------------------------------------------------------------------------
# Claude session logic
# ---------------------------------------------------------------------------

def list_repos() -> list[Path]:
    if not GIT_ROOT.is_dir():
        return []
    repos = [p for p in GIT_ROOT.iterdir() if p.is_dir() and (p / ".git").exists()]
    return sorted(repos, key=lambda p: p.name.lower())


def _format_session_list_text(sessions: list[str]) -> str:
    lines = [f"• {s}" for s in sessions]
    return "Running sessions:\n" + "\n".join(lines)


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
        claude_str = (
            datetime.fromtimestamp(claude_mtime).strftime("%Y-%m-%d %H:%M")
            if claude_mtime
            else "—"
        )
        lines.append(
            f"{name:<32} {_format_idle(hours):<16} ({overall_dt})  claude: {claude_str}"
        )
    return "```\n" + "\n".join(lines) + "\n```"


def sanitize_session_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "-", name)
    return cleaned or "claude-session"


def _repo_session_name(repo_name: str) -> str:
    pascal = "".join(word.capitalize() for word in re.split(r"[-_\s]+", repo_name) if word)
    return DEVICE_NAME + "-" + pascal


def tmux_session_exists(session_name: str) -> bool:
    return (
        subprocess.run(
            [TMUX_BIN, "has-session", "-t", session_name], capture_output=True
        ).returncode
        == 0
    )


def list_active_sessions() -> list[str]:
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
        return f"No running session named *{session_name}*."
    result = subprocess.run([TMUX_BIN, "kill-session", "-t", session_name], capture_output=True)
    if result.returncode != 0:
        return f"Session *{session_name}* already stopped."
    return f"Stopped session *{session_name}*."


def _update_pane_snapshot(session_name: str) -> float:
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
    pane = subprocess.run(
        [TMUX_BIN, "display-message", "-t", session_name, "-p", "#{pane_current_path}"],
        capture_output=True,
        text=True,
    )
    if pane.returncode != 0 or not pane.stdout.strip():
        return None
    work_dir = pane.stdout.strip()
    project_dir = Path.home() / ".claude" / "projects" / work_dir.replace("/", "-")
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
    killed = []
    for name, hours in list_session_idle_hours().items():
        if hours >= timeout_hours and not _session_has_live_claude(name):
            subprocess.run([TMUX_BIN, "kill-session", "-t", name], capture_output=True)
            killed.append(name)
    return killed


def _session_has_live_claude(session_name: str) -> bool:
    pane = subprocess.run(
        [TMUX_BIN, "list-panes", "-t", session_name, "-F", "#{pane_pid}"],
        capture_output=True,
        text=True,
    )
    if pane.returncode != 0 or not pane.stdout.strip():
        return False
    pane_pid = pane.stdout.strip().split()[0]
    return subprocess.run(["pgrep", "-P", pane_pid], capture_output=True).returncode == 0


AUTH_URL_RE = re.compile(r"https?://\S*(auth|login|oauth|authorize)\S*", re.IGNORECASE)
SESSION_URL_RE = re.compile(r"https://claude\.ai/code/session_\S+")
TRUST_PROMPT = "Is this a project you created or one you trust"


def _extract_session_url(session_name: str, timeout: float = 15.0) -> str | None:
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
        time.sleep(0.5)
    return None


def _extract_auth_prompt(session_name: str, timeout: float = 5.0) -> str | None:
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
        time.sleep(0.5)
    return None


def claude_login_status(timeout: float = 10.0) -> tuple[bool | None, str | None]:
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "auth", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None, None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, None
    return data.get("loggedIn"), data.get("email")


def launch_remote_control(
    repo_path: Path,
    channel_name: str = "",
) -> tuple[str, str | None]:
    session_name = _repo_session_name(repo_path.name)
    source = f"#{channel_name}" if channel_name else "Slack"
    device_tag = f"on *{DEVICE_NAME}*"

    if tmux_session_exists(session_name):
        url = _extract_session_url(session_name, timeout=3.0)
        if url:
            return (
                f"Session *{session_name}* is already running in `{repo_path}` "
                f"({source}, {device_tag})\n{url}",
                None,
            )
        if _session_has_live_claude(session_name):
            return (
                f"Session *{session_name}* is running ({source}, {device_tag}) "
                "— pick it up in the Claude app / claude.ai/code.",
                None,
            )
        subprocess.run([TMUX_BIN, "kill-session", "-t", session_name], capture_output=True)

    subprocess.run(
        [
            TMUX_BIN, "new-session", "-d", "-s", session_name,
            "-c", str(repo_path),
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
        if not auth:
            logged_in, _ = claude_login_status()
            if logged_in is False:
                auth = (
                    f"not logged in on *{DEVICE_NAME}* — run `claude auth login` "
                    "on the device, then try again."
                )
        suffix = " — pick it up in the Claude app / claude.ai/code."

    return (
        f"Started remote-control session *{session_name}* in `{repo_path}` "
        f"({source}, {device_tag}){suffix}",
        auth,
    )


def run_bash_command(command: str, timeout: float = BASH_TIMEOUT_SECONDS) -> str:
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
    return f"{prefix}```\n{output}\n```"


def _week_progress() -> str:
    """Percentage of the week elapsed since Tuesday 11 PM (resets weekly)."""
    now = datetime.now()
    # Find the most recent Tuesday at 23:00
    days_since_tuesday = (now.weekday() - 1) % 7  # Tuesday = weekday 1
    reset = now.replace(hour=23, minute=0, second=0, microsecond=0) - timedelta(days=days_since_tuesday)
    if now < reset:
        reset -= timedelta(weeks=1)
    elapsed = (now - reset).total_seconds()
    week_seconds = 7 * 24 * 3600
    pct = elapsed / week_seconds * 100
    bar_len = 20
    filled = round(pct / 100 * bar_len)
    bar = "█" * filled + "░" * (bar_len - filled)
    return f"Week:  {bar} {pct:.1f}%"


def pi_stats() -> str:
    lines = [f"Host:  {DEVICE_NAME}"]
    cpu = subprocess.run(["top", "-bn1"], capture_output=True, text=True).stdout
    for line in cpu.splitlines():
        if "Cpu(s)" in line or "cpu(s)" in line.lower():
            idle = re.search(r"([\d.]+)\s*id", line)
            if idle:
                lines.append(f"CPU:   {100.0 - float(idle.group(1)):.1f}%")
            break
    mem = subprocess.run(["free", "-h"], capture_output=True, text=True).stdout
    for line in mem.splitlines():
        if line.startswith("Mem:"):
            parts = line.split()
            lines.append(f"RAM:   {parts[2]} / {parts[1]} used")
            break
    disk = subprocess.run(["df", "-h", "/"], capture_output=True, text=True).stdout
    for line in disk.splitlines()[1:]:
        parts = line.split()
        lines.append(f"Disk:  {parts[2]} / {parts[1]} used ({parts[4]})")
        break
    temp = subprocess.run(["vcgencmd", "measure_temp"], capture_output=True, text=True)
    if temp.returncode == 0:
        lines.append(f"Temp:  {temp.stdout.strip().replace('temp=', '')}")
    up = subprocess.run(["uptime", "-p"], capture_output=True, text=True)
    load = subprocess.run(["uptime"], capture_output=True, text=True)
    if up.returncode == 0:
        lines.append(f"Up:    {up.stdout.strip()}")
    m = re.search(r"load average[s]?:\s*([\d.]+)", load.stdout)
    if m:
        lines.append(f"Load:  {m.group(1)} (1m avg)")
    lines.append(_week_progress())
    return "```\n" + "\n".join(lines) + "\n```"


def create_and_launch(name: str, channel_name: str = "") -> tuple[str, str | None]:
    safe = sanitize_session_name(name)
    dest = GIT_ROOT / safe
    if not dest.exists():
        dest.mkdir(parents=True)
        subprocess.run([GIT_BIN, "init", str(dest)], capture_output=True, check=True)
    return launch_remote_control(dest, channel_name)


def clone_and_launch(github_url: str, channel_name: str = "") -> tuple[str, str | None]:
    m = GITHUB_URL_RE.match(github_url)
    if not m:
        return ("That doesn't look like a valid GitHub URL.", None)
    repo_name = m.group("repo")
    dest = GIT_ROOT / repo_name
    if not dest.exists():
        result = subprocess.run(
            [GIT_BIN, "clone", github_url, str(dest)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return (f"Clone failed:\n```\n{result.stderr.strip()}\n```", None)
    return launch_remote_control(dest, channel_name)


# ---------------------------------------------------------------------------
# Program management  (programs.toml)
# ---------------------------------------------------------------------------

def _load_programs() -> dict:
    """Load programs.toml; returns {} if the file is absent or malformed."""
    if not PROGRAMS_FILE.is_file():
        return {}
    try:
        with open(PROGRAMS_FILE, "rb") as fh:
            return tomllib.load(fh)
    except Exception:
        return {}


def _systemctl(action: str, prog: dict) -> subprocess.CompletedProcess:
    service = prog.get("service", "")
    if prog.get("user_service"):
        cmd = ["systemctl", "--user", action, service]
    else:
        cmd = ["sudo", "systemctl", action, service]
    return subprocess.run(cmd, capture_output=True, text=True)


def _program_is_running(name: str, prog: dict) -> bool:
    if prog.get("type") == "systemd":
        return _systemctl("is-active", prog).stdout.strip() == "active"
    pattern = prog.get("check") or prog.get("command") or prog.get("start", "")
    if not pattern:
        return False
    return subprocess.run(["pgrep", "-f", pattern.split()[0]], capture_output=True).returncode == 0


def _program_state_str(name: str, prog: dict) -> str:
    """Returns a short state label (e.g. 'active', 'running', 'stopped')."""
    if prog.get("type") == "systemd":
        return _systemctl("is-active", prog).stdout.strip() or "unknown"
    return "running" if _program_is_running(name, prog) else "stopped"


def format_programs_status(programs: dict) -> str:
    if not programs:
        return "No programs configured — add a `programs.toml` to this repo."
    lines = []
    for name, prog in programs.items():
        running = _program_is_running(name, prog)
        dot = ":large_green_circle:" if running else ":red_circle:"
        desc = prog.get("description", name)
        state = _program_state_str(name, prog)
        lines.append(f"{dot} *{name}* — {desc}  _{state}_")
    return "\n".join(lines)


def start_program(name: str, prog: dict) -> str:
    if prog.get("type") == "systemd":
        result = _systemctl("start", prog)
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip()
            raise subprocess.CalledProcessError(
                result.returncode, f"systemctl start {prog.get('service')}", err
            )
        return f"Started *{name}*."
    cmd = prog.get("command") or prog.get("start")
    if not cmd:
        return f"No command configured for *{name}*."
    subprocess.Popen(
        cmd, shell=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return f"Triggered *{name}*."


def stop_program(name: str, prog: dict) -> str:
    if prog.get("type") == "systemd":
        result = _systemctl("stop", prog)
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip()
            raise subprocess.CalledProcessError(
                result.returncode, f"systemctl stop {prog.get('service')}", err
            )
        return f"Stopped *{name}*."
    cmd = prog.get("command") or prog.get("start", "")
    if not cmd:
        return f"No command configured for *{name}*."
    result = subprocess.run(["pkill", "-f", cmd.split()[0]], capture_output=True)
    return f"Stopped *{name}*." if result.returncode == 0 else f"*{name}* was not running."


def get_program_logs(name: str, prog: dict, lines: int = LOG_LINES) -> str:
    if prog.get("type") == "systemd":
        service = prog.get("service", name)
        result = subprocess.run(
            ["journalctl", "-u", service, f"-n{lines}", "--no-pager"],
            capture_output=True,
            text=True,
        )
        output = result.stdout.strip() or "(no logs)"
    else:
        log_file = prog.get("log")
        if not log_file:
            return f"No `log` path configured for *{name}* in `programs.toml`."
        result = subprocess.run(
            ["tail", f"-n{lines}", log_file], capture_output=True, text=True
        )
        if result.returncode != 0:
            return f"Could not read `{log_file}` for *{name}*."
        output = result.stdout.strip() or "(empty log)"
    return f"*{name}* — last {lines} lines:\n```\n{output}\n```"


# ---------------------------------------------------------------------------
# Message routing  (tested independently of slack-bolt wiring)
# ---------------------------------------------------------------------------

def _handle_message_content(content: str, channel_id: str, say) -> None:
    raw_lower = content.lower()

    if raw_lower in HELP_COMMANDS:
        say(text=(
            f"*{DEVICE_NAME}*\n"
            "```\n"
            "Programs\n"
            "  !status              List managed programs + state\n"
            "  !start <name>        Start a program\n"
            "  !stop  <name>        Stop a program\n"
            "  !restart <name>      Restart a program\n"
            "  !logs  <name>        Last 20 lines of logs\n"
            "\n"
            "Claude sessions\n"
            "  !repos               List git repos\n"
            "  <number>             Launch session for that repo\n"
            "  <github url>         Clone repo and launch session\n"
            "  !new <name>          Create repo and launch session\n"
            "  !sessions            List running sessions\n"
            "  !kill <n|name>       Kill a session\n"
            "  !activity            Last-active times per session\n"
            "\n"
            "System\n"
            "  !stats               Pi CPU / RAM / disk / temp / uptime\n"
            "  !bash <command>      Run a raw shell command\n"
            "  !help                Show this message\n"
            "```\n"
            f"Sessions idle {IDLE_TIMEOUT_HOURS:.0f}+ hours are auto-killed."
        ))
        return

    if raw_lower in PROGRAMS_COMMANDS:
        say(text=format_programs_status(_load_programs()))
        return

    if raw_lower in ACTIVITY_COMMANDS:
        say(text=session_activity_report())
        return

    if raw_lower in STATS_COMMANDS:
        say(text=pi_stats())
        return

    if raw_lower in LIST_COMMANDS:
        repos = list_repos()
        if not repos:
            say(text=f"No git repos found under `{GIT_ROOT}`.")
            return
        options = [{"text": {"type": "plain_text", "text": r.name}, "value": str(r)} for r in repos]
        say(blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Launch a Claude session*"}},
            {
                "type": "section",
                "block_id": "inline_repo_picker",
                "text": {"type": "mrkdwn", "text": "Pick a repo:"},
                "accessory": {
                    "type": "static_select",
                    "action_id": "inline_repo_select",
                    "placeholder": {"type": "plain_text", "text": "Select…"},
                    "initial_option": options[0],
                    "options": options,
                },
            },
            {
                "type": "actions",
                "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "Launch"}, "action_id": "inline_repo_launch", "style": "primary"},
                    {"type": "button", "text": {"type": "plain_text", "text": "Cancel"}, "action_id": "inline_dismiss"},
                ],
            },
        ], text="Launch a Claude session")
        return

    if raw_lower in SESSION_COMMANDS:
        sessions = list_active_sessions()
        if not sessions:
            say(text="No sessions running.")
            return
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*Running Claude Sessions*"}}]
        for name in sessions:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"⚡ *{name}*"},
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Kill"},
                    "action_id": f"inline_session_kill:{name}",
                    "style": "danger",
                },
            })
        say(blocks=blocks, text="Running Claude sessions")
        return

    command, _, rest = content.partition(" ")
    cmd_lower = command.lower()

    if cmd_lower in START_COMMANDS:
        name = rest.strip().lower()
        if not name:
            say(text="Usage: `!start <program-name>`")
            return
        programs = _load_programs()
        prog = programs.get(name)
        if not prog:
            say(text=f"Unknown program `{name}`. Check `programs.toml`.")
            return
        try:
            reply = start_program(name, prog)
        except subprocess.CalledProcessError as exc:
            _post_error(f"Failed to start {name}", exc)
            reply = f"Failed to start `{name}`: {exc}"
        say(text=reply)
        return

    if cmd_lower in STOP_COMMANDS:
        name = rest.strip().lower()
        if not name:
            say(text="Usage: `!stop <program-name>`")
            return
        programs = _load_programs()
        prog = programs.get(name)
        if not prog:
            say(text=f"Unknown program `{name}`. Check `programs.toml`.")
            return
        try:
            reply = stop_program(name, prog)
        except subprocess.CalledProcessError as exc:
            _post_error(f"Failed to stop {name}", exc)
            reply = f"Failed to stop `{name}`: {exc}"
        say(text=reply)
        return

    if cmd_lower in RESTART_COMMANDS:
        name = rest.strip().lower()
        if not name:
            say(text="Usage: `!restart <program-name>`")
            return
        programs = _load_programs()
        prog = programs.get(name)
        if not prog:
            say(text=f"Unknown program `{name}`. Check `programs.toml`.")
            return
        try:
            if prog.get("type") == "systemd":
                result = _systemctl("restart", prog)
                if result.returncode != 0:
                    err = result.stderr.strip() or result.stdout.strip()
                    raise subprocess.CalledProcessError(result.returncode, "systemctl restart", err)
                reply = f"Restarted *{name}*."
            else:
                stop_program(name, prog)
                time.sleep(1)
                reply = start_program(name, prog)
        except subprocess.CalledProcessError as exc:
            _post_error(f"Failed to restart {name}", exc)
            reply = f"Failed to restart `{name}`: {exc}"
        say(text=reply)
        return

    if cmd_lower in LOGS_COMMANDS:
        name = rest.strip().lower()
        if not name:
            say(text="Usage: `!logs <program-name>`")
            return
        programs = _load_programs()
        prog = programs.get(name)
        if not prog:
            say(text=f"Unknown program `{name}`. Check `programs.toml`.")
            return
        say(text=get_program_logs(name, prog))
        return

    if cmd_lower in BASH_COMMANDS:
        raw_cmd = rest.strip()
        if not raw_cmd:
            say(text="Usage: `!bash <command>`")
            return
        say(text=run_bash_command(raw_cmd))
        return

    if cmd_lower in KILL_COMMANDS:
        arg = rest.strip()
        if not arg:
            say(text="Usage: `!kill <name>`  (use `!sessions` to see running sessions)")
            return
        try:
            reply = kill_session(sanitize_session_name(arg))
        except subprocess.CalledProcessError as exc:
            _post_error("Failed to stop session", exc)
            reply = f"Failed to stop session: {exc}"
        say(text=reply)
        return

    if cmd_lower in NEW_COMMANDS:
        name = rest.strip()
        if not name:
            say(text="Usage: `!new <project-name>`")
            return
        try:
            reply, auth = create_and_launch(name)
        except subprocess.CalledProcessError as exc:
            _post_error("Failed to create project", exc)
            reply, auth = f"Failed to create project: {exc}", None
        say(text=reply)
        if auth:
            say(text=f"*Authorization required:* {auth}")
        return

    if GITHUB_URL_RE.match(content):
        say(text="Cloning…")
        try:
            reply, auth = clone_and_launch(content)
        except subprocess.CalledProcessError as exc:
            _post_error("Failed to start session", exc)
            reply, auth = f"Failed to start session: {exc}", None
        say(text=reply)
        if auth:
            say(text=f"*Authorization required:* {auth}")
        return


# ---------------------------------------------------------------------------
# Background reaper thread
# ---------------------------------------------------------------------------

def _idle_reaper_loop(slack_client) -> None:
    global _login_alert_sent
    while True:
        time.sleep(IDLE_CHECK_INTERVAL_SECONDS)
        try:
            _reaper_tick(slack_client)
        except Exception as exc:
            _post_error("Unhandled error in idle reaper", exc)


def _reaper_tick(slack_client) -> None:
    global _login_alert_sent
    for name in list_active_sessions():
        _update_pane_snapshot(name)
    killed = reap_idle_sessions()
    if killed:
        names = ", ".join(f"*{n}*" for n in killed)
        slack_client.chat_postMessage(
            channel=ALLOWED_CHANNEL_ID,
            text=(
                f"Cleaned up {len(killed)} session(s) idle "
                f"{IDLE_TIMEOUT_HOURS:.0f}+ hours: {names}"
            ),
        )
        remaining = list_active_sessions()
        slack_client.chat_postMessage(
            channel=ALLOWED_CHANNEL_ID,
            text=_format_session_list_text(remaining) if remaining else "No sessions still running.",
        )

    logged_in, _ = claude_login_status()
    if logged_in is False and not _login_alert_sent:
        _login_alert_sent = True
        slack_client.chat_postMessage(
            channel=ALLOWED_CHANNEL_ID,
            text=(
                f"*{DEVICE_NAME}* is not logged in to Claude — run "
                "`claude auth login` on the device to log in."
            ),
        )
    elif logged_in is True:
        _login_alert_sent = False


def _send_startup_message(slack_client) -> None:
    slack_client.chat_postMessage(
        channel=ALLOWED_CHANNEL_ID,
        text=f"*{DEVICE_NAME}* is online and activated to Slack.",
    )
    logged_in, _ = claude_login_status()
    if logged_in is False:
        slack_client.chat_postMessage(
            channel=ALLOWED_CHANNEL_ID,
            text=(
                f"*{DEVICE_NAME}* is not logged in to Claude — run "
                "`claude auth login` on the device to log in."
            ),
        )


# ---------------------------------------------------------------------------
# Block Kit helpers
# ---------------------------------------------------------------------------

def _programs_blocks(programs: dict) -> list:
    """Build per-program status rows with Start/Stop/Restart/Logs buttons."""
    if not programs:
        return []
    blocks: list[dict] = [
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Programs*"}},
    ]
    for name, prog in list(programs.items())[:8]:  # cap at 8 to stay under block limit
        running = _program_is_running(name, prog)
        dot = ":large_green_circle:" if running else ":red_circle:"
        desc = prog.get("description", name)
        state = _program_state_str(name, prog)
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{dot} *{name}* — {desc}  _{state}_"},
        })
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Start"},
                    "action_id": f"prog_start:{name}",
                    "style": "primary",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Stop"},
                    "action_id": f"prog_stop:{name}",
                    "style": "danger",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Restart"},
                    "action_id": f"prog_restart:{name}",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Logs"},
                    "action_id": f"prog_logs:{name}",
                },
            ],
        })
    return blocks


def _repos_blocks(repos: list[Path], sessions: list[str]) -> list:
    """One button per repo: repo name in the button text, Kill (red) or Launch (green)."""
    session_set = set(sessions)
    blocks: list[dict] = [
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Repos*"}},
    ]
    if not repos:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"_No repos found in `{GIT_ROOT}`_"},
        })
        return blocks
    # Two buttons per actions row to save vertical space on mobile
    repo_list = repos[:24]
    for i in range(0, len(repo_list), 2):
        pair = repo_list[i:i + 2]
        elements = []
        for repo in pair:
            session_name = _repo_session_name(repo.name)
            running = session_name in session_set
            if running:
                elements.append({
                    "type": "button",
                    "text": {"type": "plain_text", "text": f"⏹ {repo.name}"},
                    "action_id": f"session_kill:{session_name}",
                    "style": "danger",
                })
            else:
                elements.append({
                    "type": "button",
                    "text": {"type": "plain_text", "text": f"▶ {repo.name}"},
                    "action_id": f"repo_launch_named:{repo}",
                    "style": "primary",
                })
        blocks.append({"type": "actions", "elements": elements})
    return blocks


# ---------------------------------------------------------------------------
# Bot setup  (deferred so importing this module doesn't require slack-bolt)
# ---------------------------------------------------------------------------

def main() -> None:
    from hiemenz_utils.slack_bot import SlackBot
    from slack_sdk import WebClient

    slack_client = WebClient(token=SLACK_BOT_TOKEN)
    bot = SlackBot(DEVICE_NAME)

    # ---- Panel blocks -------------------------------------------------------

    @bot.add_blocks
    def _panel_blocks():
        programs = _load_programs()
        sessions = list_active_sessions()
        repos = list_repos()
        blocks = []
        blocks.extend(_repos_blocks(repos, sessions))
        blocks.extend(_programs_blocks(programs))
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "📊 Stats"}, "action_id": "bot_stats"},
                {"type": "button", "text": {"type": "plain_text", "text": "⚡ Activity"}, "action_id": "bot_activity"},
                {"type": "button", "text": {"type": "plain_text", "text": "💻 Bash…"}, "action_id": "bash_modal_open"},
                {"type": "button", "text": {"type": "plain_text", "text": "📁 New Project…"}, "action_id": "new_project_modal_open"},
                {"type": "button", "text": {"type": "plain_text", "text": "🔗 Clone…"}, "action_id": "clone_modal_open"},
                {"type": "button", "text": {"type": "plain_text", "text": "🔄 Refresh"}, "action_id": "panel_refresh"},
            ],
        })
        return blocks

    # ---- Program actions ---------------------------------------------------

    @bot.action(re.compile(r"prog_start:.+"))
    def _prog_start(ack, body, client, say):
        ack()
        name = body["actions"][0]["action_id"].split(":", 1)[1]
        prog = _load_programs().get(name)
        if not prog:
            say(text=f"Program `{name}` not found in `programs.toml`.")
            return
        try:
            reply = start_program(name, prog)
        except subprocess.CalledProcessError as exc:
            _post_error(f"Failed to start {name}", exc)
            reply = f"Failed to start `{name}`: {exc}"
        say(text=reply)
        bot.update_message(client, body["channel"]["id"], body["message"]["ts"])

    @bot.action(re.compile(r"prog_stop:.+"))
    def _prog_stop(ack, body, client, say):
        ack()
        name = body["actions"][0]["action_id"].split(":", 1)[1]
        prog = _load_programs().get(name)
        if not prog:
            say(text=f"Program `{name}` not found in `programs.toml`.")
            return
        try:
            reply = stop_program(name, prog)
        except subprocess.CalledProcessError as exc:
            _post_error(f"Failed to stop {name}", exc)
            reply = f"Failed to stop `{name}`: {exc}"
        say(text=reply)
        bot.update_message(client, body["channel"]["id"], body["message"]["ts"])

    @bot.action(re.compile(r"prog_restart:.+"))
    def _prog_restart(ack, body, client, say):
        ack()
        name = body["actions"][0]["action_id"].split(":", 1)[1]
        prog = _load_programs().get(name)
        if not prog:
            say(text=f"Program `{name}` not found in `programs.toml`.")
            return
        try:
            if prog.get("type") == "systemd":
                result = _systemctl("restart", prog)
                if result.returncode != 0:
                    raise subprocess.CalledProcessError(
                        result.returncode, "systemctl restart",
                        result.stderr.strip() or result.stdout.strip()
                    )
                reply = f"Restarted *{name}*."
            else:
                stop_program(name, prog)
                time.sleep(1)
                reply = start_program(name, prog)
        except subprocess.CalledProcessError as exc:
            _post_error(f"Failed to restart {name}", exc)
            reply = f"Failed to restart `{name}`: {exc}"
        say(text=reply)
        bot.update_message(client, body["channel"]["id"], body["message"]["ts"])

    @bot.action(re.compile(r"prog_logs:.+"))
    def _prog_logs(ack, body, client, say):
        ack()
        name = body["actions"][0]["action_id"].split(":", 1)[1]
        prog = _load_programs().get(name)
        if not prog:
            say(text=f"Program `{name}` not found in `programs.toml`.")
            return
        say(text=get_program_logs(name, prog))

    # ---- Session actions ---------------------------------------------------

    @bot.action(re.compile(r"session_kill:.+"))
    def _session_kill(ack, body, client, say):
        ack()
        name = body["actions"][0]["action_id"].split(":", 1)[1]
        try:
            reply = kill_session(name)
        except subprocess.CalledProcessError as exc:
            _post_error("Failed to stop session", exc)
            reply = f"Failed to stop session: {exc}"
        say(text=reply)
        bot.update_message(client, body["channel"]["id"], body["message"]["ts"])

    # ---- Repo launch -------------------------------------------------------

    @bot.action(re.compile(r"repo_launch_named:.+"))
    def _repo_launch_named(ack, body, client, say):
        ack()
        path = body["actions"][0]["action_id"].split(":", 1)[1]
        try:
            reply, auth = launch_remote_control(Path(path))
        except subprocess.CalledProcessError as exc:
            _post_error("Failed to start session", exc)
            reply, auth = f"Failed to start session: {exc}", None
        say(text=reply)
        if auth:
            say(text=f"*Authorization required:* {auth}")
        bot.update_message(client, body["channel"]["id"], body["message"]["ts"])

    @bot.action("clone_modal_open")
    def _clone_modal_open(ack, body, client):
        ack()
        client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal",
                "callback_id": "clone_modal",
                "title": {"type": "plain_text", "text": "Clone GitHub Repo"},
                "submit": {"type": "plain_text", "text": "Clone & Launch"},
                "close": {"type": "plain_text", "text": "Cancel"},
                "blocks": [
                    {
                        "type": "input",
                        "block_id": "github_url_block",
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "github_url",
                            "placeholder": {
                                "type": "plain_text",
                                "text": "https://github.com/owner/repo",
                            },
                        },
                        "label": {"type": "plain_text", "text": "GitHub URL"},
                    }
                ],
            },
        )

    # ---- System actions ---------------------------------------------------

    @bot.action("bot_stats")
    def _bot_stats(ack, body, client, say):
        ack()
        say(text=pi_stats())
        bot.update_message(client, body["channel"]["id"], body["message"]["ts"])

    @bot.action("bot_activity")
    def _bot_activity(ack, body, client, say):
        ack()
        say(text=session_activity_report())
        bot.update_message(client, body["channel"]["id"], body["message"]["ts"])

    @bot.action("inline_repo_select")
    def _inline_repo_select(ack):
        ack()

    @bot.action("inline_repo_launch")
    def _inline_repo_launch(ack, body, client):
        ack()
        selected = (
            body.get("state", {})
            .get("values", {})
            .get("inline_repo_picker", {})
            .get("inline_repo_select", {})
            .get("selected_option", {})
            .get("value")
        )
        if not selected:
            repos = list_repos()
            selected = str(repos[0]) if repos else None
        if not selected:
            client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], text="No repos available.", blocks=[])
            return
        try:
            reply, auth = launch_remote_control(Path(selected))
        except subprocess.CalledProcessError as exc:
            _post_error("Failed to start session", exc)
            reply, auth = f"Failed to start session: {exc}", None
        client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], text=reply, blocks=[])
        if auth:
            client.chat_postMessage(channel=body["channel"]["id"], text=f"*Authorization required:* {auth}")

    @bot.action("inline_dismiss")
    def _inline_dismiss(ack, body, client):
        ack()
        client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], text="Dismissed.", blocks=[])

    @bot.action(re.compile(r"inline_session_kill:.+"))
    def _inline_session_kill(ack, body, client):
        ack()
        name = body["actions"][0]["action_id"].split(":", 1)[1]
        try:
            reply = kill_session(name)
        except subprocess.CalledProcessError as exc:
            _post_error("Failed to stop session", exc)
            reply = f"Failed to stop session: {exc}"
        client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], text=reply, blocks=[])

    @bot.action("bash_modal_open")
    def _bash_modal_open(ack, body, client):
        ack()
        client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal",
                "callback_id": "bash_modal",
                "title": {"type": "plain_text", "text": "Run Bash Command"},
                "submit": {"type": "plain_text", "text": "Run"},
                "close": {"type": "plain_text", "text": "Cancel"},
                "blocks": [
                    {
                        "type": "input",
                        "block_id": "bash_input_block",
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "bash_input",
                            "placeholder": {"type": "plain_text", "text": "e.g. df -h"},
                        },
                        "label": {"type": "plain_text", "text": "Command"},
                    }
                ],
            },
        )

    @bot.action("new_project_modal_open")
    def _new_project_modal_open(ack, body, client):
        ack()
        client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal",
                "callback_id": "new_project_modal",
                "title": {"type": "plain_text", "text": "New Project"},
                "submit": {"type": "plain_text", "text": "Create & Launch"},
                "close": {"type": "plain_text", "text": "Cancel"},
                "blocks": [
                    {
                        "type": "input",
                        "block_id": "project_name_block",
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "project_name",
                            "placeholder": {"type": "plain_text", "text": "e.g. my-new-project"},
                        },
                        "label": {"type": "plain_text", "text": "Project name"},
                        "hint": {"type": "plain_text", "text": "Creates a new git repo under ~/git/ and launches a Claude session."},
                    }
                ],
            },
        )

    @bot.action("panel_refresh")
    def _panel_refresh(ack, body, client):
        ack()
        bot.update_message(client, body["channel"]["id"], body["message"]["ts"])

    # ---- Modal submissions ------------------------------------------------

    app = bot._get_app()

    @app.view("bash_modal")
    def _bash_submit(ack, body, client):
        ack()
        cmd = (
            body["view"]["state"]["values"]
            ["bash_input_block"]["bash_input"]["value"]
        )
        client.chat_postMessage(channel=ALLOWED_CHANNEL_ID, text=run_bash_command(cmd))

    @app.view("new_project_modal")
    def _new_project_submit(ack, body, client):
        ack()
        name = (
            body["view"]["state"]["values"]
            ["project_name_block"]["project_name"]["value"]
        ).strip()
        if not name:
            client.chat_postMessage(channel=ALLOWED_CHANNEL_ID, text="Project name cannot be empty.")
            return
        try:
            reply, auth = create_and_launch(name)
        except subprocess.CalledProcessError as exc:
            _post_error("Failed to create project", exc)
            reply, auth = f"Failed to create project: {exc}", None
        client.chat_postMessage(channel=ALLOWED_CHANNEL_ID, text=reply)
        if auth:
            client.chat_postMessage(channel=ALLOWED_CHANNEL_ID, text=f"*Authorization required:* {auth}")

    @app.view("clone_modal")
    def _clone_submit(ack, body, client):
        ack()
        url = (
            body["view"]["state"]["values"]
            ["github_url_block"]["github_url"]["value"]
        )
        client.chat_postMessage(channel=ALLOWED_CHANNEL_ID, text="Cloning…")
        try:
            reply, auth = clone_and_launch(url)
        except subprocess.CalledProcessError as exc:
            _post_error("Failed to clone repo", exc)
            reply, auth = f"Failed to clone: {exc}", None
        client.chat_postMessage(channel=ALLOWED_CHANNEL_ID, text=reply)
        if auth:
            client.chat_postMessage(
                channel=ALLOWED_CHANNEL_ID,
                text=f"*Authorization required:* {auth}",
            )

    # ---- Message event handler --------------------------------------------

    @app.event("message")
    def handle_message(event, say):
        if event.get("subtype"):
            return
        user = event.get("user", "")
        channel_id = event.get("channel", "")
        raw = (event.get("text") or "").strip()
        if not raw or channel_id != ALLOWED_CHANNEL_ID:
            return
        if ALLOWED_USER_ID and user != ALLOWED_USER_ID:
            return
        _handle_message_content(raw, channel_id, say)

    # ---- Start ------------------------------------------------------------

    _send_startup_message(slack_client)
    threading.Thread(target=_idle_reaper_loop, args=(slack_client,), daemon=True).start()
    bot.start()


if __name__ == "__main__":
    main()
