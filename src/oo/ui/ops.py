"""What `oo ui` asks Ona to do: the same steps as the commands, as plain calls.

Nothing here prints or asks. Every function returns data or raises UiError, so the screen can
run it on a worker thread and show the result. Progress goes through `on_tick(seconds)`, as
`ona.quietly` does.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .. import claude, ona, ssh
from ..envs import Env, name_taken, safe_name, sort_envs
from ..ona import OnaError
from ..projects import ChoiceError, EnvClass, Project

Tick = Callable[[float], None] | None

AUTH_WORDS = ("login", "unauthenticated", "unauthorized", "expired")
LIST_TIMEOUT = 10.0


class UiError(Exception):
    """One kind of error for the screen, with what oo's errors carry: message, detail, hint."""

    def __init__(self, message: str, detail: str | None = None, hint: str | None = None):
        super().__init__(message)
        self.detail = detail
        self.hint = hint

    @property
    def about_auth(self) -> bool:
        """Whether this looks like Ona wants you to log in again."""
        text = f"{self} {self.detail or ''}".lower()
        return any(word in text for word in AUTH_WORDS)


def _wrap(e: Exception) -> UiError:
    detail = getattr(e, "detail", None)
    hint = getattr(e, "hint", None)
    return UiError(str(e), detail=detail, hint=hint)


@dataclass(frozen=True, slots=True)
class Editor:
    id: str
    alias: str
    name: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Editor:
        return cls(id=data.get("id", ""), alias=data.get("alias", ""), name=data.get("name", ""))


def ona_binary() -> str:
    path = shutil.which("ona")
    if not path:
        raise UiError(
            "Couldn't find the ona CLI.",
            hint="Install it and make sure ona is on your PATH, then run ona login.",
        )
    return path


def list_envs(timeout: float = LIST_TIMEOUT) -> list[Env]:
    try:
        return sort_envs(ona.list_envs(timeout=timeout))
    except OnaError as e:
        raise _wrap(e) from e


def catalog() -> tuple[list[Project], list[EnvClass]]:
    """Projects and environment classes, asked for at the same time."""
    try:
        with ThreadPoolExecutor() as pool:
            projects = pool.submit(ona.list_projects)
            classes = pool.submit(ona.list_classes)
            return projects.result(), classes.result()
    except OnaError as e:
        raise _wrap(e) from e


