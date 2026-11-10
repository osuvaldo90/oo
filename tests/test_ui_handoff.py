from __future__ import annotations

import subprocess

import pytest

from oo.ui import handoff
from oo.ui.handoff import HandoffError


@pytest.mark.parametrize(
    ("environ", "want", "expected"),
    [
        ({}, "auto", "here"),
        ({"TMUX": "/tmp/tmux-1/default,1,0"}, "auto", "tmux"),
        # cmux sets TERM_PROGRAM to ghostty, so it has to be looked for first.
        ({"CMUX_WORKSPACE_ID": "ws", "TERM_PROGRAM": "ghostty"}, "auto", "cmux"),
        ({"CMUX_WORKSPACE_ID": "ws", "TMUX": "x"}, "auto", "cmux"),
        ({"TERM_PROGRAM": "iTerm.app"}, "auto", "iterm"),
        ({"TERM_PROGRAM": "iTerm.app", "TMUX": "x"}, "auto", "tmux"),
        ({"WEZTERM_PANE": "3"}, "auto", "wezterm"),
        ({"KITTY_WINDOW_ID": "2"}, "auto", "kitty"),
        ({"TERM_PROGRAM": "Apple_Terminal"}, "auto", "terminal"),
        ({"TERM_PROGRAM": "ghostty"}, "auto", "here"),
        ({"TERM_PROGRAM": "iTerm.app", "TMUX": "x"}, "tab", "iterm"),
        ({"TERM_PROGRAM": "iTerm.app", "TMUX": "x"}, "here", "here"),
        ({"TERM_PROGRAM": "iTerm.app"}, "tmux", "here"),
        ({"TMUX": "x"}, "cmux", "here"),
    ],
)
def test_detect_picks_where_sessions_open(environ, want, expected):
    assert handoff.detect(environ, want) == expected


def test_preference_comes_from_the_environment():
    assert handoff.preference({}) == "auto"
    assert handoff.preference({"OO_CONNECT": " Tmux "}) == "tmux"
    assert handoff.preference({"OO_CONNECT": ""}) == "auto"


def test_applescript_strings_are_escaped():
    assert handoff.applescript_string('say "hi" \\ there') == '"say \\"hi\\" \\\\ there"'


def test_session_names_suit_tmux():
    assert handoff.session_name("adam/login-fix") == "adam-login-fix"
    assert handoff.session_name("...") == "oo"


def test_setup_script_makes_the_session_in_the_repo_with_the_reaper():
    script = handoff.setup_script("login-fix", "/workspaces/web", timeout=42)
    assert "tmux has-session -t '=login-fix'" in script
    assert "tmux new-session -d -s login-fix -c /workspaces/web" in script
    assert f"sh {handoff.REAPER_PATH} 42 {handoff.REAPER_INTERVAL}" in script


@pytest.fixture
def runs(monkeypatch):
    """Record what open_elsewhere runs instead of running it."""
    record: list[list[str]] = []

    def run(argv, **kwargs):
        record.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(handoff.subprocess, "run", run)
    monkeypatch.setattr(handoff.shutil, "which", lambda name: f"/usr/bin/{name}")
    return record


ARGV = ["/usr/local/bin/ona", "environment", "ssh", "env-1"]


HELD = ["/bin/sh", "-c", handoff.HOLD, "sh", *ARGV]


def test_tmux_gets_a_named_window_that_stays_up_on_failure(runs):
    handoff.open_elsewhere("tmux", ARGV, "login-fix")
    assert runs == [["/usr/bin/tmux", "new-window", "-n", "login-fix", "--", *HELD]]


