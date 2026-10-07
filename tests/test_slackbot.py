import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import slackbot


# ---- list_repos -------------------------------------------------------

def test_list_repos_returns_only_git_dirs_sorted_case_insensitively(tmp_path, monkeypatch):
    monkeypatch.setattr(slackbot, "GIT_ROOT", tmp_path)

    for name in ["Zebra", "alpha", "no-git-here"]:
        d = tmp_path / name
        d.mkdir()
        if name != "no-git-here":
            (d / ".git").mkdir()

    (tmp_path / "a-file.txt").write_text("not a dir")

    repos = slackbot.list_repos()

    assert [r.name for r in repos] == ["alpha", "Zebra"]


def test_list_repos_missing_git_root_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(slackbot, "GIT_ROOT", tmp_path / "does-not-exist")

    assert slackbot.list_repos() == []


# ---- _format_session_list_text ----------------------------------------

def test_format_session_list_text_lists_all_sessions():
    text = slackbot._format_session_list_text(["repo-a", "repo-b"])
    assert "repo-a" in text
    assert "repo-b" in text


# ---- sanitize_session_name ---------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("my-repo", "my-repo"),
        ("my repo!", "my-repo-"),
        ("weird/../path", "weird----path"),
        ("", "claude-session"),
    ],
)
def test_sanitize_session_name(raw, expected):
    assert slackbot.sanitize_session_name(raw) == expected


# ---- tmux_session_exists -----------------------------------------------

def test_tmux_session_exists_true_when_returncode_zero():
    with patch("slackbot.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=0)
        assert slackbot.tmux_session_exists("some-session") is True


def test_tmux_session_exists_false_when_returncode_nonzero():
    with patch("slackbot.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=1)
        assert slackbot.tmux_session_exists("some-session") is False


# ---- launch_remote_control ---------------------------------------------

def test_launch_remote_control_reuses_existing_session(tmp_path):
    repo = tmp_path / "my-repo"
    repo.mkdir()

    with patch("slackbot.tmux_session_exists", return_value=True), \
         patch("slackbot._extract_session_url", return_value="https://claude.ai/code/session_abc"), \
         patch("slackbot.subprocess.run") as run:
        reply, auth = slackbot.launch_remote_control(repo)

    run.assert_not_called()
    assert "already running" in reply
    assert "my-repo" in reply
    assert auth is None


def test_launch_remote_control_starts_new_tmux_session(tmp_path):
    repo = tmp_path / "my-repo"
    repo.mkdir()

    with patch("slackbot.tmux_session_exists", return_value=False), \
         patch("slackbot._extract_session_url", return_value="https://claude.ai/code/session_xyz"), \
         patch("slackbot.subprocess.run") as run:
        reply, auth = slackbot.launch_remote_control(repo)

    run.assert_called_once()
    args = run.call_args.args[0]
    assert args[0] == slackbot.TMUX_BIN
    assert "new-session" in args
    assert str(repo) in args
    assert "Started remote-control session" in reply
    assert auth is None


def test_launch_remote_control_propagates_subprocess_error(tmp_path):
    repo = tmp_path / "my-repo"
    repo.mkdir()

    with patch("slackbot.tmux_session_exists", return_value=False), \
         patch("slackbot.subprocess.run", side_effect=subprocess.CalledProcessError(1, "tmux")):
        with pytest.raises(subprocess.CalledProcessError):
            slackbot.launch_remote_control(repo)


def test_launch_remote_control_reports_not_logged_in_when_no_url_or_auth_prompt(tmp_path):
    repo = tmp_path / "my-repo"
    repo.mkdir()

    with patch("slackbot.tmux_session_exists", return_value=False), \
         patch("slackbot._extract_session_url", return_value=None), \
         patch("slackbot._extract_auth_prompt", return_value=None), \
         patch("slackbot.claude_login_status", return_value=(False, None)), \
         patch("slackbot.subprocess.run") as run:
        reply, auth = slackbot.launch_remote_control(repo)

    run.assert_called_once()
    assert "Started remote-control session" in reply
    assert auth is not None
    assert "not logged in" in auth
    assert slackbot.DEVICE_NAME in auth


def test_launch_remote_control_no_auth_message_when_logged_in_and_no_url(tmp_path):
    repo = tmp_path / "my-repo"
    repo.mkdir()

    with patch("slackbot.tmux_session_exists", return_value=False), \
         patch("slackbot._extract_session_url", return_value=None), \
         patch("slackbot._extract_auth_prompt", return_value=None), \
         patch("slackbot.claude_login_status", return_value=(True, "me@example.com")), \
         patch("slackbot.subprocess.run"):
        reply, auth = slackbot.launch_remote_control(repo)

    assert auth is None


# ---- claude_login_status -----------------------------------------------

def test_claude_login_status_parses_logged_in_json():
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout='{"loggedIn": true, "email": "me@example.com"}'
    )
    with patch("slackbot.subprocess.run", return_value=completed):
        logged_in, email = slackbot.claude_login_status()

    assert logged_in is True
    assert email == "me@example.com"


def test_claude_login_status_parses_logged_out_json():
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout='{"loggedIn": false}')
    with patch("slackbot.subprocess.run", return_value=completed):
        logged_in, email = slackbot.claude_login_status()

    assert logged_in is False
    assert email is None


