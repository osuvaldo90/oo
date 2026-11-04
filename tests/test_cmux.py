from __future__ import annotations

import shutil
import subprocess

import pytest

from oo import cmux


def test_only_in_a_cmux_terminal(monkeypatch, tmp_path):
    cli = tmp_path / "cmux"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)
    monkeypatch.setenv("CMUX_BUNDLED_CLI_PATH", str(cli))
    assert cmux.binary() is None
    monkeypatch.setenv("CMUX_WORKSPACE_ID", "workspace-1")
    assert cmux.binary() == str(cli)


@pytest.mark.parametrize(
    ("label", "session"),
    [("login-fix", "login-fix"), ("my env.v2", "my-env-v2"), ("a:b", "a-b"), ("...", "oo")],
)
def test_session_names_suit_tmux(label, session):
    assert cmux.session_name(label) == session


def test_idle_timeout_comes_from_the_environment(monkeypatch):
    assert cmux.idle_timeout() == 3600
    monkeypatch.setenv("ONA_IDLE_TIMEOUT", "600")
    assert cmux.idle_timeout() == 600
    monkeypatch.setenv("OO_IDLE_TIMEOUT", "60")
    assert cmux.idle_timeout() == 60


def test_setup_script_makes_the_session_once_in_the_repo():
    script = cmux.setup_script("login-fix", "/workspaces/my repo", 600)
    assert "tmux has-session -t '=login-fix'" in script
    assert "tmux new-session -d -s login-fix -c '/workspaces/my repo'" in script
    assert f"sh {cmux.REAPER_PATH} 600 300" in script
    assert "@oc_reaper" in script


def test_setup_script_is_valid_shell():
    done = subprocess.run(["sh", "-n", "-c", cmux.setup_script("x", "/w")], capture_output=True)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("shell", ["sh", "bash", "zsh"])
def test_setup_script_works_in_any_login_shell(shell, tmp_path):
    """The environment's login shell runs it, and zsh expands a bare `=word`."""
    if not shutil.which(shell):
        pytest.skip(f"no {shell}")
    log = tmp_path / "tmux.log"
    fake = tmp_path / "tmux"
    # A tmux with no sessions, that writes down each command it gets, one argument a line.
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" ---- >> {log}\n[ "$1" != has-session ]\n')
    fake.chmod(0o755)
    script = cmux.setup_script("demo-domains", "/workspaces/my repo", 600)
    script = script.replace(cmux.REAPER_PATH, str(tmp_path / "reaper.sh"))
    done = subprocess.run(
        [shell, "-c", script],
        input=cmux.REAPER,
        capture_output=True,
        text=True,
        env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
    )
    assert done.returncode == 0, done.stderr
    calls = [c.strip("\n").split("\n") for c in log.read_text().split("----") if c.strip()]
    assert calls[0] == ["has-session", "-t", "=demo-domains"]
    assert calls[1] == ["new-session", "-d", "-s", "demo-domains", "-c", "/workspaces/my repo"]
    assert calls[2][:3] == ["if", "-F", "#{@oc_reaper}"]
    assert (tmp_path / "reaper.sh").read_text() == cmux.REAPER


def ssh_that_exits(monkeypatch, codes, said="nope"):
    """Fake ssh, exiting with each of `codes` in turn."""
    runs = []

    def run(command, **kwargs):
        runs.append(command)
        return subprocess.CompletedProcess(command, codes[len(runs) - 1], "", said)

    monkeypatch.setattr(cmux.ssh, "binary", lambda name: name)
    monkeypatch.setattr(cmux.subprocess, "run", run)
    monkeypatch.setattr(cmux.time, "sleep", lambda s: None)
    return runs


def test_prepare_waits_for_ssh_to_come_up(monkeypatch):
    runs = ssh_that_exits(monkeypatch, [255, 255, 0])
    cmux.prepare("e.ona.environment", "e")
    assert len(runs) == 3
    assert runs[0][:6] == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={cmux.CONNECT_TIMEOUT}",
        "e.ona.environment",
    ]


def test_prepare_gives_up_on_other_failures(monkeypatch):
    runs = ssh_that_exits(monkeypatch, [1])
    with pytest.raises(cmux.CmuxError) as e:
        cmux.prepare("e.ona.environment", "e")
    assert e.value.detail == "nope"
    assert len(runs) == 1


def test_prepare_gives_up_eventually(monkeypatch):
    runs = ssh_that_exits(monkeypatch, [255] * cmux.ATTEMPTS)
    with pytest.raises(cmux.CmuxError):
        cmux.prepare("e.ona.environment", "e")
    assert len(runs) == cmux.ATTEMPTS


def test_prepare_leaves_out_onas_update_warnings(monkeypatch):
    ssh_that_exits(monkeypatch, [1], said="[WARN ] new version available\nzsh:2: x not found\n")
    with pytest.raises(cmux.CmuxError) as e:
        cmux.prepare("e.ona.environment", "e")
    assert e.value.detail == "zsh:2: x not found"


def test_prepare_gives_up_after_timeouts(monkeypatch):
    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 0))

    monkeypatch.setattr(cmux.ssh, "binary", lambda name: name)
    monkeypatch.setattr(cmux.subprocess, "run", run)
    monkeypatch.setattr(cmux.time, "sleep", lambda s: None)
    with pytest.raises(cmux.CmuxError, match="Couldn't set up tmux") as e:
        cmux.prepare("h.ona.environment", "s")
    assert e.value.detail == "ssh took too long to answer."
