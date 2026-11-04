"""Handing an environment to the Claude desktop app, which connects to it over SSH itself."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from . import ssh

BUNDLE_ID = "com.anthropic.claudefordesktop"
NOT_INSTALLED_HINT = "Install it from [bold]https://claude.ai/download[/], then open it once."

# Ona's SFTP server writes files as root, which the app's install can't use. The box also runs
# its own sshd on this port, whose SFTP writes files as the box user, so the app goes through it.
SSHD_PORT = 2222
KEY_NAME = "oo_claude"
KEY_PATH = "~/.ssh/oo_claude"
CONFIG_INCLUDES = ("oo/config", "~/.ssh/oo/config")
HOST_SUFFIX = ".claude.oo"
STALE_DAYS = 30
USER_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
# What the app's connections to environments end with: oo's own host, or Ona's.
APP_HOST_SUFFIXES = (HOST_SUFFIX, ssh.host(""))


class ClaudeError(Exception):
    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


def link(env_id: str, checkout: str = "", host: str = "") -> str:
    """The link that starts a Claude Code session in the desktop app, over SSH to `env_id`.

    `host` is the SSH host to use, which is Ona's by default. The app only takes folders
    starting with `/` or `~/`, so anything else is left off and Claude Code starts in the
    home folder.
    """
    params = {"ssh_host": host or ssh.host(env_id)}
    if checkout.startswith(("/", "~/")):
        params["ssh_folder"] = checkout
    return f"claude://code/new?{urlencode(params)}"


def own_host(env_id: str, name: str = "") -> str:
    """The SSH host that reaches the environment's own sshd, through Ona's.

    It's named `name` if given, since the app shows the host as the remote's name.
    """
    return f"{name or env_id}{HOST_SUFFIX}"


def set_up_host(env_id: str, live_ids: set[str], name: str = "") -> str:
    """Set up `own_host` for `env_id`, and return its name for the app to use.

    Raises ClaudeError if that can't be done, so the caller can fall back to Ona's own host.
    `live_ids` are the environments that still exist; the others are forgotten. `name` must
    already be safe as a host name.
    """
    try:
        _ensure_included()
        key = _ensure_key()
        user, listening = _probe(env_id)
        if not listening:
            raise ClaudeError(
                f"Nothing is listening on port {SSHD_PORT} in the environment.",
                hint="The environment's own sshd isn't running.",
            )
        if not USER_PATTERN.fullmatch(user):
            raise ClaudeError(f"The environment's user name, {user!r}, looks odd.")
        _authorize(env_id, key.with_name(f"{KEY_NAME}.pub").read_text().strip())
        _write_config(env_id, user, live_ids, name)
    except ssh.SshError as e:
        raise ClaudeError(str(e), hint=e.hint) from e
    return own_host(env_id, name)


def _ssh_dir() -> Path:
    return Path.home() / ".ssh"


def _included() -> bool:
    """Whether ~/.ssh/config includes oo's file before any Host block, where ssh reads it."""
    config = _ssh_dir() / "config"
    if not config.exists():
        return False
    for line in config.read_text().splitlines():
        words = line.split()
        if not words or words[0].startswith("#"):
            continue
        keyword = words[0].lower()
        if keyword in ("host", "match"):
            return False
        if keyword == "include" and any(word in CONFIG_INCLUDES for word in words[1:]):
            return True
    return False


def _ensure_included() -> None:
    """Add oo's Include line to the top of ~/.ssh/config if it's missing, backing the file up first.

    It goes first because ssh only reads an Include before the first Host block.
    """
    if _included():
        return
    config = _ssh_dir() / "config"
    _ssh_dir().mkdir(mode=0o700, exist_ok=True)
    existing = ""
    if config.exists():
        existing = config.read_text()
        backup = config.with_name("config.oo-backup")
        if not backup.exists():
            shutil.copy2(config, backup)
    config.write_text(f"Include {CONFIG_INCLUDES[0]}\n\n{existing}")
    config.chmod(0o600)


def _ensure_key() -> Path:
    """Make the key for reaching the box's sshd, the first time only."""
    key = _ssh_dir() / KEY_NAME
    if key.exists():
        return key
    key.parent.mkdir(mode=0o700, exist_ok=True)
    done = subprocess.run(
        [ssh.binary("ssh-keygen"), "-q", "-t", "ed25519", "-N", "", "-C", "oo", "-f", str(key)],
        capture_output=True,
        text=True,
    )
    if done.returncode:
        raise ClaudeError("Couldn't make an SSH key for the Claude app.", hint=done.stderr.strip())
    return key


