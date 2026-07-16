import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import bot


# ---- list_repos -------------------------------------------------------

def test_list_repos_returns_only_git_dirs_sorted_case_insensitively(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "GIT_ROOT", tmp_path)

    for name in ["Zebra", "alpha", "no-git-here"]:
        d = tmp_path / name
        d.mkdir()
        if name != "no-git-here":
            (d / ".git").mkdir()

    (tmp_path / "a-file.txt").write_text("not a dir")

    repos = bot.list_repos()

    assert [r.name for r in repos] == ["alpha", "Zebra"]


def test_list_repos_missing_git_root_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "GIT_ROOT", tmp_path / "does-not-exist")

    assert bot.list_repos() == []


# ---- format_repo_listing ----------------------------------------------

def test_format_repo_listing_single_chunk_has_prompt_suffix():
    repos = [Path("one"), Path("two")]

    chunks = bot.format_repo_listing(repos)

    assert len(chunks) == 1
    assert "0: one" in chunks[0]
    assert "1: two" in chunks[0]
    assert chunks[0].endswith("Reply with a number.")


def test_format_repo_listing_splits_when_over_discord_limit(monkeypatch):
    monkeypatch.setattr(bot, "DISCORD_MESSAGE_LIMIT", 40)
    repos = [Path(f"repo-{i}") for i in range(10)]

    chunks = bot.format_repo_listing(repos)

    assert len(chunks) > 1
    for chunk in chunks:
        # Strip the trailing prompt before checking the size budget, since
        # only the last chunk carries it.
        body = chunk.removesuffix("\nReply with a number.")
        assert len(body) <= 40
    assert chunks[-1].endswith("Reply with a number.")


# ---- format_session_listing --------------------------------------------

def test_format_session_listing_has_kill_footer():
    chunks = bot.format_session_listing(["repo-a", "repo-b"])

    assert len(chunks) == 1
    assert "0: repo-a" in chunks[0]
    assert "1: repo-b" in chunks[0]
    assert chunks[0].endswith("Reply with `!kill <number>` to stop one, or `!kill <name>`.")


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
    assert bot.sanitize_session_name(raw) == expected


# ---- tmux_session_exists ------------------------------------------------