def editors() -> list[Editor]:
    """The editors Ona can open an environment in."""
    try:
        done = subprocess.run(
            [ona_binary(), "environment", "list-editors", "--format", "json"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=LIST_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise UiError("ona took too long to list editors.") from e
    if done.returncode:
        raise _wrap(ona.error_from("ona couldn't list editors.", done.stderr))
    try:
        items = json.loads(done.stdout or "[]") or []
    except json.JSONDecodeError as e:
        raise UiError("ona sent back something that isn't JSON.", detail=str(e)) from e
    return [Editor.from_json(item) for item in items if item.get("alias")]


def _quietly(*args: str, failed: str, on_tick: Tick = None) -> tuple[float, str]:
    try:
        return ona.quietly(*args, failed=failed, on_tick=on_tick)
    except OnaError as e:
        raise _wrap(e) from e


def start(ids: list[str], *, wait: bool, on_tick: Tick = None) -> float:
    """Start environments. Without `wait`, ona returns as soon as it has asked."""
    took, _ = _quietly(
        "environment",
        "start",
        *ids,
        *([] if wait else ["--dont-wait"]),
        failed="Ona couldn't start it." if len(ids) == 1 else "Ona couldn't start them.",
        on_tick=on_tick,
    )
    return took


def stop(ids: list[str]) -> None:
    """Ask Ona to stop environments, and come back at once."""
    _quietly(
        "environment",
        "stop",
        *ids,
        "--dont-wait",
        failed="Ona couldn't stop it." if len(ids) == 1 else "Ona couldn't stop them.",
    )


def delete(ids: list[str]) -> tuple[list[str], str | None]:
    """Ask Ona to delete environments, then take them out of the Claude desktop app's picker.

    Returns the names the app showed for the connections dropped, and a warning if the app's
    list couldn't be tidied. Ona finishes the deletion in the background.
    """
    _quietly(
        "environment",
        "delete",
        *ids,
        "--dont-wait",
        failed="Ona couldn't delete it." if len(ids) == 1 else "Ona couldn't delete them.",
    )
    doomed = set(ids)
    try:
        return claude.forget(lambda env_id: env_id in doomed), None
    except claude.ClaudeError as e:
        return [], f"Couldn't tidy up the Claude desktop app: {e}"


def would_forget(ids: list[str]) -> list[str]:
    """What `delete` would take out of the Claude desktop app, as the app names them."""
    doomed = set(ids)
    try:
        return claude.forget(lambda env_id: env_id in doomed, dry_run=True)
    except claude.ClaudeError:
        return []


def create(
    project: Project,
    env_class: EnvClass | None,
    name: str,
    before: list[Env],
    on_tick: Tick = None,
) -> tuple[Env, float]:
    """Create an environment and wait for Ona to start it. Returns it and how long it took.

    `before` is the list as it was, so the new environment can be told apart from the rest.
    """
    known = {e.id for e in before}
    took, output = _quietly(
        "environment",
        "create",
        project.id,
        *(["--class-id", env_class.id] if env_class else []),
        *(["--name", name] if name else []),
        failed="Ona couldn't create the environment.",
        on_tick=on_tick,
    )
    fresh = [e for e in list_envs() if e.id not in known]
    # Another `oo new` running alongside makes more than one new environment, so go by the ID
    # ona printed, and only by there being just the one new environment if it didn't print one.
    created = next((e for e in fresh if e.id in output), fresh[0] if len(fresh) == 1 else None)
    if created is None:
        raise UiError(
            "Ona created it, but I can't tell which environment is yours.",
            hint="It's in the list; find it by name.",
        )
    if name and name_taken(name, before):
        # Names double as SSH host names for the Claude app, so keep them unique, as oo new does.
        created = rename(created, f"{name}-{created.short_id}")
    return created, took


def rename(env: Env, name: str) -> Env:
    """Rename `env` to `name`, and return it as it is now. Raises UiError if Ona won't."""
    _quietly(
        "environment",
        "update",
        env.id,
        "--name",
        name,
        failed="Ona couldn't rename the environment.",
    )
    return replace(env, name=name)


def open_editor(env_id: str, editor: str | None, on_tick: Tick = None) -> float:
    """Have ona open the environment in an editor, starting it first if it has to."""
    took, _ = _quietly(
        "environment",
        "open",
        env_id,
        "--start",
        *(["--editor", editor] if editor else []),
        failed="Ona couldn't open your editor.",
        on_tick=on_tick,
    )
    return took


def ensure_ssh(env_id: str) -> bool:
    """Have ona teach ssh about its environments if it hasn't yet. True if it had to."""
    try:
        if ssh.configured(env_id):
            return False
        _quietly("environment", "ssh-config", failed="Ona couldn't set up SSH.")
        if not ssh.configured(env_id):
            raise UiError(
                "Ona set up SSH, but ssh still can't reach the environment.",
                hint="Check what ona environment ssh-config added to ~/.ssh/config.",
            )
    except ssh.SshError as e:
        raise _wrap(e) from e
    return True


def check_claude() -> None:
    """Raise UiError if the Claude desktop app clearly isn't installed."""
    try:
        claude.check_installed()
    except claude.ClaudeError as e:
        raise _wrap(e) from e


def claude_needs_consent() -> bool:
    """Whether opening in Claude would set up its SSH key for the first time on this machine."""
    return not (Path.home() / ".ssh" / claude.KEY_NAME).exists()


def open_in_claude(env: Env, envs: list[Env], label: str) -> list[str]:
    """Open a Claude Code session on `env` in the Claude desktop app.

    Returns notes worth logging: what was set up, and anything that fell back.
    """
    notes = []
    if ensure_ssh(env.id):
        notes.append("Set up SSH for Ona environments with ona environment ssh-config")
    # The app shows the host as the remote's name, so it's named after the environment when
    # that name works as a host name and no other environment has it.
    name = env.name if safe_name(env.name) and not name_taken(env.name, envs, but=env.id) else ""
    try:
        host = claude.set_up_host(env.id, {e.id for e in envs}, name=name)
        notes.append(f"Wrote an SSH host for the app, {host}, to ~/.ssh/oo/config")
    except claude.ClaudeError as e:
        host = ssh.host(env.id)
        notes.append(f"{e} Opening with Ona's own SSH host instead.")
    try:
        if called := claude.name_connection(host, label):
            notes.append(f"Shows as {called} in the app's SSH connections")
    except claude.ClaudeError as e:
        notes.append(f"Couldn't name the connection in the Claude desktop app: {e}")
    try:
        claude.open_link(claude.link(env.id, env.checkout, host=host))
    except claude.ClaudeError as e:
        raise _wrap(e) from e
    return notes


def ssh_argv(env_id: str) -> list[str]:
    """The command that becomes an SSH session on the environment."""
    return [ona_binary(), "environment", "ssh", env_id]


def login_argv() -> list[str]:
    return [ona_binary(), "login"]


def pick_defaults(
    projects: list[Project], classes: list[EnvClass], envs: list[Env]
) -> tuple[Project | None, EnvClass | None]:
    """What `oo new` would create from with no options: the newest environment's choices."""
    from ..projects import pick_class, pick_project

    try:
        project = pick_project(projects, envs, None)
    except ChoiceError:
        return None, None
    # pick_project stands in a project Ona no longer lists; the form can't offer that.
    if project.id not in {p.id for p in projects}:
        return None, None
    try:
        return project, pick_class(classes, project, envs, None)
    except ChoiceError:
        return project, None
