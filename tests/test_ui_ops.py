from __future__ import annotations

import json

import pytest
from conftest import raw_env

from oo import claude, ona, ssh
from oo.envs import Env
from oo.ona import OnaError
from oo.projects import EnvClass, Project
from oo.ui import ops
from oo.ui.ops import UiError

RAW = [
    raw_env("11111111-aaaa", "sam/feat-10", "running"),
    raw_env("22222222-bbbb", "sam/docs"),
]
WIDGETS = Project(id="p-1", name="Widgets", class_ids=("c-fr",))
FRANKFURT = EnvClass(id="c-fr", name="m7i.xlarge", runner="Frankfurt")


@pytest.fixture
def envs() -> list[Env]:
    return [Env.from_json(r) for r in RAW]


@pytest.fixture
def calls(monkeypatch, envs):
    record: list[tuple[str, ...]] = []

    def quietly(*args, failed, on_tick=None):
        record.append(args)
        if args[:2] == ("environment", "create"):
            envs.append(Env.from_json(raw_env("99999999-new", "main", "running")))
            return 3.0, "99999999-new\n"
        return 2.0, ""

    monkeypatch.setattr(ona, "list_envs", lambda timeout=None: list(envs))
    monkeypatch.setattr(ona, "quietly", quietly)
    monkeypatch.setattr(ssh, "configured", lambda env_id: True)
    return record


def test_ui_errors_carry_what_ona_said_and_spot_auth():
    e = UiError("Ona couldn't list.", detail="Error: unauthenticated", hint="ona login")
    assert e.about_auth
    assert not UiError("Ona couldn't list.", detail="not found").about_auth


def test_list_envs_wraps_ona_errors(monkeypatch):
    def broken(timeout=None):
        raise OnaError("ona couldn't list your environments.", detail="token expired")

    monkeypatch.setattr(ona, "list_envs", broken)
    with pytest.raises(UiError) as e:
        ops.list_envs()
    assert e.value.detail == "token expired" and e.value.about_auth


def test_start_stop_delete_pass_the_right_flags(calls):
    took = ops.start(["a", "b"], wait=False)
    assert took == 2.0
    ops.start(["a"], wait=True)
    ops.stop(["a", "b"])
    assert calls == [
        ("environment", "start", "a", "b", "--dont-wait"),
        ("environment", "start", "a"),
        ("environment", "stop", "a", "b", "--dont-wait"),
    ]


def test_delete_tidies_the_claude_app(calls, ssh_configs):
    ssh_configs.write_text(
        json.dumps(
            {
                "configs": [
                    {"sshHost": "22222222-bbbb.ona.environment", "name": "docs"},
                    {"sshHost": "11111111-aaaa.ona.environment", "name": "feat"},
                ],
                "trustedHosts": [],
            }
        )
    )
    assert ops.would_forget(["22222222-bbbb"]) == ["docs"]
    dropped, warning = ops.delete(["22222222-bbbb"])
    assert calls == [("environment", "delete", "22222222-bbbb", "--dont-wait")]
    assert dropped == ["docs"] and warning is None
    left = json.loads(ssh_configs.read_text())["configs"]
    assert [c["name"] for c in left] == ["feat"]


def test_create_finds_the_new_environment(calls, envs):
    before = list(envs)
    created, took = ops.create(WIDGETS, FRANKFURT, "login-fix", before)
    assert created.id == "99999999-new" and took == 3.0
    assert calls == [
        ("environment", "create", "p-1", "--class-id", "c-fr", "--name", "login-fix"),
    ]


def test_create_without_a_class_or_name(calls, envs):
    ops.create(WIDGETS, None, "", list(envs))
    assert calls == [("environment", "create", "p-1")]


def test_open_editor_starts_if_it_has_to(calls):
    ops.open_editor("a", "cursor")
    ops.open_editor("a", None)
    assert calls == [
        ("environment", "open", "a", "--start", "--editor", "cursor"),
        ("environment", "open", "a", "--start"),
    ]


def test_ensure_ssh_runs_ssh_config_once(calls, monkeypatch):
    known = {"ready": False}

    def configured(env_id):
        return known["ready"]

    def quietly(*args, failed, on_tick=None):
        calls.append(args)
        known["ready"] = True
        return 1.0, ""

    monkeypatch.setattr(ssh, "configured", configured)
    monkeypatch.setattr(ona, "quietly", quietly)
    assert ops.ensure_ssh("a") is True
    assert ops.ensure_ssh("a") is False
    assert calls == [("environment", "ssh-config")]


def test_editors_come_from_ona(monkeypatch, tmp_path):
    script = tmp_path / "ona"
    listing = json.dumps([{"id": "1", "alias": "cursor", "name": "Cursor"}, {"id": "2"}])
    script.write_text(f"#!/bin/sh\necho '{listing}'\n")
    script.chmod(0o755)
    monkeypatch.setattr(ops.shutil, "which", lambda name: str(script))
    found = ops.editors()
    assert found == [ops.Editor(id="1", alias="cursor", name="Cursor")]
    assert ops.ssh_argv("env-1") == [str(script), "environment", "ssh", "env-1"]


def test_missing_ona_is_an_error(monkeypatch):
    monkeypatch.setattr(ops.shutil, "which", lambda name: None)
    with pytest.raises(UiError, match="ona CLI"):
        ops.ssh_argv("x")


def test_open_in_claude_falls_back_to_onas_host(calls, envs, monkeypatch):
    opened = []

    def set_up_host(env_id, live_ids):
        raise claude.ClaudeError("Nothing is listening on port 2222.", hint="no sshd")

    monkeypatch.setattr(claude, "set_up_host", set_up_host)
    monkeypatch.setattr(claude, "open_link", lambda url: opened.append(url))
    notes = ops.open_in_claude(envs[0], envs, "feat-10")
    assert any("Ona's own SSH host instead" in n for n in notes)
    assert opened == [claude.link("11111111-aaaa", "/workspaces/widgets")]


def test_pick_defaults_follow_the_newest_environment(envs):
    project, env_class = ops.pick_defaults([WIDGETS], [FRANKFURT], envs)
    assert project == WIDGETS and env_class == FRANKFURT
    assert ops.pick_defaults([WIDGETS], [FRANKFURT], []) == (None, None)
