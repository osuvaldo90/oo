"""Where an SSH session opens from the screen: here, in a new tab or window, or in cmux.

`oo ui` keeps running, so it can't become ssh the way `oo c` does. Either it steps aside for
a while (`here`), or it asks the terminal it's running in to open the session next to it.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from collections.abc import Mapping

CONNECT_VAR = "OO_CONNECT"
PREFERENCES = ("auto", "here", "cmux", "tmux", "tab")

# What the screen says about each place a session can open.
WHERE = {
    "here": "here",
    "cmux": "as a cmux workspace",
    "tmux": "in a new tmux window",
    "iterm": "in a new iTerm2 tab",
    "wezterm": "in a new WezTerm tab",
    "kitty": "in a new kitty tab",
    "terminal": "in a new Terminal window",
}

# Seconds to give a command that should return straight away, and to watch cmux for an early exit.
QUICK = 15.0
CMUX_GRACE = 3.0

# tmux on the environment: a session per environment, starting in its repo, with a reaper that
# detaches idle clients so the environment can stop. The same setup `oo c` does in cmux.
IDLE_VARS = ("OO_IDLE_TIMEOUT", "ONA_IDLE_TIMEOUT")
IDLE_TIMEOUT = 3600
REAPER_INTERVAL = 300
REAPER_PATH = "/tmp/oc-reaper.sh"
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
ATTEMPTS = 5
RETRY_DELAY = 3.0


class HandoffError(Exception):
    def __init__(self, message: str, detail: str | None = None, hint: str | None = None):
        super().__init__(message)
        self.detail = detail
        self.hint = hint


def preference(environ: Mapping[str, str] = os.environ) -> str:
    """What $OO_CONNECT asks for, or `auto`."""
    return environ.get(CONNECT_VAR, "auto").strip().lower() or "auto"


def detect(environ: Mapping[str, str] = os.environ, want: str = "auto") -> str:
    """Where to open a session: a key of WHERE.

    `auto` takes the first of these that fits the terminal we're in: cmux, tmux, then a terminal
    whose tabs can be opened from a command (iTerm2, WezTerm, kitty, Terminal.app), else here.
    cmux comes before anything else because it sets TERM_PROGRAM to ghostty, which can't open
    a tab with a command. A preference that doesn't fit falls back to here.
    """
    in_cmux = bool(environ.get("CMUX_WORKSPACE_ID"))
    in_tmux = bool(environ.get("TMUX"))
    program = environ.get("TERM_PROGRAM", "")
    tab = ""
    if program == "iTerm.app":
        tab = "iterm"
    elif environ.get("WEZTERM_PANE"):
        tab = "wezterm"
    elif environ.get("KITTY_WINDOW_ID"):
        tab = "kitty"
    elif program == "Apple_Terminal":
        tab = "terminal"

    if want == "here":
        return "here"
    if want == "cmux":
        return "cmux" if in_cmux else "here"
    if want == "tmux":
        return "tmux" if in_tmux else "here"
    if want == "tab":
        return tab or "here"
    if in_cmux:
        return "cmux"
    if in_tmux:
        return "tmux"
    return tab or "here"


def cmux_binary(environ: Mapping[str, str] = os.environ) -> str | None:
    bundled = environ.get("CMUX_BUNDLED_CLI_PATH", "")
    if bundled and os.access(bundled, os.X_OK):
        return bundled
    return shutil.which("cmux")


def _binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise HandoffError(f"Couldn't find {name}.", hint=f"Make sure {name} is on your PATH.")
    return path


def applescript_string(text: str) -> str:
    """`text` as an AppleScript string literal."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _run(argv: list[str], failed: str, *, input: str | None = None) -> None:
    try:
        done = subprocess.run(
            argv,
            input=input,
            capture_output=True,
            text=True,
            stdin=None if input is not None else subprocess.DEVNULL,
            timeout=QUICK,
        )
    except subprocess.TimeoutExpired as e:
        raise HandoffError(failed, detail=f"{argv[0]} took too long to answer.") from e
    if done.returncode:
        said = (done.stderr or done.stdout).strip()
        raise HandoffError(failed, detail=said or None)


