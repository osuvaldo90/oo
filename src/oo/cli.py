"""`oo`: hop into your Ona environments by branch name."""

from __future__ import annotations

import difflib
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, NoReturn

import typer
from rich.console import Console
from rich.prompt import Confirm, IntPrompt

from . import __version__, claude, github, ona, render, ssh
from .envs import PREFIX_VAR, Ambiguous, Env, NoMatch, branch_prefix, pick, sort_envs
from .github import BranchStatus, GitHubError, NoGh
from .ona import OnaError
from .projects import CLASS_VAR, PROJECT_VAR, ChoiceError, pick_class, pick_project

out = Console(highlight=False)
err = Console(stderr=True, highlight=False)

app = typer.Typer(
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

ACTIVE = ("running", "starting", "creating", "updating")
NO_ENVS_HINT = "Create one with [bold]ona environment create <repo-url>[/]."


def _fail(message: str, *, detail: str | None = None, hint: str | None = None) -> NoReturn:
    render.error(err, message, detail=detail, hint=hint)
    raise typer.Exit(1)


def _interactive() -> bool:
    """Whether there's someone at a terminal to answer questions."""
    return sys.stdin.isatty() and err.is_terminal


def _cancelled() -> NoReturn:
    err.print("\n[dim]Cancelled.[/]")
    raise typer.Exit(130)


def _load() -> list[Env]:
    try:
        with err.status(render.working("Asking Ona…"), spinner_style=render.ACCENT):
            return sort_envs(ona.list_envs())
    except OnaError as e:
        _fail(str(e), detail=e.detail, hint=e.hint)
    except KeyboardInterrupt:
        _cancelled()


def _github(envs: list[Env]) -> dict[str, BranchStatus] | None:
    """What GitHub says about each branch, or None if we can't ask."""
    try:
        with err.status(render.working("Asking GitHub…"), spinner_style=render.ACCENT):
            return github.lookup(envs)
    except NoGh:
        return None
    except GitHubError as e:
        render.warning(err, "Couldn't get pull requests from GitHub.", detail=str(e))
        return None
    except KeyboardInterrupt:
        _cancelled()


def _ask_which(envs: list[Env]) -> Env:
    """Show `envs` numbered and have the person at the terminal pick one."""
    err.print()
    err.print(render.env_table(envs, branch_prefix(), width=err.width, numbered=True))
    err.print()
    try:
        choice = IntPrompt.ask(
            f"[{render.ACCENT}]Which one?[/] [dim](1-{len(envs)})[/]",
            console=err,
            choices=[str(i) for i in range(1, len(envs) + 1)],
            show_choices=False,
        )
    except (KeyboardInterrupt, EOFError):
        _cancelled()
    return envs[choice - 1]


def _find(envs: list[Env], query: str) -> Env:
    """The environment `query` names, asking which one if it names several."""
    prefix = branch_prefix()
    try:
        return pick(envs, query, prefix)
    except NoMatch:
        if not envs:
            _fail("You don't have any environments yet.", hint=NO_ENVS_HINT)
        close = difflib.get_close_matches(query, [e.short_branch(prefix) for e in envs], n=3)
        hint = "Run [bold]oo[/] to see your environments."
        if close:
            hint = "Did you mean " + " or ".join(f"[bold]{name}[/]" for name in close) + "?"
        _fail(f'No environment matches "{query}".', hint=hint)
    except Ambiguous as e:
        message = f'"{query}" matches {len(e.candidates)} environments.'
        if _interactive():
            render.question(err, message)
            return _ask_which(e.candidates)
        render.error(err, message)
        err.print()
        err.print(render.env_table(e.candidates, prefix, width=err.width))
        err.print()
        twins = len({c.short_branch(prefix) for c in e.candidates}) < len(e.candidates)
        err.print("  Pass one of the IDs above instead." if twins else "  Be more specific.")
        raise typer.Exit(1) from None


def _find_all(envs: list[Env], queries: list[str]) -> list[Env]:
    found = [_find(envs, q) for q in queries]
    return list({env.id: env for env in found}.values())


def _choose(envs: list[Env], query: str | None, verb: str) -> Env:
    """The environment `query` names; without one, the obvious choice or else ask."""
    if query:
        return _find(envs, query)
    if not envs:
        _fail("You don't have any environments yet.", hint=NO_ENVS_HINT)
    running = [e for e in envs if e.running]
    if len(envs) == 1:
        return envs[0]
    if len(running) == 1:
        return running[0]
    if not _interactive():
        _fail(f"Which environment should I {verb}?", hint="Pass its branch name.")
    return _ask_which(envs)


def _quietly(
    doing: str, *args: str, failed: str, cancelled: str = "", harmless: str = ""
) -> tuple[float, str]:
    """Run an ona command behind a spinner, showing its error nicely if it fails.

    Returns how many seconds it took and what ona printed. An error mentioning `harmless`
    doesn't count as failing.
    """
    started = time.monotonic()
    try:
        with err.status(render.working(doing), spinner_style=render.ACCENT) as status:
            return ona.quietly(
                *args, failed=failed, on_tick=lambda s: status.update(render.working(doing, s))
            )
    except OnaError as e:
        if harmless and harmless in (e.detail or ""):
            return time.monotonic() - started, e.detail or ""
        _fail(str(e), detail=e.detail, hint=e.hint)
    except KeyboardInterrupt:
        if cancelled:
            err.print(f"\n[dim]{cancelled}[/]")
        _cancelled()


def _start_if_stopped(env: Env) -> str:
    """Start `env` if it's stopped, so ona has nothing to say before connecting."""
    if env.phase != "stopped":
        return ""
    label = render.names([env])
    took, _ = _quietly(
        f"Starting {label}…", "environment", "start", env.id, failed=f"Ona couldn't start {label}."
    )
    return f"started in {render.duration(took)}"


def _complete_branch(incomplete: str) -> list[tuple[str, str]]:
    try:
        envs = sort_envs(ona.list_envs(timeout=5))
    except (OnaError, OSError):
        return []
    prefix = branch_prefix()
    names = [(e.short_branch(prefix), e.phase) for e in envs]
    return [(name, phase) for name, phase in names if name.startswith(incomplete)]


BRANCH_HELP = (
    f"Branch name, leaving off your ${PREFIX_VAR} if you set one. "
    "Any unique part of it, an environment name or an ID works too."
)
Branch = Annotated[
    str, typer.Argument(help=BRANCH_HELP, autocompletion=_complete_branch, show_default=False)
]
MaybeBranch = Annotated[
    str | None,
    typer.Argument(help=BRANCH_HELP, autocompletion=_complete_branch, show_default=False),
]
Branches = Annotated[
    list[str],
    typer.Argument(help=BRANCH_HELP, autocompletion=_complete_branch, show_default=False),
]
MaybeBranches = Annotated[
    list[str] | None,
    typer.Argument(help=BRANCH_HELP, autocompletion=_complete_branch, show_default=False),
]
Wait = Annotated[bool, typer.Option("--wait", "-w", help="Wait for ona to finish.")]
MAX_NAME = 80  # the longest name Ona takes for an environment


def _version(value: bool) -> None:
    if value:
        out.print(f"oo {__version__}")
        raise typer.Exit()


EPILOG = (
    "[bold]Examples[/]\n\n"
    "  [cyan]oo[/]                           list your environments\n"
    "  [cyan]oo c login-fix[/]               SSH into the one on adam/login-fix\n"
    "  [cyan]oo c[/]                         …the only running one, or pick from a list\n"
    "  [cyan]oo c login -- git status[/]     run a command instead of a shell\n"
    "  [cyan]oo open login -e cursor[/]      open it in Cursor\n"
    "  [cyan]oo claude login[/]              open it in the Claude desktop app\n"
    "  [cyan]oo cp notes.md login:[/]        copy a file into its repo\n"
    "  [cyan]oo cp login:out.csv .[/]        …or out of it\n"
    "  [cyan]oo new[/]                       create an environment and connect to it\n"
    "  [cyan]oo new -b pr-9[/]               …check out branch pr-9 first\n"
    "  [cyan]oo stop --all[/]                stop everything that's running\n"
    "  [cyan]ona env logs $(oo id login)[/]  hand an ID to any other ona command\n\n"
    "[bold]Branch prefix[/]\n\n"
    "If you always start your branch names the same way, like [dim]adam/[/]login-fix, tell oo "
    f"by setting [bold]${PREFIX_VAR}[/], ideally in your shell profile:\n\n"
    f"  [cyan]export {PREFIX_VAR}=adam/[/]\n\n"
    "Then you can leave the prefix off whenever you name a branch, as in [cyan]oo c login-fix[/], "
    "and lists dim it so the part you'd type stands out. With or without a prefix, any unique "
    "part of a branch name works too, like [cyan]oo c login[/]."
)


@app.callback(invoke_without_command=True, epilog=EPILOG)
def root(
    ctx: typer.Context,
    version: Annotated[
        bool | None,
        typer.Option("--version", "-V", callback=_version, is_eager=True, help="Show version."),
    ] = None,
) -> None:
    """Hop into your [bold]Ona[/] environments by branch name.

    With no command, lists your environments.
    """
    if ctx.invoked_subcommand is None:
        ls()


@app.command("ls")
def ls() -> None:
    """List your environments, running ones first, with their pull requests."""
    envs = _load()
    if not envs:
        render.error(err, "You don't have any environments yet.", hint=NO_ENVS_HINT)
        return
    render.listing(out, envs, branch_prefix(), _github(envs))


@app.command("c")
def connect(
    branch: MaybeBranch = None,
    command: Annotated[
        list[str] | None,
        typer.Argument(
            help="A command to run instead of a shell, after [bold]--[/].", show_default=False
        ),
    ] = None,
) -> None:
    """SSH into an environment, starting it first if it's stopped."""
    env = _choose(_load(), branch, "connect to")
    render.heading(err, "Connecting to", env, branch_prefix(), _start_if_stopped(env))
    ona.replace_with("environment", "ssh", env.id, *(["--", *command] if command else []))


@app.command("open")
def open_(
    branch: MaybeBranch = None,
    editor: Annotated[
        str | None,
        typer.Option(
            "--editor",
            "-e",
            envvar="OO_EDITOR",
            help="Editor alias like [bold]cursor[/] or [bold]vscode[/] "
            "(see [bold]ona environment list-editors[/]).",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Open an environment in your editor, starting it first if it's stopped."""
    env = _choose(_load(), branch, "open")
    notes = [_start_if_stopped(env), f"in {editor}" if editor else ""]
    render.heading(err, "Opening", env, branch_prefix(), ", ".join(n for n in notes if n))
    ona.replace_with(
        "environment", "open", env.id, "--start", *(["--editor", editor] if editor else [])
    )


def _set_up_ssh(env: Env) -> None:
    """Have ona teach ssh about its environments, if it hasn't yet, and say so."""
    try:
        if ssh.configured(env.id):
            return
        _quietly("Setting up SSH…", "environment", "ssh-config", failed="Ona couldn't set up SSH.")
        if not ssh.configured(env.id):
            _fail(
                "Ona set up SSH, but ssh still can't reach the environment.",
                hint="Check what [bold]ona environment ssh-config[/] added to ~/.ssh/config.",
            )
    except ssh.SshError as e:
        _fail(str(e), hint=e.hint)
    render.success(err, "Set up SSH for Ona environments", "with ona environment ssh-config")


def _forget_in_claude(stale: Callable[[str], bool], *, quiet: bool = False) -> None:
    """Take connections to deleted environments out of the Claude desktop app's picker.

    `quiet` says nothing when there was nothing to take out.
    """
    try:
        dropped = claude.forget(stale)
    except claude.ClaudeError as e:
        render.warning(err, "Couldn't tidy up the Claude desktop app.", detail=str(e))
        return
    if dropped:
        render.success(err, f"Removed {', '.join(dropped)} from the Claude desktop app")
    elif not quiet:
        render.success(err, "The Claude desktop app has nothing left over to remove.")


def _clean_claude(yes: bool) -> None:
    """Take every deleted environment out of the Claude desktop app, once you've seen which."""
    alive = {e.id for e in _load()}

    def stale(env_id: str) -> bool:
        return env_id not in alive

    try:
        doomed = claude.forget(stale, dry_run=True)
    except claude.ClaudeError as e:
        _fail("Couldn't tidy up the Claude desktop app.", detail=str(e))
    # Ona only lists the environments in your current context, so one signed in elsewhere would
    # make every connection look deleted. Show which before taking any out.
    if doomed and not yes:
        render.question(
            err,
            f"Ona doesn't list {len(doomed)} of the Claude desktop app's environments: "
            f"{', '.join(doomed)}.",
        )
        if not _interactive():
            _fail(
                "Not removing them without asking.",
                hint="Run it in a terminal to confirm, or pass [bold]--yes[/].",
            )
        try:
            sure = Confirm.ask(
                f"[{render.ACCENT}]Remove them from the app?[/]", console=err, default=False
            )
        except (KeyboardInterrupt, EOFError):
            _cancelled()
        if not sure:
            _cancelled()
    _forget_in_claude(stale)


@app.command("claude")
def claude_(
    branch: MaybeBranch = None,
    clean: Annotated[
        bool,
        typer.Option(
            "--clean",
            help="Instead, remove environments you've deleted from the app's SSH connections.",
        ),
    ] = False,
    label: Annotated[
        str | None,
        typer.Option(
            "--label",
            help="What to call the connection in the app's SSH picker. Without it, an unnamed "
            "one is named after the branch, and one you've named keeps its name.",
            show_default=False,
        ),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="With --clean, remove them without asking.")
    ] = False,
) -> None:
    """Open a Claude Code session on an environment in the Claude desktop app, over SSH."""
    if clean:
        if branch or label:
            _fail("--clean tidies up every deleted environment, so it takes no branch or label.")
        _clean_claude(yes)
        return
    if yes:
        _fail("--yes only goes with --clean.")
    try:
        claude.check_installed()
    except claude.ClaudeError as e:
        _fail(str(e), hint=e.hint)
    envs = _load()
    env = _choose(envs, branch, "open in Claude")
    _set_up_ssh(env)
    note = _start_if_stopped(env)
    host = _claude_host(env, envs)
    render.heading(err, "Opening in Claude", env, branch_prefix(), note)
    label = (label or "").strip()
    try:
        named = label or env.short_branch(branch_prefix())
        if called := claude.name_connection(host, named, rename=bool(label)):
            render.success(err, f"Shows as {called} in the app's SSH connections")
    except claude.ClaudeError as e:
        render.warning(err, "Couldn't name the connection in the Claude desktop app.", str(e))
    try:
        claude.open_link(claude.link(env.id, env.checkout, host=host))
    except claude.ClaudeError as e:
        _fail(str(e), hint=e.hint)


def _claude_host(env: Env, envs: list[Env]) -> str:
    """The SSH host for the Claude app: the environment's own sshd if it works, else Ona's."""
    try:
        with err.status(render.working("Setting up SSH for Claude…"), spinner_style=render.ACCENT):
            return claude.set_up_host(env.id, {e.id for e in envs})
    except claude.ClaudeError as e:
        render.warning(err, f"{e} Opening with Ona's own SSH host instead.", detail=e.hint)
        return ssh.host(env.id)


@app.command("cp")
def copy(
    paths: Annotated[
        list[str],
        typer.Argument(
            help="Where to copy from, then where to. Write paths in an environment as "
            "[bold]branch:path[/], or [bold]:path[/] for the only running one. Relative ones "
            "start in the repo.",
            show_default=False,
        ),
    ],
) -> None:
    """Copy files and folders between here and an environment, like scp."""
    example = "Like [bold]oo cp notes.md login:[/] or [bold]oo cp login:out.csv .[/]"
    if len(paths) < 2:
        _fail("Copy what, and where to?", hint=example)
    spots = [ssh.Spot.parse(p) for p in paths]
    *sources, dest = spots
    remotes = [s for s in spots if s.remote]
    if not remotes:
        _fail("None of those are in an environment.", hint=example)
    if any(s.remote == dest.remote for s in sources):
        _fail("oo cp copies between here and an environment, one way at a time.", hint=example)

    verb = "copy to" if dest.remote else "copy from"
    envs = _load()
    chosen = {branch: _choose(envs, branch or None, verb) for branch in {s.branch for s in remotes}}
    if len({e.id for e in chosen.values()}) > 1:
        _fail("Those are in more than one environment.", hint="Copy from one at a time.")
    env = next(iter(chosen.values()))
    _set_up_ssh(env)
    note = _start_if_stopped(env)
    render.heading(err, "Copying to" if dest.remote else "Copying from", env, branch_prefix(), note)
    where = ssh.host(env.id)
    args = [f"{where}:{s.resolved(env.checkout)}" if s.remote else s.path for s in spots]
    # -O: Ona's SFTP server writes files as root, so they'd be stuck read-only in your shell.
    ssh.replace_with_scp("-O", "-r", "--", *args)


@app.command()
def new(
    project: Annotated[
        str | None,
        typer.Option(
            "--project",
            "-p",
            envvar=PROJECT_VAR,
            help="Project to create from, by ID or name. "
            "Defaults to the one your newest environment came from.",
            show_default=False,
        ),
    ] = None,
    env_class: Annotated[
        str | None,
        typer.Option(
            "--class",
            "-c",
            envvar=CLASS_VAR,
            help="Where to run it: a region like [bold]frankfurt[/], optionally with a machine "
            "like [bold]m7i[/], or a class ID. Defaults to what your newest environment runs on.",
            show_default=False,
        ),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            "-n",
            help="A name for it, like [bold]login-fix[/], to find it by before it has a branch.",
            show_default=False,
        ),
    ] = None,
    branch: Annotated[
        str | None,
        typer.Option(
            "--branch",
            "-b",
            help="Branch to check out once it's ready, before it connects. "
            "If the checkout fails, oo warns and carries on.",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Create an environment and connect to it."""
    name = (name or "").strip()
    if len(name) > MAX_NAME:
        _fail(f"Ona takes names of up to {MAX_NAME} characters.", hint=f"That one has {len(name)}.")
    if branch is not None and not safe_branch(branch):
        _fail(
            f"{branch!r} can't be a branch name.",
            hint="Give a branch without spaces that doesn't start with a hyphen.",
        )
    try:
        with (
            err.status(render.working("Asking Ona…"), spinner_style=render.ACCENT),
            ThreadPoolExecutor() as pool,
        ):
            envs_job = pool.submit(ona.list_envs)
            projects_job = pool.submit(ona.list_projects)
            classes_job = pool.submit(ona.list_classes)
            envs, projects, classes = envs_job.result(), projects_job.result(), classes_job.result()
    except OnaError as e:
        _fail(str(e), detail=e.detail, hint=e.hint)
    except KeyboardInterrupt:
        _cancelled()
    try:
        chosen = pick_project(projects, envs, project)
        where = pick_class(classes, chosen, envs, env_class)
    except ChoiceError as e:
        _fail(str(e), hint=e.hint)

    render.creating(err, chosen, where)
    before = {e.id for e in envs}
    took, output = _quietly(
        "Waiting for Ona to set it up…",
        "environment",
        "create",
        chosen.id,
        *(["--class-id", where.id] if where else []),
        *(["--name", name] if name else []),
        failed="Ona couldn't create the environment.",
        cancelled="Ona may still finish creating it. Run oo to check.",
    )
    fresh = [e for e in _load() if e.id not in before]
    # Another oo new running alongside makes more than one new environment, so go by the ID
    # ona printed, and only by there being just the one new environment if it didn't print one.
    created = next((e for e in fresh if e.id in output), fresh[0] if len(fresh) == 1 else None)
    if created is None:
        _fail(
            "Ona created it, but I can't tell which environment is yours to connect.",
            hint="Run [bold]oo[/] to look.",
        )
    note = f"created in {render.duration(took)}"
    if branch:
        _check_out(created, branch, note)
        note = ""
    render.heading(err, "Connecting to", created, branch_prefix(), note)
    ona.replace_with("environment", "ssh", created.id)


# Fetch the branch by name, so it works on shallow and single-branch clones too, then switch to
# it. The branch comes in as $1 rather than in the script, so it needs no quoting.
CHECK_OUT = 'git fetch origin "$1:refs/remotes/origin/$1" && git switch "$1"'
# Seconds to give the fetch and switch: more than ona's default of 120, for big repos.
CHECK_OUT_TIMEOUT = "600"


def safe_branch(branch: str) -> bool:
    """Whether `branch` can go to git as a branch, not be taken for an option."""
    return bool(branch) and not branch.startswith("-") and not any(c.isspace() for c in branch)


def _check_out(env: Env, branch: str, note: str = "") -> None:
    """Have ona check `branch` out in `env`, and carry on. Only warns if it can't."""
    render.heading(
        err, "Checking out", env, branch_prefix(), ", ".join(n for n in (note, branch) if n)
    )
    try:
        with err.status(render.working(f"Checking out {branch}…"), spinner_style=render.ACCENT):
            ona.quietly(
                "environment",
                "exec",
                env.id,
                "--timeout",
                CHECK_OUT_TIMEOUT,
                *(["--working-dir", env.checkout] if env.checkout else []),
                "--",
                "sh",
                "-c",
                CHECK_OUT,
                "sh",
                branch,
                failed=f"Ona couldn't check out {branch}.",
            )
    except OnaError as e:
        render.warning(err, str(e), detail=e.detail)
    except KeyboardInterrupt:
        _cancelled()


@app.command()
def start(branches: Branches, wait: Wait = False) -> None:
    """Start environments without connecting, to warm them up."""
    prefix = branch_prefix()
    targets = []
    for env in _find_all(_load(), branches):
        if env.phase in ACTIVE:
            render.heading(err, "Skipping", env, prefix, f"already {env.phase}")
        else:
            targets.append(env)
    if not targets:
        return
    label = render.names(targets)
    took, _ = _quietly(
        f"Starting {label}…",
        "environment",
        "start",
        *(e.id for e in targets),
        *([] if wait else ["--dont-wait"]),
        failed=f"Ona couldn't start {label}.",
    )
    if wait:
        render.success(err, f"Started {label}", f"in {render.duration(took)}")
    else:
        render.success(err, f"Starting {label} in the background.", "oo c waits if it has to.")


@app.command()
def stop(
    branches: MaybeBranches = None,
    everything: Annotated[
        bool, typer.Option("--all", "-a", help="Stop every running one.")
    ] = False,
    wait: Wait = False,
) -> None:
    """Stop environments. With no branch, stops the only running one."""
    prefix = branch_prefix()
    envs = _load()
    running = [e for e in envs if e.phase in ACTIVE]
    if branches:
        targets = []
        for env in _find_all(envs, branches):
            if env.phase in ACTIVE:
                targets.append(env)
            else:
                render.heading(err, "Skipping", env, prefix, f"already {env.phase}")
        if not targets:
            return
    elif everything or len(running) <= 1:
        targets = running
    else:
        _fail(
            f"{len(running)} environments are running.",
            hint="Name the ones to stop, or stop them all with [bold]oo stop --all[/].",
        )
    if not targets:
        render.success(err, "Nothing is running.")
        return
    label = render.names(targets)
    took, _ = _quietly(
        f"Stopping {label}…",
        "environment",
        "stop",
        *(e.id for e in targets),
        *([] if wait else ["--dont-wait"]),
        failed=f"Ona couldn't stop {label}.",
    )
    if wait:
        render.success(err, f"Stopped {label}", f"in {render.duration(took)}")
    else:
        render.success(err, f"Stopping {label} in the background.")


def _until_gone(envs: list[Env], doing: str, elapsed: float) -> float:
    """Wait for `envs` to leave the list, carrying on the timer from `elapsed` seconds in.

    Returns how many seconds it took altogether.
    """
    ids = {e.id for e in envs}
    started = time.monotonic() - elapsed
    try:
        with err.status(render.working(doing, elapsed), spinner_style=render.ACCENT) as status:
            while ids & {e.id for e in ona.list_envs() if e.phase != "deleted"}:
                time.sleep(2)
                status.update(render.working(doing, time.monotonic() - started))
    except OnaError as e:
        _fail(str(e), detail=e.detail, hint=e.hint)
    except KeyboardInterrupt:
        err.print("\n[dim]Ona will still finish deleting. Run oo to check.[/]")
        _cancelled()
    return time.monotonic() - started


@app.command("rm")
def remove(branches: Branches, wait: Wait = False) -> None:
    """Delete environments. Ona finishes the job in the background."""
    targets = _find_all(_load(), branches)
    label = render.names(targets)
    doing = f"Deleting {label}…"
    took, _ = _quietly(
        doing,
        "environment",
        "delete",
        *(e.id for e in targets),
        *([] if wait else ["--dont-wait"]),
        failed=f"Ona couldn't delete {label}.",
        # Deleting stops a running environment first, and ona's waiting takes that as failure.
        harmless="unexpectedly stopped" if wait else "",
    )
    if wait:
        took = _until_gone(targets, doing, took)
        render.success(err, f"Deleted {label}", f"in {render.duration(took)}")
    else:
        render.success(err, f"Deleting {label} in the background.")
    deleted = {e.id for e in targets}
    _forget_in_claude(lambda env_id: env_id in deleted, quiet=True)


@app.command("id")
def id_(branch: Branch) -> None:
    """Print an environment's ID, to hand to other ona commands."""
    print(_find(_load(), branch).id)


@app.command("ui")
def ui() -> None:
    """Keep the list on screen and act on it with one key each.

    Needs oo's [bold]ui[/] extra, which brings in Textual.
    """
    from . import ui as screen

    raise typer.Exit(screen.run())


# Longhand spellings, for muscle memory from ona and friends.
app.command("ssh", hidden=True)(connect)
app.command("list", hidden=True)(ls)
app.command("delete", hidden=True)(remove)


def main() -> None:
    app()