def test_tmux_session_exists_true_when_returncode_zero():
    with patch("bot.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=0)
        assert bot.tmux_session_exists("some-session") is True


def test_tmux_session_exists_false_when_returncode_nonzero():
    with patch("bot.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=1)
        assert bot.tmux_session_exists("some-session") is False


# ---- launch_remote_control ----------------------------------------------

def test_launch_remote_control_reuses_existing_session(tmp_path):
    repo = tmp_path / "my-repo"
    repo.mkdir()

    with patch("bot.tmux_session_exists", return_value=True), \
         patch("bot.subprocess.run") as run:
        reply = bot.launch_remote_control(repo)

    run.assert_not_called()
    assert "already running" in reply
    assert "my-repo" in reply


def test_launch_remote_control_starts_new_tmux_session(tmp_path):
    repo = tmp_path / "my-repo"
    repo.mkdir()

    with patch("bot.tmux_session_exists", return_value=False), \
         patch("bot.subprocess.run") as run:
        reply = bot.launch_remote_control(repo)

    run.assert_called_once()
    args = run.call_args.args[0]
    assert args[0] == bot.TMUX_BIN
    assert "new-session" in args
    assert str(repo) in args
    assert "Started remote-control session" in reply


def test_launch_remote_control_propagates_subprocess_error(tmp_path):
    repo = tmp_path / "my-repo"
    repo.mkdir()

    with patch("bot.tmux_session_exists", return_value=False), \
         patch("bot.subprocess.run", side_effect=subprocess.CalledProcessError(1, "tmux")):
        with pytest.raises(subprocess.CalledProcessError):
            bot.launch_remote_control(repo)


# ---- list_active_sessions ------------------------------------------------

def test_list_active_sessions_parses_tmux_output():
    with patch("bot.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=0, stdout="alpha\nbeta\n")
        assert bot.list_active_sessions() == ["alpha", "beta"]


def test_list_active_sessions_empty_when_no_server():
    with patch("bot.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=1, stdout="")
        assert bot.list_active_sessions() == []


# ---- kill_session ----------------------------------------------------------

def test_kill_session_reports_missing_session():
    with patch("bot.tmux_session_exists", return_value=False), \
         patch("bot.subprocess.run") as run:
        reply = bot.kill_session("ghost")

    run.assert_not_called()
    assert "No running session named" in reply
    assert "ghost" in reply


def test_kill_session_kills_running_session():
    with patch("bot.tmux_session_exists", return_value=True), \
         patch("bot.subprocess.run") as run:
        reply = bot.kill_session("my-repo")

    run.assert_called_once()
    args = run.call_args.args[0]
    assert args[0] == bot.TMUX_BIN
    assert "kill-session" in args
    assert "my-repo" in args
    assert "Stopped session" in reply


def test_kill_session_propagates_subprocess_error():
    with patch("bot.tmux_session_exists", return_value=True), \
         patch("bot.subprocess.run", side_effect=subprocess.CalledProcessError(1, "tmux")):
        with pytest.raises(subprocess.CalledProcessError):
            bot.kill_session("my-repo")


# ---- on_message integration ---------------------------------------------

def make_message(content, author_id=None, channel_id=None, is_bot=False):
    author_id = bot.ALLOWED_USER_ID if author_id is None else author_id
    channel_id = bot.ALLOWED_CHANNEL_ID if channel_id is None else channel_id
    message = SimpleNamespace(
        content=content,
        author=SimpleNamespace(id=author_id, bot=is_bot),
        channel=SimpleNamespace(id=channel_id, send=AsyncMock()),
    )
    return message


@pytest.fixture(autouse=True)
def clear_pending_listings():
    bot.pending_listings.clear()
    bot.pending_sessions.clear()
    yield
    bot.pending_listings.clear()
    bot.pending_sessions.clear()


async def test_on_message_ignores_other_users():
    message = make_message("!repos", author_id=999)
    await bot.on_message(message)
    message.channel.send.assert_not_called()


async def test_on_message_ignores_other_channels():
    message = make_message("!repos", channel_id=999)
    await bot.on_message(message)
    message.channel.send.assert_not_called()


async def test_on_message_ignores_bots():
    message = make_message("!repos", is_bot=True)
    await bot.on_message(message)
    message.channel.send.assert_not_called()


async def test_on_message_repos_with_none_found(monkeypatch):
    monkeypatch.setattr(bot, "list_repos", lambda: [])
    message = make_message("!repos")

    await bot.on_message(message)

    message.channel.send.assert_awaited_once()
    assert "No git repos found" in message.channel.send.await_args.args[0]


async def test_on_message_repos_lists_and_remembers_for_channel(monkeypatch):
    repos = [Path("alpha"), Path("beta")]
    monkeypatch.setattr(bot, "list_repos", lambda: repos)
    message = make_message("!repos")

    await bot.on_message(message)

    assert bot.pending_listings[bot.ALLOWED_CHANNEL_ID] == repos
    sent = message.channel.send.await_args.args[0]
    assert "0: alpha" in sent and "1: beta" in sent


async def test_on_message_number_without_prior_listing():
    message = make_message("0")

    await bot.on_message(message)

    message.channel.send.assert_awaited_once_with("No repo list yet — send `!repos` first.")


async def test_on_message_number_out_of_range():
    bot.pending_listings[bot.ALLOWED_CHANNEL_ID] = [Path("only-one")]
    message = make_message("5")

    await bot.on_message(message)

    sent = message.channel.send.await_args.args[0]
    assert "Pick a number between 0 and 0" in sent


async def test_on_message_number_launches_session(tmp_path, monkeypatch):
    repo = tmp_path / "picked-repo"
    repo.mkdir()
    bot.pending_listings[bot.ALLOWED_CHANNEL_ID] = [repo]
    message = make_message("0")

    with patch("bot.launch_remote_control", return_value="Started!") as launch:
        await bot.on_message(message)

    launch.assert_called_once_with(repo)
    message.channel.send.assert_awaited_once_with("Started!")


async def test_on_message_number_reports_subprocess_failure():
    repo = Path("some-repo")
    bot.pending_listings[bot.ALLOWED_CHANNEL_ID] = [repo]
    message = make_message("0")

    error = subprocess.CalledProcessError(1, "tmux")
    with patch("bot.launch_remote_control", side_effect=error):
        await bot.on_message(message)

    sent = message.channel.send.await_args.args[0]
    assert "Failed to start session" in sent


# ---- on_message: !status / !kill -----------------------------------------

async def test_on_message_status_with_none_running(monkeypatch):
    monkeypatch.setattr(bot, "list_active_sessions", lambda: [])
    message = make_message("!status")

    await bot.on_message(message)

    message.channel.send.assert_awaited_once_with("No sessions running.")


async def test_on_message_status_lists_and_remembers_for_channel(monkeypatch):
    sessions = ["alpha", "beta"]
    monkeypatch.setattr(bot, "list_active_sessions", lambda: sessions)
    message = make_message("!sessions")

    await bot.on_message(message)

    assert bot.pending_sessions[bot.ALLOWED_CHANNEL_ID] == sessions
    sent = message.channel.send.await_args.args[0]
    assert "0: alpha" in sent and "1: beta" in sent


async def test_on_message_kill_without_arg_shows_usage():
    message = make_message("!kill")

    await bot.on_message(message)

    sent = message.channel.send.await_args.args[0]
    assert "Usage:" in sent


async def test_on_message_kill_number_without_prior_status():
    message = make_message("!kill 0")

    await bot.on_message(message)

    message.channel.send.assert_awaited_once_with(
        "No status list yet — send `!status` first."
    )


async def test_on_message_kill_number_out_of_range():
    bot.pending_sessions[bot.ALLOWED_CHANNEL_ID] = ["only-one"]
    message = make_message("!kill 5")

    await bot.on_message(message)

    sent = message.channel.send.await_args.args[0]
    assert "Pick a number between 0 and 0" in sent


async def test_on_message_kill_by_number_uses_session_from_status(monkeypatch):
    bot.pending_sessions[bot.ALLOWED_CHANNEL_ID] = ["picked-session"]
    message = make_message("!kill 0")

    with patch("bot.kill_session", return_value="Stopped!") as kill:
        await bot.on_message(message)

    kill.assert_called_once_with("picked-session")
    message.channel.send.assert_awaited_once_with("Stopped!")


async def test_on_message_kill_by_name_sanitizes_and_kills(monkeypatch):
    message = make_message("!stop my repo!")

    with patch("bot.kill_session", return_value="Stopped!") as kill:
        await bot.on_message(message)

    kill.assert_called_once_with("my-repo-")
    message.channel.send.assert_awaited_once_with("Stopped!")


async def test_on_message_kill_reports_subprocess_failure():
    message = make_message("!kill some-repo")

    error = subprocess.CalledProcessError(1, "tmux")
    with patch("bot.kill_session", side_effect=error):
        await bot.on_message(message)

    sent = message.channel.send.await_args.args[0]
    assert "Failed to stop session" in sent