def _on_box(env_id: str, command: str) -> str:
    """Run `command` on the environment over the Ona connection, and return what it printed."""
    done = subprocess.run(
        [ssh.binary("ssh"), "-o", "BatchMode=yes", ssh.host(env_id), command],
        capture_output=True,
        text=True,
    )
    if done.returncode:
        raise ClaudeError(
            "Couldn't run a command on the environment over SSH.", hint=done.stderr.strip()
        )
    return done.stdout


def _probe(env_id: str) -> tuple[str, bool]:
    """The user the box logs in as, and whether its own sshd is listening."""
    out = _on_box(
        env_id,
        "id -un; "
        f"bash -c 'exec 3<>/dev/tcp/127.0.0.1/{SSHD_PORT}' 2>/dev/null && echo open || echo closed",
    )
    words = out.split()
    if len(words) < 2 or words[-1] not in ("open", "closed"):
        raise ClaudeError("The environment sent back something unexpected.", hint=out.strip())
    return words[-2], words[-1] == "open"


def _authorize(env_id: str, public_key: str) -> None:
    """Add `public_key` to the box's authorized_keys if missing, with the modes sshd wants."""
    key = shlex.quote(public_key)
    _on_box(
        env_id,
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys"
        " && chmod 600 ~/.ssh/authorized_keys"
        f" && (grep -qxF {key} ~/.ssh/authorized_keys || echo {key} >> ~/.ssh/authorized_keys)",
    )


def _entry(env_id: str, user: str, today: date, name: str = "") -> str:
    alias = own_host(env_id, name)
    return (
        f"Host {alias}\n"
        "  HostName localhost\n"
        f"  Port {SSHD_PORT}\n"
        f"  User {user}\n"
        f"  IdentityFile {KEY_PATH}\n"
        "  IdentitiesOnly yes\n"
        f"  HostKeyAlias {alias}\n"
        f"  ProxyJump {ssh.host(env_id)}\n"
        "  StrictHostKeyChecking accept-new\n"
        f"  # env {env_id}\n"
        f"  # last used {today.isoformat()}\n"
    )


def _blocks(text: str) -> dict[str, str]:
    """Each Host block in oo's config file, keyed by its host name."""
    blocks = re.split(r"^(?=Host )", text, flags=re.MULTILINE)
    return {block.split()[1]: block for block in blocks if block.startswith("Host ")}


def _last_used(block: str) -> date:
    """When oo last used a Host block. Blocks without a date count as long unused."""
    found = re.search(r"^  # last used (\d{4}-\d{2}-\d{2})$", block, flags=re.MULTILINE)
    return date.fromisoformat(found.group(1)) if found else date.min


def _env_id(alias: str, block: str) -> str:
    """The environment a Host block reaches. Blocks from before names went by their ID."""
    found = re.search(r"^  # env (\S+)$", block, flags=re.MULTILINE)
    return found.group(1) if found else alias.removesuffix(HOST_SUFFIX)


def _write_config(env_id: str, user: str, live_ids: set[str], name: str = "") -> None:
    """Record how to reach `env_id` in oo's config file, replacing any older host for it.

    Forgets environments that are gone, and hosts not used in the last STALE_DAYS days.
    """
    path = _ssh_dir() / "oo" / "config"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    today = date.today()
    cutoff = today - timedelta(days=STALE_DAYS)
    kept = _blocks(path.read_text()) if path.exists() else {}
    kept = {
        alias: block
        for alias, block in kept.items()
        if _env_id(alias, block) in live_ids - {env_id} and _last_used(block) >= cutoff
    }
    kept[own_host(env_id, name)] = _entry(env_id, user, today, name)
    path.write_text("\n".join(kept.values()))


def check_installed() -> None:
    """Fail early if the Claude desktop app clearly isn't there.

    Only macOS can say for sure, so elsewhere this lets everything through, and `open_link`
    finds out instead.
    """
    osascript = shutil.which("osascript") if sys.platform == "darwin" else None
    if not osascript:
        return
    script = f'POSIX path of (path to application id "{BUNDLE_ID}")'
    done = subprocess.run([osascript, "-e", script], capture_output=True, text=True)
    if done.returncode:
        raise ClaudeError("The Claude desktop app isn't installed.", hint=NOT_INSTALLED_HINT)


def open_link(url: str) -> None:
    """Have the OS hand `url` to the Claude desktop app."""
    if sys.platform == "win32":
        try:
            os.startfile(url)
        except OSError as e:
            raise ClaudeError(
                "Couldn't open the Claude desktop app.", hint=NOT_INSTALLED_HINT
            ) from e
        return
    opener = shutil.which("open" if sys.platform == "darwin" else "xdg-open")
    if not opener:
        raise ClaudeError(
            "Couldn't find a way to open links.", hint=f"Open [bold]{url}[/] yourself."
        )
    done = subprocess.run([opener, url], capture_output=True, text=True)
    if done.returncode:
        raise ClaudeError("Couldn't open the Claude desktop app.", hint=NOT_INSTALLED_HINT)