def test_holding_keeps_the_exit_code_and_waits_only_on_failure():
    ok = subprocess.run(handoff.holding(["true"]), capture_output=True, text=True, timeout=10)
    assert ok.returncode == 0 and ok.stdout == ""
    bad = subprocess.run(
        handoff.holding(["sh", "-c", "exit 3"]),
        input="\n",
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert "sh exited with 3. Press Enter to close." in bad.stdout


def test_iterm_gets_a_tab_through_applescript(runs):
    handoff.open_elsewhere("iterm", ARGV, "login-fix")
    assert runs[0][:2] == ["/usr/bin/osascript", "-e"]
    assert "create tab with default profile command " in runs[0][2]
    assert "/usr/local/bin/ona environment ssh env-1" in runs[0][2]
    assert "Press Enter to close" in runs[0][2]


def test_terminal_app_gets_a_window(runs):
    handoff.open_elsewhere("terminal", ARGV, "login-fix")
    assert runs[0][2] == (
        'tell application "Terminal" to do script "/usr/local/bin/ona environment ssh env-1"'
    )


def test_prepare_tmux_gives_up_after_timeouts(monkeypatch):
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))

    monkeypatch.setattr(handoff.subprocess, "run", run)
    monkeypatch.setattr(handoff.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(handoff.time, "sleep", lambda s: None)
    with pytest.raises(HandoffError, match="Couldn't set up tmux") as e:
        handoff.prepare_tmux("h.ona.environment", "s")
    assert e.value.detail == "ssh took too long to answer."


def test_wezterm_and_kitty_spawn(runs):
    handoff.open_elsewhere("wezterm", ARGV, "login-fix")
    handoff.open_elsewhere("kitty", ARGV, "login-fix")
    assert runs[0] == ["/usr/bin/wezterm", "cli", "spawn", "--", *HELD]
    assert runs[1][:6] == [
        "/usr/bin/kitten",
        "@",
        "launch",
        "--type=tab",
        "--tab-title",
        "login-fix",
    ]


def test_a_terminal_that_refuses_is_reported(monkeypatch):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "no server running")

    monkeypatch.setattr(handoff.subprocess, "run", run)
    monkeypatch.setattr(handoff.shutil, "which", lambda name: f"/usr/bin/{name}")
    with pytest.raises(HandoffError, match="tmux couldn't open a window") as e:
        handoff.open_elsewhere("tmux", ARGV, "login-fix")
    assert e.value.detail == "no server running"


def test_unknown_kind_is_an_error():
    with pytest.raises(HandoffError):
        handoff.open_elsewhere("teleport", ARGV, "x")


def test_cmux_prepares_tmux_then_mirrors(monkeypatch):
    calls: list[tuple] = []

    class Process:
        returncode = None

        def __init__(self, argv, **kwargs):
            calls.append(("popen", argv))

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired("cmux", timeout or 0)

    monkeypatch.setattr(handoff, "cmux_binary", lambda environ=None: "/apps/cmux")
    monkeypatch.setattr(
        handoff,
        "prepare_tmux",
        lambda host, session, checkout="": calls.append(("prepare", host, session, checkout)),
    )
    monkeypatch.setattr(handoff.subprocess, "Popen", Process)
    handoff.open_elsewhere(
        "cmux", ARGV, "adam/login-fix", host="env-1.ona.environment", checkout="/w"
    )
    assert calls == [
        ("prepare", "env-1.ona.environment", "adam-login-fix", "/w"),
        ("popen", ["/apps/cmux", "ssh-tmux", "env-1.ona.environment"]),
    ]


def test_cmux_that_gives_up_at_once_is_reported(monkeypatch):
    class Process:
        returncode = 2

        def __init__(self, argv, **kwargs):
            pass

        def communicate(self, timeout=None):
            return "socket not found", None

    monkeypatch.setattr(handoff, "cmux_binary", lambda environ=None: "/apps/cmux")
    monkeypatch.setattr(handoff, "prepare_tmux", lambda *a, **k: None)
    monkeypatch.setattr(handoff.subprocess, "Popen", Process)
    with pytest.raises(HandoffError, match="cmux couldn't open") as e:
        handoff.open_in_cmux("h", "t")
    assert e.value.detail == "socket not found"


def test_without_the_cmux_cli_there_is_a_hint(monkeypatch):
    monkeypatch.setattr(handoff, "cmux_binary", lambda environ=None: None)
    with pytest.raises(HandoffError, match="cmux CLI"):
        handoff.open_in_cmux("h", "t")


def test_cmux_binary_prefers_the_bundled_cli(tmp_path, monkeypatch):
    cli = tmp_path / "cmux"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)
    assert handoff.cmux_binary({"CMUX_BUNDLED_CLI_PATH": str(cli)}) == str(cli)
    monkeypatch.setattr(handoff.shutil, "which", lambda name: None)
    assert handoff.cmux_binary({"CMUX_BUNDLED_CLI_PATH": str(tmp_path / "missing")}) is None
