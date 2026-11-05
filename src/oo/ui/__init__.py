"""`oo ui`: one screen that keeps an eye on your environments and acts on them."""

from __future__ import annotations

import sys

from rich.console import Console

from .. import render

# Rich reads [ui] as markup, so the brackets are escaped.
INSTALL_HINT = (
    "Install oo with its ui extra: [bold]uv tool install 'oo\\[ui] @ git+https://github.com/"
    "adamtrain/oo'[/], or [bold]uv tool install --editable '.\\[ui]'[/] from a checkout."
)


def run() -> int:
    """Start the screen. Returns an exit code, having said why if it can't start."""
    err = Console(stderr=True, highlight=False)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        render.error(err, "oo ui needs a terminal.", hint="Run it where you can see it.")
        return 1
    try:
        from .app import OoApp
    except ModuleNotFoundError as e:
        if e.name and e.name.split(".")[0] == "textual":
            render.error(err, "oo ui needs textual, which isn't installed.", hint=INSTALL_HINT)
            return 1
        raise
    OoApp().run()
    return 0
