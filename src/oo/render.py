"""Everything oo prints for people to read."""

from __future__ import annotations

from datetime import UTC, datetime

from rich.console import Console
from rich.table import Table
from rich.text import Text

from .envs import Env
from .projects import EnvClass, Project

ACCENT = "#7c83f7"
ERROR = "#f2506e"
FAINT = "grey42"
GREEN = "#1fbf8f"
AMBER = "#eb9a12"

PHASE_LOOKS = {
    "running": ("●", GREEN),
    "starting": ("◐", AMBER),
    "creating": ("◐", AMBER),
    "updating": ("◐", AMBER),
    "stopping": ("◑", AMBER),
    "stopped": ("○", FAINT),
    "deleting": ("✕", ERROR),
    "deleted": ("✕", FAINT),
}


def ago(when: datetime | None, now: datetime | None = None) -> str:
    if when is None:
        return "?"
    seconds = ((now or datetime.now(UTC)) - when).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    if seconds < 60 * 86400:
        return f"{int(seconds // 86400)}d ago"
    return when.strftime("%b %-d, %Y")


def created(when: datetime | None) -> str:
    """Date and time in your timezone, precise enough to tell same-day environments apart."""
    return when.astimezone().strftime("%b %-d %H:%M") if when else "?"


def name(env: Env) -> Text:
    return Text(env.label, style="bold")


def phase(env: Env) -> Text:
    dot, color = PHASE_LOOKS.get(env.phase, ("?", FAINT))
    return Text.assemble((f"{dot} ", color), (env.phase, "" if env.running else color))


def changes(env: Env) -> Text:
    return Text(f"{env.changed_count} uncommitted" if env.changed_count else "", style=AMBER)


def env_table(envs: list[Env], *, width: int | None = None, numbered: bool = False) -> Table:
    """One row per environment, fitted to `width` by cutting the name short if need be.

    When the shown names repeat, rows also show the end of their ID and when they were created, to
    tell them apart.
    """
    show_repo = len({e.repo for e in envs}) > 1
    twins = len({e.label.lower() for e in envs}) < len(envs)
    table = Table(box=None, pad_edge=False, padding=(0, 1), header_style=FAINT)
    if numbered:
        table.add_column("#", justify="right", style=FAINT, no_wrap=True)
    table.add_column("Name", no_wrap=True, overflow="ellipsis")
    if twins:
        table.add_column("ID", no_wrap=True)
    if show_repo:
        table.add_column("Repo", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    if twins:
        table.add_column("Created", no_wrap=True)
    table.add_column("Active", no_wrap=True, style=FAINT)
    table.add_column("Changes", no_wrap=True)

    rows: list[list[Text]] = []
    for i, env in enumerate(envs, 1):
        cells = [Text(str(i))] if numbered else []
        cells.append(name(env))
        if twins:
            cells.append(Text(env.short_id, style=ACCENT))
        if show_repo:
            cells.append(Text(env.repo))
        cells.append(phase(env))
        if twins:
            cells.append(Text(created(env.created)))
        cells.append(Text(ago(env.last_active)))
        cells.append(changes(env))
        rows.append(cells)
        table.add_row(*cells)

    if width is not None:
        # Every other column gets what it needs, plus a gap of 2 between columns.
        name_index = 1 if numbered else 0
        others = sum(
            max(len(str(column.header)), *(row[i].cell_len for row in rows))
            for i, column in enumerate(table.columns)
            if i != name_index
        )
        table.columns[name_index].max_width = max(10, width - others - 2 * (len(rows[0]) - 1))
    return table


def listing(console: Console, envs: list[Env]) -> None:
    console.print()
    console.print(env_table(envs, width=console.width))
    console.print()


def heading(console: Console, verb: str, env: Env, note: str = "") -> None:
    """e.g. `→ Connecting to login-fix · running`"""
    line = Text.assemble(("→ ", ACCENT), (f"{verb} ", ACCENT), name(env))
    line.append(f" · {note or env.phase}", style=FAINT)
    console.print(line)


def question(console: Console, message: str) -> None:
    console.print(Text.assemble(("? ", f"bold {ACCENT}"), (message, "bold")))


def warning(console: Console, message: str, detail: str | None = None) -> None:
    console.print(Text.assemble(("! ", f"bold {AMBER}"), message))
    if detail:
        console.print(Text(f"  {detail}", style=FAINT))


def duration(seconds: float) -> str:
    seconds = round(seconds)
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m {seconds % 60:02d}s"


def names(envs: list[Env]) -> str:
    """e.g. `a`, `a and b`, `a, b and c`"""
    named = [e.label for e in envs]
    return named[0] if len(named) == 1 else ", ".join(named[:-1]) + f" and {named[-1]}"


def working(message: str, elapsed: float | None = None) -> Text:
    """Spinner text, with how long it's been going once that's worth saying."""
    text = Text(message, style=ACCENT)
    if elapsed and elapsed >= 1:
        text.append(f" · {duration(elapsed)}", style=FAINT)
    return text


def success(console: Console, message: str, note: str = "") -> None:
    line = Text.assemble(("✓ ", f"bold {GREEN}"), message)
    if note:
        line.append(f" {note}", style=FAINT)
    console.print(line)


def error(console: Console, message: str, *, detail: str | None = None, hint: str | None = None):
    console.print(Text.assemble(("✗ ", f"bold {ERROR}"), (message, "bold")))
    if detail:
        console.print(Text(f"  {detail}", style=FAINT))
    if hint:
        console.print(Text.from_markup(f"  {hint}"))


def creating(console: Console, project: Project, env_class: EnvClass | None) -> None:
    """e.g. `→ Creating an environment from Web app · m7i.xlarge in Frankfurt`"""
    line = Text.assemble(
        ("→ Creating an environment from ", ACCENT), (project.name or project.id, "bold")
    )
    line.append(f" · {env_class}" if env_class else " · on Ona's default class", style=FAINT)
    console.print(line)