def ssh_configs_path() -> Path:
    """Where the desktop app keeps the SSH connections it offers in its picker."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "Claude" / "ssh_configs.json"


def _own_hosts() -> dict[str, str]:
    """The environment each of oo's own hosts reaches, from oo's config file."""
    path = _ssh_dir() / "oo" / "config"
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise ClaudeError(f"Couldn't read {path}: {e}") from e
    return {alias: _env_id(alias, block) for alias, block in _blocks(text).items()}


def _app_env_id(host: Any, own_hosts: dict[str, str]) -> str | None:
    """The environment an app connection goes to, or None if it isn't to one.

    oo's own hosts are named after environments, so oo's config says which one each reaches.
    One it no longer lists goes by what comes before the suffix: the environment's name, or its
    ID for hosts from before names.
    """
    if not isinstance(host, str):
        return None
    if host in own_hosts:
        return own_hosts[host]
    for suffix in APP_HOST_SUFFIXES:
        if host.endswith(suffix) and len(host) > len(suffix):
            return host.removesuffix(suffix)
    return None


def _read(path: Path) -> dict[str, Any] | None:
    """The app's SSH connections file, or None if the app hasn't made one yet.

    The file is the app's own, not a documented format, so if it doesn't look the way oo
    expects, oo leaves it alone rather than guess.
    """
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise ClaudeError(f"Couldn't read the Claude desktop app's SSH connections: {e}") from e

    def laid_out_as_expected() -> bool:
        if not isinstance(data, dict):
            return False
        configs = data.get("configs", [])
        trusted = data.get("trustedHosts", [])
        return (
            isinstance(configs, list)
            and all(isinstance(c, dict) for c in configs)
            and isinstance(trusted, list)
        )

    if not laid_out_as_expected():
        raise ClaudeError(
            f"{path} isn't laid out the way oo expects, so oo left it alone. "
            "The Claude desktop app may have changed how it keeps SSH connections."
        )
    return data


def _write(path: Path, data: dict[str, Any]) -> None:
    # Write beside it and swap it in, so the app never sees half a file.
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as f:
        temp = Path(f.name)
        try:
            json.dump(data, f, indent=2)
        except BaseException:
            f.close()
            temp.unlink(missing_ok=True)
            raise
    if path.exists():
        shutil.copymode(path, temp)
    temp.replace(path)


def forget(
    stale: Callable[[str], bool], path: Path | None = None, *, dry_run: bool = False
) -> list[str]:
    """Drop the app's connections to Ona environments where `stale(env)` says so.

    `env` is the environment's ID, or for one of oo's hosts that oo's config no longer lists,
    the name the host was given, so `stale` should know environments by both. Leaves every
    other connection, and every other part of the file, as it was. Returns the names of the
    connections it dropped, as the app shows them. With `dry_run`, only says which it would drop.
    """
    path = path or ssh_configs_path()
    data = _read(path)
    if data is None:
        return []
    own_hosts = _own_hosts()

    def gone(host: Any) -> bool:
        env_id = _app_env_id(host, own_hosts)
        return env_id is not None and stale(env_id)

    configs = data.get("configs") or []
    dropped = [c for c in configs if gone(c.get("sshHost"))]
    trusted = data.get("trustedHosts") or []
    if not dropped and not any(gone(h) for h in trusted):
        return []
    names = [c.get("name") or c["sshHost"] for c in dropped]
    if dry_run:
        return names
    data["configs"] = [c for c in configs if c not in dropped]
    data["trustedHosts"] = [h for h in trusted if not gone(h)]
    _write(path, data)
    return names


def name_connection(
    host: str, name: str, *, rename: bool = False, path: Path | None = None
) -> str | None:
    """Give the app's connection to `host` a name to show in its SSH picker.

    `host` is the one the link opens, so the name lands on the connection the app uses. Only
    names a connection the app already has: the app adds it the first time it connects, and oo
    doesn't make up entries in a file that isn't its own. A name you already gave it in the app
    stays, unless `rename`. Returns the new name, or None if it changed nothing.
    """
    path = path or ssh_configs_path()
    data = _read(path)
    if data is None:
        return None
    existing = next((c for c in data.get("configs", []) if c.get("sshHost") == host), None)
    if existing is None or existing.get("name") == name:
        return None
    if not rename and existing.get("name") not in (None, "", host):
        return None
    existing["name"] = name
    _write(path, data)
    return name