# Runs its arguments, and if they fail, keeps the window open long enough to read why.
HOLD = (
    '"$@"; s=$?; [ "$s" -eq 0 ] || { '
    'printf "\n%s exited with %s. Press Enter to close.\n" "$1" "$s"; read -r _; }'
)


def holding(argv: list[str]) -> list[str]:
    """`argv`, wrapped so a window opened for it stays up if it exits with an error at once."""
    return ["/bin/sh", "-c", HOLD, "sh", *argv]


def open_elsewhere(
    kind: str, argv: list[str], title: str, *, host: str = "", checkout: str = ""
) -> None:
    """Open `argv` as a session titled `title`, `kind` being a key of WHERE other than `here`.

    For cmux, `argv` isn't used: cmux mirrors a tmux session on `host` instead, started in
    `checkout`. Raises HandoffError if the terminal wouldn't do it. The terminal's answer says
    only that it opened the window; what happens inside stays there, so a command that fails
    at once holds the window open with its exit code.
    """
    held = holding(argv)
    if kind == "tmux":
        _run(
            [_binary("tmux"), "new-window", "-n", title, "--", *held],
            "tmux couldn't open a window.",
        )
    elif kind == "iterm":
        script = (
            'tell application "iTerm2" to tell current window to '
            f"create tab with default profile command {applescript_string(shlex.join(held))}"
        )
        _run([_binary("osascript"), "-e", script], "iTerm2 couldn't open a tab.")
    elif kind == "wezterm":
        _run(
            [_binary("wezterm"), "cli", "spawn", "--", *held],
            "WezTerm couldn't open a tab.",
        )
    elif kind == "kitty":
        _run(
            [_binary("kitten"), "@", "launch", "--type=tab", "--tab-title", title, *held],
            "kitty couldn't open a tab.",
        )
    elif kind == "terminal":
        # Terminal.app keeps the window after the command ends, so no holding needed.
        script = f'tell application "Terminal" to do script {applescript_string(shlex.join(argv))}'
        _run([_binary("osascript"), "-e", script], "Terminal couldn't open a window.")
    elif kind == "cmux":
        open_in_cmux(host, title, checkout)
    else:
        raise HandoffError(f"Don't know how to open a session {kind}.")


def session_name(label: str) -> str:
    """A tmux session name for an environment. cmux calls its workspace this too."""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", label).strip("-") or "oo"


def idle_timeout(environ: Mapping[str, str] = os.environ) -> int:
    for var in IDLE_VARS:
        value = environ.get(var, "").strip()
        if value.isdigit():
            return int(value)
    return IDLE_TIMEOUT


def setup_script(session: str, checkout: str = "", timeout: int = IDLE_TIMEOUT) -> str:
    """What to run on the environment, with REAPER on its stdin, before cmux mirrors it."""
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


def prepare_tmux(host: str, session: str, checkout: str = "") -> None:
    """Get the environment at `host` ready to mirror, trying again while its SSH comes up."""
    command = [
        _binary("ssh"),
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={int(QUICK)}",
        host,
        setup_script(session, checkout, idle_timeout()),
    ]
    for attempt in range(1, ATTEMPTS + 1):
        try:
            done = subprocess.run(
                command, input=REAPER, capture_output=True, text=True, timeout=QUICK * 2
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
            raise HandoffError(
                "Couldn't set up tmux on the environment for cmux.",
                detail=detail or None,
                hint="oo c NAME -- true checks that plain SSH works.",
            )
        time.sleep(RETRY_DELAY)


def open_in_cmux(host: str, title: str, checkout: str = "") -> None:
    """Have cmux mirror a tmux session on `host` as a workspace named after `title`."""
    cli = cmux_binary()
    if not cli:
        raise HandoffError("Couldn't find the cmux CLI.", hint="Is cmux's CLI installed?")
    prepare_tmux(host, session_name(title), checkout)
    # cmux may keep running while the workspace is open, so don't wait for it; just make sure
    # it didn't give up straight away.
    process = subprocess.Popen(
        [cli, "ssh-tmux", host],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        output, _ = process.communicate(timeout=CMUX_GRACE)
    except subprocess.TimeoutExpired:
        return
    if process.returncode:
        raise HandoffError("cmux couldn't open the workspace.", detail=(output or "").strip())
