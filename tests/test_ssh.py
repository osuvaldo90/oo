from __future__ import annotations

import pytest

from oo.ssh import Spot


@pytest.mark.parametrize(
    ("arg", "spot"),
    [
        ("notes.md", Spot("notes.md")),
        ("login:", Spot("", remote=True, name="login")),
        ("login-fix:src/a.py", Spot("src/a.py", remote=True, name="login-fix")),
        (":out.csv", Spot("out.csv", remote=True)),
        ("login:a:b", Spot("a:b", remote=True, name="login")),
        ("./odd:name", Spot("./odd:name")),
        ("/tmp/odd:name", Spot("/tmp/odd:name")),
        ("~/odd:name", Spot("~/odd:name")),
    ],
)
def test_parse_tells_local_from_remote(arg, spot):
    assert Spot.parse(arg) == spot


@pytest.mark.parametrize(
    ("path", "resolved"),
    [
        ("", "/workspaces/web"),
        ("src/a.py", "/workspaces/web/src/a.py"),
        ("/tmp/x", "/tmp/x"),
        ("~/x", "~/x"),
    ],
)
def test_relative_paths_start_in_the_repo(path, resolved):
    assert Spot(path, remote=True).resolved("/workspaces/web") == resolved


def test_relative_paths_start_at_home_when_the_repo_is_unknown():
    assert Spot("src/a.py", remote=True).resolved("") == "src/a.py"