def test_claude_login_status_returns_none_on_timeout():
    with patch("slackbot.subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 10)):
        logged_in, email = slackbot.claude_login_status()

    assert logged_in is None
    assert email is None


def test_claude_login_status_returns_none_on_bad_json():
    completed = subprocess.CompletedProcess(args=[], returncode=1, stdout="not json")
    with patch("slackbot.subprocess.run", return_value=completed):
        logged_in, email = slackbot.claude_login_status()

    assert logged_in is None
    assert email is None


# ---- list_active_sessions -----------------------------------------------

D = slackbot.DEVICE_NAME


def test_list_active_sessions_parses_tmux_output():
    with patch("slackbot.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=0, stdout=f"{D}-Alpha\n{D}-Beta\n")
        assert slackbot.list_active_sessions() == [f"{D}-Alpha", f"{D}-Beta"]


def test_list_active_sessions_ignores_non_claude_tmux_sessions():
    with patch("slackbot.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=0, stdout=f"eink\n{D}-Alpha\nwork\n")
        assert slackbot.list_active_sessions() == [f"{D}-Alpha"]


def test_list_active_sessions_empty_when_no_server():
    with patch("slackbot.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=1, stdout="")
        assert slackbot.list_active_sessions() == []


# ---- kill_session -------------------------------------------------------

def test_kill_session_reports_missing_session():
    with patch("slackbot.tmux_session_exists", return_value=False), \
         patch("slackbot.subprocess.run") as run:
        reply = slackbot.kill_session("ghost")

    run.assert_not_called()
    assert "No running session named" in reply
    assert "ghost" in reply


def test_kill_session_kills_running_session():
    mock_result = MagicMock()
    mock_result.returncode = 0
    with patch("slackbot.tmux_session_exists", return_value=True), \
         patch("slackbot.subprocess.run", return_value=mock_result) as run:
        reply = slackbot.kill_session("my-repo")

    run.assert_called_once()
    args = run.call_args.args[0]
    assert args[0] == slackbot.TMUX_BIN
    assert "kill-session" in args
    assert "my-repo" in args
    assert "Stopped session" in reply


def test_kill_session_handles_race_condition():
    mock_result = MagicMock()
    mock_result.returncode = 1
    with patch("slackbot.tmux_session_exists", return_value=True), \
         patch("slackbot.subprocess.run", return_value=mock_result):
        reply = slackbot.kill_session("my-repo")
    assert "already stopped" in reply


# ---- list_session_idle_hours / reap_idle_sessions -----------------------

def test_list_session_idle_hours_parses_tmux_output(monkeypatch):
    monkeypatch.setattr(slackbot.time, "time", lambda: 1_000_000)
    with patch("slackbot.subprocess.run") as run:
        run.return_value = SimpleNamespace(
            returncode=0,
            stdout=f"{D}-Alpha 964000\n{D}-Beta 999999\neink 1\n",
        )
        idle = slackbot.list_session_idle_hours()

    assert idle[f"{D}-Alpha"] == pytest.approx(10.0)
    assert idle[f"{D}-Beta"] == pytest.approx(1 / 3600)
    assert "eink" not in idle  # never reap tmux sessions this bot didn't launch


def test_claude_project_mtime_maps_underscores_like_claude_code(tmp_path, monkeypatch):
    monkeypatch.setattr(slackbot.Path, "home", lambda: tmp_path)
    project = tmp_path / ".claude" / "projects" / "-home-pi-git-mlb-display"
    project.mkdir(parents=True)
    (project / "s.jsonl").write_text("{}")
    with patch("slackbot.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=0, stdout="/home/pi/git/mlb_display\n")
        assert slackbot._claude_project_mtime(f"{D}-MlbDisplay") is not None


def test_list_session_idle_hours_empty_when_no_server():
    with patch("slackbot.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=1, stdout="")
        assert slackbot.list_session_idle_hours() == {}


def test_reap_idle_sessions_kills_only_sessions_past_threshold():
    with patch(
        "slackbot.list_session_idle_hours",
        return_value={"stale": 100.0, "fresh": 1.0},
    ), patch("slackbot.subprocess.run") as run:
        killed = slackbot.reap_idle_sessions(timeout_hours=72)

    assert killed == ["stale"]
    kill_calls = [c for c in run.call_args_list if "kill-session" in c.args[0]]
    assert len(kill_calls) == 1
    args = kill_calls[0].args[0]
    assert args[0] == slackbot.TMUX_BIN
    assert "stale" in args


def test_reap_idle_sessions_kills_nothing_when_all_fresh():
    with patch(
        "slackbot.list_session_idle_hours", return_value={"fresh": 1.0}
    ), patch("slackbot.subprocess.run") as run:
        killed = slackbot.reap_idle_sessions(timeout_hours=72)

    assert killed == []
    run.assert_not_called()


# ---- run_bash_command ---------------------------------------------------

def test_run_bash_command_returns_stdout():
    with patch("slackbot.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=0, stdout="hello\n", stderr="")
        reply = slackbot.run_bash_command("echo hello")

    args, kwargs = run.call_args
    assert args[0] == "echo hello"
    assert kwargs["shell"] is True
    assert kwargs["cwd"] == str(slackbot.GIT_ROOT)
    assert reply == "```\nhello\n```"


def test_run_bash_command_prefixes_nonzero_exit():
    with patch("slackbot.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=1, stdout="", stderr="boom")
        reply = slackbot.run_bash_command("false")

    assert reply.startswith("[exit 1] ```\nboom")


def test_run_bash_command_reports_no_output():
    with patch("slackbot.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        reply = slackbot.run_bash_command("true")

    assert reply == "```\n(no output)\n```"


def test_run_bash_command_truncates_long_output_keeping_tail():
    long = "x" * 10_000 + "END"
    with patch("slackbot.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=0, stdout=long, stderr="")
        reply = slackbot.run_bash_command("cat big")

    assert len(reply) < slackbot.MAX_OUTPUT_CHARS + 100
    assert "truncated" in reply
    assert reply.endswith("END\n```")


def test_run_bash_command_handles_timeout():
    with patch(
        "slackbot.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="sleep 100", timeout=60),
    ):
        reply = slackbot.run_bash_command("sleep 100", timeout=60)

    assert "Timed out after 60s" in reply
    assert "sleep 100" in reply


# ---- program management ------------------------------------------------

_SYSTEMD_PROG = {"type": "systemd", "service": "mlb.service", "description": "MLB"}
_SCRIPT_PROG = {"type": "script", "command": "/home/pi/run.sh", "log": "/home/pi/run.log"}
_CRON_PROG = {"type": "cron", "command": "/home/pi/fetch.py", "log": "/home/pi/fetch.log"}


def test_program_is_running_systemd_active():
    result = SimpleNamespace(returncode=0, stdout="active\n")
    with patch("slackbot.subprocess.run", return_value=result):
        assert slackbot._program_is_running("mlb", _SYSTEMD_PROG) is True


def test_program_is_running_systemd_inactive():
    result = SimpleNamespace(returncode=0, stdout="inactive\n")
    with patch("slackbot.subprocess.run", return_value=result):
        assert slackbot._program_is_running("mlb", _SYSTEMD_PROG) is False


def test_program_is_running_script_true():
    with patch("slackbot.subprocess.run", return_value=SimpleNamespace(returncode=0)):
        assert slackbot._program_is_running("fetch", _SCRIPT_PROG) is True


def test_program_is_running_script_false():
    with patch("slackbot.subprocess.run", return_value=SimpleNamespace(returncode=1)):
        assert slackbot._program_is_running("fetch", _SCRIPT_PROG) is False


def test_start_program_systemd_ok():
    with patch("slackbot.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")):
        reply = slackbot.start_program("mlb", _SYSTEMD_PROG)
    assert "Started" in reply and "mlb" in reply


def test_start_program_systemd_error():
    with patch("slackbot.subprocess.run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="bang")):
        with pytest.raises(subprocess.CalledProcessError):
            slackbot.start_program("mlb", _SYSTEMD_PROG)


def test_start_program_script_no_command():
    prog = {"type": "script"}
    reply = slackbot.start_program("empty", prog)
    assert "No command" in reply


def test_stop_program_systemd_ok():
    with patch("slackbot.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")):
        reply = slackbot.stop_program("mlb", _SYSTEMD_PROG)
    assert "Stopped" in reply and "mlb" in reply


def test_stop_program_script_running():
    with patch("slackbot.subprocess.run", return_value=SimpleNamespace(returncode=0)):
        reply = slackbot.stop_program("fetch", _SCRIPT_PROG)
    assert "Stopped" in reply


def test_stop_program_uses_full_command_not_first_word():
    prog = {"type": "script", "command": "python3 /home/pi/rotate.py"}
    with patch("slackbot.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
        slackbot.stop_program("rotate", prog)
    assert run.call_args.args[0] == ["pkill", "-f", "python3 /home/pi/rotate.py"]


def test_stop_and_status_prefer_check_pattern():
    prog = {"type": "script", "start": "python3 /home/pi/rotate.py", "check": "rotate.py"}
    with patch("slackbot.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
        slackbot.stop_program("rotate", prog)
        slackbot._program_is_running("rotate", prog)
    assert run.call_args_list[0].args[0] == ["pkill", "-f", "rotate.py"]
    assert run.call_args_list[1].args[0] == ["pgrep", "-f", "rotate.py"]


def test_stop_program_script_not_running():
    with patch("slackbot.subprocess.run", return_value=SimpleNamespace(returncode=1)):
        reply = slackbot.stop_program("fetch", _SCRIPT_PROG)
    assert "not running" in reply


def test_get_program_logs_systemd(tmp_path):
    with patch("slackbot.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=0, stdout="line1\nline2\n")
        reply = slackbot.get_program_logs("mlb", _SYSTEMD_PROG)
    assert "mlb" in reply
    assert "line1" in reply


def test_get_program_logs_script(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("log content\n")
    prog = {**_SCRIPT_PROG, "log": str(log)}
    with patch("slackbot.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=0, stdout="log content\n")
        reply = slackbot.get_program_logs("fetch", prog)
    assert "fetch" in reply
    assert "log content" in reply


def test_get_program_logs_no_log_path():
    prog = {"type": "script", "command": "/home/pi/run.sh"}
    reply = slackbot.get_program_logs("run", prog)
    assert "No `log` path" in reply


def test_format_programs_status_empty():
    reply = slackbot.format_programs_status({})
    assert "No programs configured" in reply


def test_format_programs_status_shows_programs():
    programs = {"mlb": _SYSTEMD_PROG}
    with patch("slackbot._program_is_running", return_value=True), \
         patch("slackbot._program_state_str", return_value="active"):
        reply = slackbot.format_programs_status(programs)
    assert "mlb" in reply
    assert "active" in reply


# ---- _handle_message_content -------------------------------------------

@pytest.fixture(autouse=True)
def clear_state():
    yield


def make_say():
    return MagicMock()


def test_handle_message_bash_without_arg_shows_usage():
    say = make_say()
    slackbot._handle_message_content("!bash", slackbot.ALLOWED_CHANNEL_ID, say)
    say.assert_called_once_with(text="Usage: `!bash <command>`")


def test_handle_message_bash_runs_command_and_replies():
    say = make_say()
    with patch("slackbot.run_bash_command", return_value="```\nhi\n```") as run_bash:
        slackbot._handle_message_content("!bash echo hi", slackbot.ALLOWED_CHANNEL_ID, say)

    run_bash.assert_called_once_with("echo hi")
    assert say.call_args.kwargs["text"] == "```\nhi\n```"


def test_handle_message_repos_with_none_found(monkeypatch):
    monkeypatch.setattr(slackbot, "list_repos", lambda: [])
    say = make_say()
    slackbot._handle_message_content("!repos", slackbot.ALLOWED_CHANNEL_ID, say)
    assert "No git repos found" in say.call_args.kwargs["text"]


def test_handle_message_repos_posts_dropdown_picker(monkeypatch):
    repos = [Path("/git/alpha"), Path("/git/beta")]
    monkeypatch.setattr(slackbot, "list_repos", lambda: repos)
    say = make_say()
    slackbot._handle_message_content("!repos", slackbot.ALLOWED_CHANNEL_ID, say)

    _, kwargs = say.call_args
    blocks = kwargs["blocks"]
    # static_select with repo options
    select_block = next(b for b in blocks if b.get("block_id") == "inline_repo_picker")
    options = select_block["accessory"]["options"]
    option_values = [o["value"] for o in options]
    assert "/git/alpha" in option_values
    assert "/git/beta" in option_values


def test_handle_message_status_shows_programs(monkeypatch):
    monkeypatch.setattr(slackbot, "_load_programs", lambda: {"mlb": _SYSTEMD_PROG})
    say = make_say()
    with patch("slackbot._program_is_running", return_value=True), \
         patch("slackbot._program_state_str", return_value="active"):
        slackbot._handle_message_content("!status", slackbot.ALLOWED_CHANNEL_ID, say)
    text = say.call_args.kwargs["text"]
    assert "mlb" in text


def test_handle_message_status_no_programs_configured(monkeypatch):
    monkeypatch.setattr(slackbot, "_load_programs", lambda: {})
    say = make_say()
    slackbot._handle_message_content("!status", slackbot.ALLOWED_CHANNEL_ID, say)
    say.assert_called_once_with(text="No programs configured — add a `programs.toml` to this repo.")


def test_handle_message_sessions_posts_kill_buttons(monkeypatch):
    sessions = ["alpha", "beta"]
    monkeypatch.setattr(slackbot, "list_active_sessions", lambda: sessions)
    say = make_say()
    slackbot._handle_message_content("!sessions", slackbot.ALLOWED_CHANNEL_ID, say)

    _, kwargs = say.call_args
    blocks = kwargs["blocks"]
    action_ids = [
        el["action_id"]
        for b in blocks if b.get("type") == "section" and "accessory" in b
        for el in [b["accessory"]]
    ]
    assert "inline_session_kill:alpha" in action_ids
    assert "inline_session_kill:beta" in action_ids


def test_handle_message_sessions_with_none_running(monkeypatch):
    monkeypatch.setattr(slackbot, "list_active_sessions", lambda: [])
    say = make_say()
    slackbot._handle_message_content("!sessions", slackbot.ALLOWED_CHANNEL_ID, say)
    say.assert_called_once_with(text="No sessions running.")


def test_handle_message_start_no_arg_shows_usage():
    say = make_say()
    slackbot._handle_message_content("!start", slackbot.ALLOWED_CHANNEL_ID, say)
    assert "Usage:" in say.call_args.kwargs["text"]


def test_handle_message_start_unknown_program(monkeypatch):
    monkeypatch.setattr(slackbot, "_load_programs", lambda: {})
    say = make_say()
    slackbot._handle_message_content("!start mlb", slackbot.ALLOWED_CHANNEL_ID, say)
    assert "Unknown program" in say.call_args.kwargs["text"]


def test_handle_message_start_known_program(monkeypatch):
    monkeypatch.setattr(slackbot, "_load_programs", lambda: {"mlb": _SYSTEMD_PROG})
    say = make_say()
    with patch("slackbot.start_program", return_value="Started *mlb*.") as sp:
        slackbot._handle_message_content("!start mlb", slackbot.ALLOWED_CHANNEL_ID, say)
    sp.assert_called_once_with("mlb", _SYSTEMD_PROG)
    assert say.call_args.kwargs["text"] == "Started *mlb*."


def test_handle_message_start_propagates_error(monkeypatch):
    monkeypatch.setattr(slackbot, "_load_programs", lambda: {"mlb": _SYSTEMD_PROG})
    say = make_say()
    error = subprocess.CalledProcessError(1, "systemctl")
    with patch("slackbot.start_program", side_effect=error), \
         patch("slackbot._post_error"):
        slackbot._handle_message_content("!start mlb", slackbot.ALLOWED_CHANNEL_ID, say)
    assert "Failed to start" in say.call_args.kwargs["text"]


def test_handle_message_stop_no_arg_shows_usage():
    say = make_say()
    slackbot._handle_message_content("!stop", slackbot.ALLOWED_CHANNEL_ID, say)
    assert "Usage:" in say.call_args.kwargs["text"]


def test_handle_message_stop_known_program(monkeypatch):
    monkeypatch.setattr(slackbot, "_load_programs", lambda: {"mlb": _SYSTEMD_PROG})
    say = make_say()
    with patch("slackbot.stop_program", return_value="Stopped *mlb*.") as sp:
        slackbot._handle_message_content("!stop mlb", slackbot.ALLOWED_CHANNEL_ID, say)
    sp.assert_called_once_with("mlb", _SYSTEMD_PROG)
    assert say.call_args.kwargs["text"] == "Stopped *mlb*."


def test_handle_message_restart_no_arg_shows_usage():
    say = make_say()
    slackbot._handle_message_content("!restart", slackbot.ALLOWED_CHANNEL_ID, say)
    assert "Usage:" in say.call_args.kwargs["text"]


def test_handle_message_restart_systemd_program(monkeypatch):
    monkeypatch.setattr(slackbot, "_load_programs", lambda: {"mlb": _SYSTEMD_PROG})
    say = make_say()
    with patch("slackbot._systemctl", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")):
        slackbot._handle_message_content("!restart mlb", slackbot.ALLOWED_CHANNEL_ID, say)
    assert "Restarted" in say.call_args.kwargs["text"]


def test_handle_message_logs_no_arg_shows_usage():
    say = make_say()
    slackbot._handle_message_content("!logs", slackbot.ALLOWED_CHANNEL_ID, say)
    assert "Usage:" in say.call_args.kwargs["text"]


def test_handle_message_logs_known_program(monkeypatch):
    monkeypatch.setattr(slackbot, "_load_programs", lambda: {"mlb": _SYSTEMD_PROG})
    say = make_say()
    with patch("slackbot.get_program_logs", return_value="*mlb* — last 20 lines:\n```\nlog\n```") as gl:
        slackbot._handle_message_content("!logs mlb", slackbot.ALLOWED_CHANNEL_ID, say)
    gl.assert_called_once_with("mlb", _SYSTEMD_PROG)
    assert "log" in say.call_args.kwargs["text"]


def test_handle_message_kill_without_arg_shows_usage():
    say = make_say()
    slackbot._handle_message_content("!kill", slackbot.ALLOWED_CHANNEL_ID, say)
    assert "Usage:" in say.call_args.kwargs["text"]


def test_handle_message_kill_by_name_sanitizes_and_kills():
    say = make_say()
    with patch("slackbot.kill_session", return_value="Stopped!") as kill:
        slackbot._handle_message_content("!kill my repo!", slackbot.ALLOWED_CHANNEL_ID, say)

    kill.assert_called_once_with("my-repo-")
    assert say.call_args.kwargs["text"] == "Stopped!"


def test_handle_message_kill_reports_subprocess_failure():
    say = make_say()
    error = subprocess.CalledProcessError(1, "tmux")
    with patch("slackbot.kill_session", side_effect=error), \
         patch("slackbot._post_error"):
        slackbot._handle_message_content("!kill some-repo", slackbot.ALLOWED_CHANNEL_ID, say)

    assert "Failed to stop session" in say.call_args.kwargs["text"]


def test_handle_message_kill_by_number(monkeypatch):
    monkeypatch.setattr(slackbot, "list_active_sessions", lambda: [f"{D}-Alpha", f"{D}-Beta"])
    say = make_say()
    with patch("slackbot.kill_session", return_value="Stopped!") as kill:
        slackbot._handle_message_content("!kill 2", slackbot.ALLOWED_CHANNEL_ID, say)
    kill.assert_called_once_with(f"{D}-Beta")


def test_handle_message_kill_number_out_of_range(monkeypatch):
    monkeypatch.setattr(slackbot, "list_active_sessions", lambda: [f"{D}-Alpha"])
    say = make_say()
    with patch("slackbot.kill_session") as kill:
        slackbot._handle_message_content("!kill 5", slackbot.ALLOWED_CHANNEL_ID, say)
    kill.assert_not_called()
    assert "No session #5" in say.call_args.kwargs["text"]


def test_handle_message_number_launches_repo(monkeypatch):
    monkeypatch.setattr(slackbot, "list_repos", lambda: [Path("/git/alpha"), Path("/git/beta")])
    say = make_say()
    with patch("slackbot.launch_remote_control", return_value=("Started!", None)) as launch:
        slackbot._handle_message_content("2", slackbot.ALLOWED_CHANNEL_ID, say)
    launch.assert_called_once_with(Path("/git/beta"))
    assert say.call_args.kwargs["text"] == "Started!"


def test_handle_message_number_out_of_range(monkeypatch):
    monkeypatch.setattr(slackbot, "list_repos", lambda: [Path("/git/alpha")])
    say = make_say()
    with patch("slackbot.launch_remote_control") as launch:
        slackbot._handle_message_content("0", slackbot.ALLOWED_CHANNEL_ID, say)
    launch.assert_not_called()
    assert "No repo #0" in say.call_args.kwargs["text"]


def test_handle_message_repos_dropdown_is_numbered(monkeypatch):
    monkeypatch.setattr(slackbot, "list_repos", lambda: [Path("/git/alpha"), Path("/git/beta")])
    say = make_say()
    slackbot._handle_message_content("!repos", slackbot.ALLOWED_CHANNEL_ID, say)
    select_block = next(b for b in say.call_args.kwargs["blocks"] if b.get("block_id") == "inline_repo_picker")
    labels = [o["text"]["text"] for o in select_block["accessory"]["options"]]
    assert labels == ["1. alpha", "2. beta"]


def test_handle_message_unknown_command_replies():
    say = make_say()
    slackbot._handle_message_content("!frobnicate now", slackbot.ALLOWED_CHANNEL_ID, say)
    assert "Unknown command `!frobnicate`" in say.call_args.kwargs["text"]


def test_handle_message_plain_chat_is_ignored():
    say = make_say()
    slackbot._handle_message_content("just chatting", slackbot.ALLOWED_CHANNEL_ID, say)
    say.assert_not_called()


@pytest.mark.parametrize(
    "text",
    [
        "<https://github.com/owner/repo>",
        "<https://github.com/owner/repo|github.com/owner/repo>",
        "https://github.com/owner/repo",
    ],
)
def test_handle_message_github_url_unwraps_slack_link(text):
    say = make_say()
    with patch("slackbot.clone_and_launch", return_value=("Cloned!", None)) as clone:
        slackbot._handle_message_content(text, slackbot.ALLOWED_CHANNEL_ID, say)
    clone.assert_called_once_with("https://github.com/owner/repo")


# ---- channel restriction ----------------------------------------------

@pytest.mark.parametrize(
    "body,allowed",
    [
        ({"type": "block_actions", "channel": {"id": "C0123456789"}}, True),
        ({"type": "block_actions", "channel": {"id": "COTHER"}}, False),
        ({"type": "event_callback", "event": {"type": "app_mention", "channel": "C0123456789"}}, True),
        ({"type": "event_callback", "event": {"type": "app_mention", "channel": "COTHER"}}, False),
        ({"type": "view_submission", "view": {}}, True),
        ({"type": "block_actions"}, False),
    ],
)
def test_is_allowed_request(body, allowed):
    assert slackbot._is_allowed_request(body) is allowed


def test_handle_message_stop_is_for_programs_not_sessions(monkeypatch):
    """!stop routes to program management, not session killing."""
    monkeypatch.setattr(slackbot, "_load_programs", lambda: {"mlb": _SYSTEMD_PROG})
    say = make_say()
    with patch("slackbot.stop_program", return_value="Stopped *mlb*."):
        slackbot._handle_message_content("!stop mlb", slackbot.ALLOWED_CHANNEL_ID, say)
    assert say.call_args.kwargs["text"] == "Stopped *mlb*."
