from __future__ import annotations

from datetime import UTC, datetime

import pytest
from conftest import raw_env

from oo.envs import Ambiguous, Env, NoMatch, matches, pick, sort_envs


def env(*args, **kwargs) -> Env:
    return Env.from_json(raw_env(*args, **kwargs))


ENVS = [
    env("11111111-aaaa", "feat-10", "running", active="2026-09-30T10:00:00Z"),
    env("22222222-bbbb", "feat-10-2", "stopped", active="2026-09-29T10:00:00Z"),
    env("33333333-cccc", "Docs", "stopped", active="2026-09-28T10:00:00Z"),
    env("44444444-dddd", "review-me", "stopped", branch="kim/review-me"),
    env("55555555-eeee", "", "stopped"),
]


def test_parses_what_we_need():
    e = env(
        "abc", "x", "running", changed=["a.py", "b.py"], active="2026-09-30T18:07:04.123456789Z"
    )
    assert (e.name, e.phase, e.repo, e.changed_count) == ("x", "running", "widgets", 2)
    assert e.changes == ("a.py", "b.py")
    assert e.last_active == datetime(2026, 9, 30, 18, 7, 4, 123456, tzinfo=UTC)


def test_falls_back_to_the_spec_before_cloning():
    e = Env.from_json(
        {
            "id": "abc",
            "metadata": {"name": "new"},
            "spec": {
                "content": {
                    "initializer": {
                        "specs": [
                            {"git": {"cloneTarget": "sam/new", "remoteUri": "https://x/y.git"}}
                        ]
                    }
                }
            },
            "status": {"phase": "ENVIRONMENT_PHASE_CREATING"},
        }
    )
    assert (e.label, e.repo, e.phase, e.last_active) == ("new", "y", "creating", None)


def test_the_label_is_the_name_never_the_branch():
    assert env("abc", "AuthFix", branch="sam/auth").label == "AuthFix"


def test_without_a_name_the_label_is_the_end_of_the_id():
    assert ENVS[4].label == ENVS[4].short_id == "5-eeee"


def test_branches_arent_something_to_match_on():
    assert matches(ENVS, "kim") == []
    assert matches(ENVS, "main") == []


def test_shows_deleting_and_stopping_as_soon_as_theyre_asked_for():
    assert env("abc", "x", "running", desired="deleted").phase == "deleting"
    assert env("abc", "x", "stopped", desired="deleted").phase == "deleting"
    assert env("abc", "x", "deleted", desired="deleted").phase == "deleted"
    assert env("abc", "x", "running", desired="stopped").phase == "stopping"
    assert env("abc", "x", "starting", desired="stopped").phase == "stopping"
    assert env("abc", "x", "stopped", desired="stopped").phase == "stopped"


def ids(envs: list[Env]) -> list[str]:
    return [e.id[:2] for e in envs]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("feat-10", ["11"]),  # exact name beats the start of feat-10-2
        ("feat-10-2", ["22"]),
        ("feat", ["11", "22"]),  # start of a name
        ("Docs", ["33"]),  # exact name
        ("DOCS", ["33"]),  # names ignore case
        ("oc", ["33"]),  # any part of a name
        ("me", ["44"]),
        ("4444", ["44"]),  # ID prefix
        ("-eeee", ["55"]),  # ID suffix
        ("11111111-aaaa", ["11"]),  # full ID
        ("nope", []),
        ("  ", []),
    ],
)
def test_matches(query, expected):
    assert ids(matches(ENVS, query)) == expected


def test_pick_complains_about_different_names():
    with pytest.raises(Ambiguous) as e:
        pick(ENVS, "feat")
    assert ids(e.value.candidates) == ["11", "22"]


def test_pick_complains_when_nothing_matches():
    with pytest.raises(NoMatch):
        pick(ENVS, "nope")


def test_pick_wont_guess_between_environments_with_one_name():
    twins = [
        env("0000-aaaaaa", "twin", "stopped", active="2026-09-30T11:00:00Z"),
        env("0000-bbbbbb", "twin", "running", active="2026-09-30T09:00:00Z"),
    ]
    with pytest.raises(Ambiguous) as e:
        pick(twins, "twin")
    assert [c.short_id for c in e.value.candidates] == ["bbbbbb", "aaaaaa"]  # running first
    assert pick(twins, "aaaaaa").id == "0000-aaaaaa"


def test_sort_puts_running_first_then_recent():
    shuffled = [ENVS[2], ENVS[1], ENVS[0], env("66", "starting-one", "starting")]
    assert ids(sort_envs(shuffled)) == ["11", "66", "22", "33"]
