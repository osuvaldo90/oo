"""`oo ui` driven headlessly, with Ona faked at the ops layer."""

from __future__ import annotations

import asyncio
import signal
import threading
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from conftest import raw_env
from textual.widgets import Input, Static

from oo.envs import Env
from oo.projects import EnvClass, Project
from oo.ui import app as app_module
from oo.ui import ops, screens
from oo.ui.app import OoApp
from oo.ui.ops import UiError

RAW = [
    raw_env("11111111-aaaa", "feat-10", "running", created="2026-09-20T00:00:00Z"),
    raw_env("22222222-bbbb", "docs", changed=["src/a.py"]),
    raw_env("33333333-cccc", "spike"),
]
WIDGETS = Project(id="p-1", name="Widgets", class_ids=("c-fr",))
FRANKFURT = EnvClass(id="c-fr", name="m7i.xlarge", runner="Frankfurt")


@pytest.fixture
def envs() -> list[Env]:
    return [Env.from_json(r) for r in RAW]


@pytest.fixture
def fake(monkeypatch, envs):
    """Ona, as the screen sees it: canned answers and a record of what was asked."""
    record: list[tuple] = []

    def start(ids, *, wait, on_tick=None):
        record.append(("start", tuple(ids), wait))
        return 4.0

    def create(project, env_class, name, before, on_tick=None):
        record.append(("create", project.id, env_class.id if env_class else None, name))
        made = Env.from_json(raw_env("99999999-new", name or "", "running"))
        envs.append(made)
        return made, 30.0

    monkeypatch.setattr(ops, "list_envs", lambda timeout=10.0: list(envs))
    monkeypatch.setattr(ops, "start", start)
    monkeypatch.setattr(ops, "stop", lambda ids: record.append(("stop", tuple(ids))))
    monkeypatch.setattr(
        ops, "delete", lambda ids: (record.append(("delete", tuple(ids))), (["docs"], None))[1]
    )
    monkeypatch.setattr(ops, "would_forget", lambda ids: ["docs"])
    monkeypatch.setattr(ops, "create", create)
    monkeypatch.setattr(ops, "catalog", lambda: ([WIDGETS], [FRANKFURT]))
    monkeypatch.setattr(
        ops,
        "open_editor",
        lambda env_id, editor, on_tick=None: (record.append(("open", env_id, editor)), 2.0)[1],
    )
    monkeypatch.setattr(ops, "editors", lambda: [ops.Editor("1", "cursor", "Cursor")])
    monkeypatch.setattr(ops, "check_claude", lambda: None)
    monkeypatch.setattr(ops, "claude_needs_consent", lambda: False)
    monkeypatch.setattr(
        ops,
        "open_in_claude",
        lambda env, envs, label: (record.append(("claude", env.id, label)), ["Set up"])[1],
    )
    monkeypatch.setattr(ops, "ensure_ssh", lambda env_id: False)
    monkeypatch.setattr(ops, "ssh_argv", lambda env_id: ["ona", "environment", "ssh", env_id])

    def run_in_terminal(self, argv, heading):
        record.append(("terminal", tuple(argv)))
        return 0

    monkeypatch.setattr(OoApp, "run_in_terminal", run_in_terminal)
    return record


async def settle(app: OoApp, pilot) -> None:
    """Let workers finish and their results land."""
    for _ in range(3):
        await pilot.pause()
        await app.workers.wait_for_complete()
    await pilot.pause()


