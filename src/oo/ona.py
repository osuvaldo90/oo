"""The thin layer that actually runs the `ona` CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any, NoReturn

from rich.markup import escape

from .envs import Env
from .projects import EnvClass, Project

# ona processes `quietly` is running right now, so a long-running caller can kill them on exit.
LIVE: set[subprocess.Popen[str]] = set()


class OnaError(Exception):
    def __init__(self, message: str, detail: str | None = None, hint: str | None = None):
        super().__init__(message)
        self.detail = detail
        self.hint = hint


def _binary() -> str:
    path = shutil.which("ona")
    if not path:
        raise OnaError(
            "Couldn't find the ona CLI.",
            hint="Install it and make sure [bold]ona[/] is on your PATH, then [bold]ona login[/].",
        )
    return path


def error_from(message: str, output: str) -> OnaError:
    """Turn what ona printed when it failed into an error worth showing.

    ona prints `Error: …` lines, sometimes buried in usage text, and sometimes a list of
    "Possible resolutions", which become the hint.
    """
    lines = [line.strip() for line in output.splitlines()]
    errors = [line.removeprefix("Error:").strip() for line in lines if line.startswith("Error:")]
    # Without an Error: line, the last thing it said is the best guess at what went wrong.
    detail = "; ".join(errors) or next((line for line in reversed(lines) if line), "") or None
    fixes = []
    if "Possible resolutions:" in lines:
        for line in lines[lines.index("Possible resolutions:") + 1 :]:
            if not line.startswith("- "):
                break
            fixes.append(escape(line))
    auth_words = ("login", "unauthenticated", "unauthorized", "expired")
    if detail and any(w in detail.lower() for w in auth_words):
        fixes.append("You may need to log in again: [bold]ona login[/]")
    return OnaError(message, detail=detail, hint="\n  ".join(fixes) or None)


def _list(what: str, *args: str, timeout: float | None = None) -> list[dict[str, Any]]:
    try:
        done = subprocess.run(
            [_binary(), *args, "--format", "json"], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as e:
        raise OnaError(f"ona took too long to list {what}.") from e
    if done.returncode != 0:
        raise error_from(f"ona couldn't list {what}.", done.stderr)
    try:
        return json.loads(done.stdout or "[]") or []
    except json.JSONDecodeError as e:
        raise OnaError("ona sent back something that isn't JSON.", detail=str(e)) from e


def list_envs(timeout: float | None = None) -> list[Env]:
    items = _list("your environments", "environment", "list", timeout=timeout)
    return [Env.from_json(item) for item in items]


def list_projects() -> list[Project]:
    return [Project.from_json(item) for item in _list("projects", "project", "list")]


def list_classes() -> list[EnvClass]:
    items = _list("environment classes", "environment", "list-classes")
    return [EnvClass.from_json(item) for item in items if item.get("enabled", True)]


def quietly(
    *args: str, failed: str, on_tick: Callable[[float], None] | None = None
) -> tuple[float, str]:
    """Run an ona command without letting it print, calling `on_tick` about once a second.

    Returns how many seconds it took and what it would have printed. If it fails, raises an
    OnaError saying `failed`, with whatever ona said about why.
    """
    started = time.monotonic()
    process = subprocess.Popen(
        [_binary(), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    LIVE.add(process)
    try:
        while True:
            try:
                output, _ = process.communicate(timeout=1)
                break
            except subprocess.TimeoutExpired:
                if on_tick:
                    on_tick(time.monotonic() - started)
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        LIVE.discard(process)
    if process.returncode:
        raise error_from(failed, output or "")
    return time.monotonic() - started, output or ""


def replace_with(*args: str) -> NoReturn:
    """Become an ona command, so SSH gets the terminal all to itself."""
    binary = _binary()
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(binary, [binary, *args])
