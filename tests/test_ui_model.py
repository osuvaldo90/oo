from __future__ import annotations

from conftest import raw_env

from oo.envs import Env
from oo.ui import model
from oo.ui.model import Ops, Overlays


def envs() -> list[Env]:
    return [
        Env.from_json(raw_env("11111111-aaaa", "feat-10", "running")),
        Env.from_json(raw_env("22222222-bbbb", "feat-10-2")),
        Env.from_json(raw_env("33333333-cccc", "docs")),
    ]


def test_search_uses_the_same_tiers_as_the_commands():
    found = model.search(envs(), "docs")
    assert [e.id for e in found] == ["33333333-cccc"]
    found = model.search(envs(), "feat-10")
    assert [e.id for e in found] == ["11111111-aaaa"]


def test_search_by_phase_word_and_empty_query():
    assert [e.id for e in model.search(envs(), "stopped")] == [
        "22222222-bbbb",
        "33333333-cccc",
    ]
    assert len(model.search(envs(), "  ")) == 3


def test_suggestion_and_twins():
    assert model.suggestion(envs(), "dcos") == "docs"
    assert model.suggestion(envs(), "zzzzzz") is None
    assert not model.twins(envs())
    twin = Env.from_json(raw_env("44444444-dddd", "docs"))
    assert model.twins([*envs(), twin])


def test_overlay_shows_until_ona_moves_then_steps_aside():
    now = [100.0]
    overlays = Overlays(clock=lambda: now[0])
    stopped = envs()[2]
    overlays.set(stopped, "starting")
    shown, expired = overlays.apply([stopped])
    assert shown[0].phase == "starting" and not expired
    # Ona catches up: its own phase wins and the overlay is gone.
    from dataclasses import replace

    shown, _ = overlays.apply([replace(stopped, phase="running")])
    assert shown[0].phase == "running"
    assert stopped.id not in overlays


def test_overlay_expires_and_says_so():
    now = [100.0]
    overlays = Overlays(clock=lambda: now[0])
    stopped = envs()[2]
    overlays.set(stopped, "deleting")
    now[0] += model.OVERLAY_TIMEOUT + 1
    shown, expired = overlays.apply([stopped])
    assert shown[0].phase == "stopped"
    assert expired == [stopped.id]


def test_overlay_forgets_environments_that_are_gone():
    overlays = Overlays()
    gone = envs()[2]
    overlays.set(gone, "deleting")
    overlays.apply(envs()[:2])
    assert gone.id not in overlays


def test_ops_track_who_is_busy():
    ops = Ops(clock=lambda: 5.0)
    op = ops.start("Starting", ("a", "b"))
    assert ops.busy("a") is op and ops.busy("c") is None
    assert op.elapsed(8.0) == 3.0
    ops.finish(op, "done")
    assert ops.busy("a") is None and len(ops) == 0
