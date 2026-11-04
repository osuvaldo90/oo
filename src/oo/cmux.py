"""cmux, the terminal: it can mirror an environment's tmux sessions as its own workspaces."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from typing import NoReturn

from . import ssh

# Seconds without typing before the reaper detaches a client, and how often it looks.
IDLE_VARS = ("OO_IDLE_TIMEOUT", "ONA_IDLE_TIMEOUT")
IDLE_TIMEOUT = 3600
REAPER_INTERVAL = 300

# How often to try reaching an environment whose SSH isn't up yet, and how long between.
ATTEMPTS = 5
RETRY_DELAY = 3.0
# Seconds to give ssh to connect and to run the setup, before counting it as one failed attempt.
CONNECT_TIMEOUT = 15
PREPARE_TIMEOUT = 30.0

# Detaches tmux clients with no input for $1 seconds, checking every $2 seconds, so idle
# environments can stop. Program output doesn't count as input. It stops when the tmux server
# exits. Input time is the newest access time of the pane ttys in the client's session:
# client_activity doesn't change for control-mode clients like cmux's, whose keys arrive as
# commands. The `oc` shell function installs the same reaper, at the same path and behind the
# same @oc_reaper option, so one runs per tmux server whichever of them connected first.
REAPER = """\
timeout=$1 interval=$2
while sleep "$interval"; do
  now=$(date +%s)
  clients=$(tmux list-clients -F '#{client_tty} #{client_session}') || break
  echo "$clients" | while read -r c s; do
    [ -n "$c" ] || continue
    a=$(tmux list-panes -s -t "=$s" -F '#{pane_tty}' |
      xargs -r stat -c %X 2>/dev/null | sort -n | tail -1)
    [ -n "$a" ] && [ $((now - a)) -gt "$timeout" ] && tmux detach-client -t "$c"
  done
done
"""
REAPER_PATH = "/tmp/oc-reaper.sh"


class CmuxError(Exception):
    def __init__(self, message: str, detail: str | None = None, hint: str | None = None):
        super().__init__(message)
        self.detail = detail
        self.hint = hint


def binary() -> str | None:
    """The cmux CLI, if oo is running in a cmux terminal; otherwise None."""
    if not os.environ.get("CMUX_WORKSPACE_ID"):
        return None
    bundled = os.environ.get("CMUX_BUNDLED_CLI_PATH", "")
    if bundled and os.access(bundled, os.X_OK):
        return bundled
    return shutil.which("cmux")


def session_name(label: str) -> str:
    """A tmux session name for an environment. cmux calls its workspace this too.

    tmux won't have dots or colons in session names, and plain words keep the shell happy.
    """
    return re.sub(r"[^A-Za-z0-9_-]+", "-", label).strip("-") or "oo"


def idle_timeout() -> int:
    for var in IDLE_VARS:
        value = os.environ.get(var, "").strip()
        if value.isdigit():
            return int(value)
    return IDLE_TIMEOUT


def setup_script(session: str, checkout: str = "", timeout: int = IDLE_TIMEOUT) -> str:
    """What to run on the environment, with REAPER on its stdin, before cmux mirrors it.

    It makes a tmux session named `session` if there isn't one, starting in the repo, and
    starts the idle reaper once per tmux server.
    """
    name = shlex.quote(session)
    # Quoted by hand: shlex leaves `=` bare, and zsh expands a bare `=word` to a command's path.
    target = "'=" + session.replace("'", "'\\''") + "'"
    new = f"tmux new-session -d -s {name}" + (f" -c {shlex.quote(checkout)}" if checkout else "")
    reaper = f"run-shell -b 'sh {REAPER_PATH} {timeout} {REAPER_INTERVAL}'"
    return "\n".join(
        [
            f'f={REAPER_PATH}; cat > "$f.tmp" && mv "$f.tmp" "$f"',
            f"tmux has-session -t {target} 2>/dev/null || {new}",
            f"tmux if -F '#{{@oc_reaper}}' '' \"set -g @oc_reaper 1 ; {reaper}\"",
        ]
    )


def prepare(host: str, session: str, checkout: str = "") -> None:
    """Get the environment at `host` ready to mirror, trying again while its SSH comes up."""
    command = [
        ssh.binary("ssh"),
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={CONNECT_TIMEOUT}",
        host,
        setup_script(session, checkout, idle_timeout()),
    ]
    for attempt in range(1, ATTEMPTS + 1):
        try:
            done = subprocess.run(
                command, input=REAPER, capture_output=True, text=True, timeout=PREPARE_TIMEOUT
            )
        except subprocess.TimeoutExpired:
            # As good as ssh giving up itself: try again, then say so.
            done = subprocess.CompletedProcess(command, 255, "", "ssh took too long to answer.")
        if done.returncode == 0:
            return
        # 255 is ssh's own failure, like the environment not taking connections yet.
        if done.returncode != 255 or attempt == ATTEMPTS:
            said = (done.stderr or done.stdout).splitlines()
            # ona, as ssh's proxy, warns about updates, which isn't why it failed.
            detail = "\n".join(line for line in said if not line.startswith("[WARN")).strip()
            raise CmuxError(
                "Couldn't set up tmux on the environment for cmux.",
                detail=detail or None,
                hint="[bold]oo c NAME -- true[/] checks that plain SSH works.",
            )
        time.sleep(RETRY_DELAY)


def replace_with_mirror(cmux: str, host: str) -> NoReturn:
    """Become `cmux ssh-tmux`, which shows the environment's tmux sessions as workspaces."""
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(cmux, [cmux, "ssh-tmux", host])