def run(test: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(test())


def ids(app: OoApp) -> list[str]:
    return [e.id for e in app.rows]


def phases(app: OoApp) -> dict[str, str]:
    return {e.id: e.phase for e in app.rows}


def test_lists_running_first_and_shows_the_header(fake):
    async def go():
        app = OoApp(environ={"OO_CONNECT": "here"})
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            assert ids(app) == ["11111111-aaaa", "22222222-bbbb", "33333333-cccc"]
            header = str(app.query_one("#header", Static).render())
            assert "3 environments, 1 running" in header
            assert "opens ssh here" in header
            assert app.cursor_id == "11111111-aaaa"
            assert fake == []

    run(go)


def test_stop_asks_ona_and_shows_stopping_at_once(fake):
    async def go():
        app = OoApp(environ={})
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            await pilot.press("x")
            assert phases(app)["11111111-aaaa"] == "stopping"
            await settle(app, pilot)
            assert ("stop", ("11111111-aaaa",)) in fake
            assert app.events[-1].message == "Stopping feat-10"
            # Ona still says running, so the overlay stays until it moves.
            assert phases(app)["11111111-aaaa"] == "stopping"

    run(go)


def test_start_on_a_running_one_is_refused_quietly(fake):
    async def go():
        app = OoApp(environ={})
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            await pilot.press("s")
            await settle(app, pilot)
            assert not [c for c in fake if c[0] == "start"]

    run(go)


def test_marks_make_one_batch_call(fake):
    async def go():
        app = OoApp(environ={})
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            await pilot.press("j", "space", "space")
            assert app.marks == {"22222222-bbbb", "33333333-cccc"}
            await pilot.press("s")
            await settle(app, pilot)
            assert ("start", ("22222222-bbbb", "33333333-cccc"), False) in fake
            assert app.marks == set()
            assert phases(app)["22222222-bbbb"] == "starting"

    run(go)


def test_connect_to_a_running_one_hands_over_the_terminal(fake):
    async def go():
        app = OoApp(environ={"OO_CONNECT": "here"})
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            await pilot.press("c")
            await settle(app, pilot)
            assert ("terminal", ("ona", "environment", "ssh", "11111111-aaaa")) in fake
            assert app.events[-1].message == "Back from feat-10"

    run(go)


def test_connect_to_a_stopped_one_starts_it_then_asks(fake):
    async def go():
        app = OoApp(environ={"OO_CONNECT": "here"})
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            await pilot.press("j", "c")
            await settle(app, pilot)
            assert ("start", ("22222222-bbbb",), True) in fake
            assert isinstance(app.screen, screens.Ready)
            await pilot.press("enter")
            await settle(app, pilot)
            assert ("terminal", ("ona", "environment", "ssh", "22222222-bbbb")) in fake

    run(go)


def test_staying_put_after_a_start_connects_nothing(fake):
    async def go():
        app = OoApp(environ={"OO_CONNECT": "here"})
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            await pilot.press("j", "c")
            await settle(app, pilot)
            assert isinstance(app.screen, screens.Ready)
            await pilot.press("escape")
            await settle(app, pilot)
            assert not [c for c in fake if c[0] == "terminal"]

    run(go)


def test_delete_a_clean_stopped_one_takes_a_y(fake):
    async def go():
        app = OoApp(environ={})
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            await pilot.press("j", "j", "D")
            assert isinstance(app.screen, screens.ConfirmDelete)
            assert app.screen.gate == "y"
            await pilot.press("y")
            await settle(app, pilot)
            assert ("delete", ("33333333-cccc",)) in fake
            assert phases(app)["33333333-cccc"] == "deleting"
            assert "removed docs from the Claude desktop app" in app.events[-1].detail

    run(go)


def test_delete_one_with_changes_wants_its_name(fake):
    async def go():
        app = OoApp(environ={})
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            await pilot.press("j", "D")
            screen = app.screen
            assert isinstance(screen, screens.ConfirmDelete) and screen.gate == "name"
            await pilot.press("enter")  # nothing typed yet: nothing happens
            assert app.screen is screen
            await pilot.press(*"doc", "enter")
            assert app.screen is screen
            await pilot.press("s", "enter")
            await settle(app, pilot)
            assert ("delete", ("22222222-bbbb",)) in fake

    run(go)


def test_batch_delete_with_a_risky_one_wants_the_word(fake):
    async def go():
        app = OoApp(environ={})
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            await pilot.press("asterisk", "D")
            screen = app.screen
            assert isinstance(screen, screens.ConfirmDelete) and screen.gate == "word"
            await pilot.press("escape")
            await settle(app, pilot)
            assert not [c for c in fake if c[0] == "delete"]
            await pilot.press("D")
            await pilot.press(*"delete", "enter")
            await settle(app, pilot)
            assert ("delete", ("11111111-aaaa", "22222222-bbbb", "33333333-cccc")) in fake

    run(go)


def test_filter_narrows_and_escape_clears(fake):
    async def go():
        app = OoApp(environ={})
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            await pilot.press("slash", *"doc")
            assert ids(app) == ["22222222-bbbb"]
            await pilot.press("escape")
            assert len(ids(app)) == 3
            assert app.query_one("#filter", Input).has_class("hidden")
            await pilot.press("slash", *"stopped")
            assert ids(app) == ["22222222-bbbb", "33333333-cccc"]

    run(go)


def test_filter_with_no_match_offers_a_suggestion(fake):
    async def go():
        app = OoApp(environ={})
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            await pilot.press("slash", *"dcos")
            assert ids(app) == []
            event = str(app.query_one("#event", Static).render())
            assert "Did you mean docs?" in event
            await pilot.press("enter")
            assert app.query_one("#filter", Input).value == "docs"
            assert ids(app) == ["22222222-bbbb"]

    run(go)


def test_open_in_editor_uses_the_default(fake):
    async def go():
        app = OoApp(environ={"OO_EDITOR": "cursor"})
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            await pilot.press("o")
            await settle(app, pilot)
            assert ("open", "11111111-aaaa", "cursor") in fake
            assert app.events[-1].message == "Opened feat-10 in cursor"

    run(go)


def test_pick_an_editor(fake):
    async def go():
        app = OoApp(environ={})
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            await pilot.press("O")
            await settle(app, pilot)
            assert isinstance(app.screen, screens.PickEditor)
            await pilot.press("enter")
            await settle(app, pilot)
            assert ("open", "11111111-aaaa", "cursor") in fake

    run(go)


def test_open_in_claude_logs_what_it_set_up(fake):
    async def go():
        app = OoApp(environ={})
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            await pilot.press("a")
            await settle(app, pilot)
            assert ("claude", "11111111-aaaa", "feat-10") in fake
            assert [e.message for e in app.events[-2:]] == [
                "Set up",
                "Opened feat-10 in the Claude desktop app",
            ]

    run(go)


def test_first_claude_use_asks_first(fake, monkeypatch):
    monkeypatch.setattr(ops, "claude_needs_consent", lambda: True)

    async def go():
        app = OoApp(environ={})
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            await pilot.press("a")
            assert isinstance(app.screen, screens.Consent)
            await pilot.press("escape")
            await settle(app, pilot)
            assert not [c for c in fake if c[0] == "claude"]

    run(go)


def test_new_environment_form_creates_then_offers_to_connect(fake):
    async def go():
        app = OoApp(environ={"OO_CONNECT": "here"})
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            await pilot.press("n")
            await settle(app, pilot)
            form = app.screen
            assert isinstance(form, screens.NewEnvironment)
            await pilot.press(*"login-fix")
            await pilot.press("enter")
            await settle(app, pilot)
            assert ("create", "p-1", "c-fr", "login-fix") in fake
            assert app.ghosts == []
            assert isinstance(app.screen, screens.Ready)
            await pilot.press("enter")
            await settle(app, pilot)
            assert ("terminal", ("ona", "environment", "ssh", "99999999-new")) in fake

    run(go)


def test_a_failed_action_marks_the_row_and_keeps_the_hint(fake, monkeypatch):
    def broken(ids):
        raise UiError("Ona couldn't stop it.", detail="locked", hint="try later")

    monkeypatch.setattr(ops, "stop", broken)

    async def go():
        app = OoApp(environ={})
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            await pilot.press("x")
            await settle(app, pilot)
            assert "11111111-aaaa" in app.failures
            assert phases(app)["11111111-aaaa"] == "running"
            assert app.events[-1].mark == "✗"
            details = str(app.query_one("#details", Static).render())
            assert "locked" in details and "try later" in details

    run(go)


def test_auth_trouble_shows_a_banner(fake, monkeypatch):
    def expired(timeout=10.0):
        raise UiError("ona couldn't list your environments.", detail="Error: unauthenticated")

    monkeypatch.setattr(ops, "list_envs", expired)

    async def go():
        app = OoApp(environ={})
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            banner = app.query_one("#banner", Static)
            assert not banner.has_class("hidden")
            assert "ona login" in str(banner.render())

    run(go)


def test_quit_asks_while_something_is_in_flight(fake, monkeypatch):
    gate = threading.Event()

    def slow_open(env_id, editor, on_tick=None):
        gate.wait(timeout=10)
        return 1.0

    monkeypatch.setattr(ops, "open_editor", slow_open)

    async def go():
        app = OoApp(environ={})
        async with app.run_test(size=(120, 40)) as pilot:
            try:
                await settle(app, pilot)
                await pilot.press("o")
                await pilot.pause()
                await pilot.press("q")
                await pilot.pause()
                assert isinstance(app.screen, screens.ConfirmQuit)
                await pilot.press("escape")
            finally:
                gate.set()
            await settle(app, pilot)

    run(go)


def test_unknown_connect_preference_falls_back_to_auto(fake):
    async def go():
        app = OoApp(environ={"OO_CONNECT": "teleport"})
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            assert app.connect_pref == "auto"

    run(go)


def test_run_refuses_without_a_terminal(monkeypatch, capsys):
    from oo import ui

    monkeypatch.setattr(ui.sys.stdin, "isatty", lambda: False)
    assert ui.run() == 1
    assert "needs a terminal" in capsys.readouterr().err


def test_cli_has_the_ui_command():
    from typer.testing import CliRunner

    from oo import cli

    result = CliRunner().invoke(cli.app, ["--help"])
    assert "ui" in result.output
    assert app_module.POLL_IDLE > app_module.POLL_BUSY


def test_the_child_gets_ctrl_c_and_we_keep_our_handler(monkeypatch):
    """The ssh child takes SIGINT itself; the app ignores it meanwhile and gets its handler back."""
    prompts = []
    monkeypatch.setattr("builtins.input", lambda: prompts.append("asked"))
    before = signal.getsignal(signal.SIGINT)
    app = OoApp(environ={})
    from rich.text import Text

    code = app._run_child(["sh", "-c", "kill -INT $$"], Text("→ test"))
    assert code != 0  # the child died of SIGINT, so it wasn't ignoring it
    assert signal.getsignal(signal.SIGINT) is before
    assert prompts == ["asked"]
    assert app._run_child(["sh", "-c", "exit 0"], Text("→ test")) == 0
    assert prompts == ["asked"]


def test_missing_textual_gets_a_hint_with_the_extra(monkeypatch, capsys):
    import sys

    from oo import ui

    monkeypatch.setattr(ui.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(ui.sys.stdout, "isatty", lambda: True)
    monkeypatch.setitem(sys.modules, "textual", None)
    for name in [m for m in sys.modules if m.startswith("oo.ui.") and m != "oo.ui"]:
        monkeypatch.delitem(sys.modules, name)
    assert ui.run() == 1
    assert "oo[ui]" in capsys.readouterr().err


def test_a_stale_default_project_is_not_offered():
    stale = Env.from_json(raw_env("55555555-eeee", "old", project="gone"))
    assert ops.pick_defaults([WIDGETS], [FRANKFURT], [stale]) == (None, None)


def test_marks_hidden_by_the_filter_still_count(fake):
    async def go():
        app = OoApp(environ={})
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(app, pilot)
            await pilot.press("j", "space")
            assert app.marks == {"22222222-bbbb"}
            await pilot.press(
                "slash", *"spike", "enter"
            )  # Enter keeps the filter, focuses the table
            assert ids(app) == ["33333333-cccc"]
            await pilot.press("s")
            await settle(app, pilot)
            assert ("start", ("22222222-bbbb",), False) in fake
            assert app.marks == set()

    run(go)
