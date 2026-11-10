"""The screen itself: the list, what's happening to it, and the keys that act on it."""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding, BindingType
from textual.widgets import DataTable, Input, RichLog, Static

from .. import ona, render, ssh
from ..envs import Env, sort_envs
from ..projects import EnvClass, Project
from . import handoff, model, ops, screens
from .model import ACTIVE, TRANSITIONAL, Event, Op, Ops, Overlays
from .ops import UiError

# How often to ask Ona for the list: while something is changing, while idle, and while the
# terminal is in the background.
POLL_BUSY = 3.0
POLL_IDLE = 15.0
POLL_BLURRED = 60.0

# Column keys in the table, so cells can be updated in place.
MARK, NAME, ID, REPO, STATUS, CREATED, ACTIVE_COL, CHANGES = (
    "mark",
    "name",
    "id",
    "repo",
    "status",
    "created",
    "active",
    "changes",
)

EDITOR_VAR = "OO_EDITOR"


def _default_sigint() -> None:
    signal.signal(signal.SIGINT, signal.SIG_DFL)


class OoApp(App[None]):
    """`oo ui`."""

    TITLE = "oo"
    CSS = """
    Screen {
        layout: vertical;
    }
    #header, #banner, #event, #keys {
        height: 1;
        padding: 0 1;
    }
    #header {
        color: $text-muted;
    }
    #banner {
        background: #f2506e 20%;
    }
    #filter {
        height: 3;
    }
    #table {
        height: 1fr;
    }
    #details {
        height: auto;
        max-height: 5;
        padding: 0 1;
        border-top: solid $panel;
        color: $text-muted;
    }
    #log {
        height: 10;
        border-top: solid $panel;
    }
    #event {
        border-top: solid $panel;
    }
    #keys {
        color: $text-muted;
    }
    .hidden {
        display: none;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("c", "connect", "ssh"),
        Binding("C", "connect_here", "ssh here", show=False),
        Binding("o", "editor", "code"),
        Binding("O", "pick_editor", "pick editor", show=False),
        Binding("a", "claude", "claude"),
        Binding("n", "new", "new"),
        Binding("s", "start", "start"),
        Binding("x", "stop", "stop"),
        Binding("D", "delete", "delete"),
        Binding("space", "mark", "mark"),
        Binding("asterisk", "mark_all", "mark all", show=False),
        Binding("escape", "clear", "clear", show=False),
        Binding("i", "details", "info"),
        Binding("L", "log", "log"),
        Binding("y", "yank", "copy id", show=False),
        Binding("slash", "filter", "filter"),
        Binding("r", "refresh", "refresh"),
        Binding("exclamation_mark", "login", "ona login", show=False),
        Binding("question_mark", "help", "help"),
        Binding("q", "quit", "quit"),
        Binding("j", "down", "down", show=False),
        Binding("k", "up", "up", show=False),
        Binding("g", "top", "top", show=False),
        Binding("G", "bottom", "bottom", show=False),
    ]

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        super().__init__()
        self.environ = os.environ if environ is None else environ
        self.prefix = model.prefix()
        self.connect_pref = handoff.preference(self.environ)
        self.editor_default = self.environ.get(EDITOR_VAR) or None

        self.envs: list[Env] = []  # as Ona reported them
        self.shown: list[Env] = []  # with overlays, sorted
        self.rows: list[Env] = []  # after the filter
        self.overlays = Overlays()
        self.ops = Ops()
        self.marks: set[str] = set()
        self.failures: dict[str, UiError] = {}
        self.events: list[Event] = []
        self.ghosts: list[tuple[str, str, Op]] = []  # rows for environments being created
        self.then: dict[int, tuple[str, object]] = {}  # what to do when a start finishes

        self.filter_text = ""
        self.cursor_id: str | None = None
        self.last_poll = 0.0
        self.polling = False
        self.poll_error: UiError | None = None
        self.blurred = False
        self.suspended = False
        self.details_shown = True
        self.log_shown = False

    # ----- layout -----

    def compose(self) -> ComposeResult:
        yield Static("", id="header")
        yield Static("", id="banner", classes="hidden")
        yield Input(
            placeholder="filter by name, or a phase like stopped", id="filter", classes="hidden"
        )
        yield DataTable(id="table", cursor_type="row", zebra_stripes=False)
        yield Static("", id="details")
        yield RichLog(id="log", markup=False, wrap=True, classes="hidden")
        yield Static("", id="event")
        yield Static("", id="keys")

    def on_mount(self) -> None:
        if self.connect_pref not in handoff.PREFERENCES:
            self.notify(
                f"${handoff.CONNECT_VAR} is {self.connect_pref!r}; using auto. "
                f"It takes {', '.join(handoff.PREFERENCES)}.",
                severity="warning",
            )
            self.connect_pref = "auto"
        self.table.focus()
        self.rebuild()
        self.set_interval(1.0, self.tick)
        self.poll()

    @property
    def table(self) -> DataTable:
        return self.query_one("#table", DataTable)

    def on_unmount(self) -> None:
        # Textual joins worker threads before App.run returns, and a worker blocked on ona
        # can't be cancelled, so stop the ona clients. Ona itself carries on, as ConfirmQuit says.
        for process in list(ona.LIVE):
            with contextlib.suppress(ProcessLookupError):
                process.kill()

    def on_app_blur(self, event: events.AppBlur) -> None:
        self.blurred = True

    def on_app_focus(self, event: events.AppFocus) -> None:
        self.blurred = False

    # ----- polling -----

    def tick(self) -> None:
        """Once a second: refresh timers, and ask Ona again when it's time."""
        if self.suspended:
            return
        now = time.monotonic()
        busy = (
            any(e.phase in TRANSITIONAL for e in self.shown)
            or len(self.ops)
            or len(self.overlays)
            or self.ghosts
        )
        interval = POLL_BUSY if busy else (POLL_BLURRED if self.blurred else POLL_IDLE)
        if not self.polling and now - self.last_poll >= interval:
            self.poll()
        self.update_header()
        self.update_stages()

    def poll(self) -> None:
        if self.polling:
            return
        self.polling = True
        self._poll_worker()

    @work(thread=True, exit_on_error=False, group="poll")
    def _poll_worker(self) -> None:
        try:
            envs = ops.list_envs()
        except Exception as e:
            wrapped = e if isinstance(e, UiError) else UiError(str(e))
            self.call_from_thread(self._poll_failed, wrapped)
            return
        self.call_from_thread(self._polled, envs)

    def _polled(self, envs: list[Env]) -> None:
        self.polling = False
        self.last_poll = time.monotonic()
        self.envs = envs
        if self.poll_error is not None:
            self.poll_error = None
            self.update_banner()
        self.recompute()

    def _poll_failed(self, e: UiError) -> None:
        self.polling = False
        self.last_poll = time.monotonic()
        self.poll_error = e
        self.update_banner()
        self.update_header()

    def recompute(self) -> None:
        """Work out what to show from Ona's list, the overlays and the filter, then redraw."""
        shown, expired = self.overlays.apply(self.envs)
        for env_id in expired:
            env = next((e for e in self.envs if e.id == env_id), None)
            if env is not None:
                self.log_event(
                    "!",
                    f"Ona still reports {self.label(env)} as {env.phase}",
                    f"after {render.duration(model.OVERLAY_TIMEOUT)}. Showing what Ona says.",
                )
        self.shown = sort_envs(shown)
        live = {e.id for e in self.envs}
        self.marks &= live
        self.failures = {k: v for k, v in self.failures.items() if k in live}
        self.rows = model.search(self.shown, self.filter_text, self.prefix)
        self.rebuild()
        self.update_header()

    # ----- drawing -----

    def label(self, env: Env) -> str:
        return model.label(env, self.prefix)

    def overlay(self, env: Env, phase: str) -> None:
        """Show `phase` for `env` until Ona's own phase moves, going by what Ona last said."""
        reported = next((e for e in self.envs if e.id == env.id), env)
        self.overlays.set(reported, phase)

    def rebuild(self) -> None:
        """Redraw the table from scratch, keeping the cursor on the same environment."""
        table = self.table
        envs = self.rows
        twins = model.twins(envs, self.prefix)
        show_repo = len({e.repo for e in envs}) > 1
        table.clear(columns=True)
        table.add_column(" ", key=MARK, width=1)
        table.add_column("Branch", key=NAME)
        if twins:
            table.add_column("ID", key=ID)
        if show_repo:
            table.add_column("Repo", key=REPO)
        table.add_column("Status", key=STATUS)
        if twins:
            table.add_column("Created", key=CREATED)
        table.add_column("Active", key=ACTIVE_COL)
        table.add_column("Changes", key=CHANGES)
        for env in envs:
            cells: list[Text] = [self.mark_cell(env.id), render.branch(env, self.prefix)]
            if twins:
                cells.append(Text(env.short_id, style=render.ACCENT))
            if show_repo:
                cells.append(Text(env.repo))
            cells.append(self.status_cell(env))
            if twins:
                cells.append(Text(render.created(env.created)))
            cells.append(Text(render.ago(env.last_active), style=render.FAINT))
            cells.append(self.changes_cell(env))
            table.add_row(*cells, key=env.id)
        for ghost_id, name, op in self.ghosts:
            cells = [Text(" "), Text(name, style="bold")]
            if twins:
                cells.append(Text(""))
            if show_repo:
                cells.append(Text(""))
            cells.append(self.ghost_status(op))
            if twins:
                cells.append(Text(""))
            cells.extend([Text(""), Text("")])
            table.add_row(*cells, key=ghost_id)
        keys = [e.id for e in envs] + [g[0] for g in self.ghosts]
        if keys:
            row = keys.index(self.cursor_id) if self.cursor_id in keys else 0
            table.move_cursor(row=row)
            self.cursor_id = keys[row]
        else:
            self.cursor_id = None
        self.update_details()
        self.update_keys()
        self.update_event_for_filter()

    def mark_cell(self, env_id: str) -> Text:
        return Text("✓", style=render.ACCENT) if env_id in self.marks else Text(" ")

    def status_cell(self, env: Env) -> Text:
        text = render.phase(env)
        if op := self.ops.busy(env.id):
            text.append(f" · {render.duration(op.elapsed(time.monotonic()))}", style=render.FAINT)
            if op.stage:
                text.append(f" · {op.stage}", style=render.FAINT)
            if then := self.then.get(op.id):
                text.append(f" → {then[0]}", style=render.FAINT)
        return text

    def ghost_status(self, op: Op) -> Text:
        dot, color = render.PHASE_LOOKS["creating"]
        text = Text.assemble((f"{dot} ", color), ("creating", color))
        text.append(f" · {render.duration(op.elapsed(time.monotonic()))}", style=render.FAINT)
        return text

    def changes_cell(self, env: Env) -> Text:
        text = Text(
            f"{env.changed_count} uncommitted" if env.changed_count else "", style=render.AMBER
        )
        if env.id in self.failures:
            text.append("  !" if text.plain else "!", style=f"bold {render.ERROR}")
        return text

    def update_stages(self) -> None:
        """Keep the timers in the Status column moving."""
        if not self.ops and not self.ghosts:
            return
        table = self.table
        for env in self.rows:
            if self.ops.busy(env.id):
                table.update_cell(env.id, STATUS, self.status_cell(env))
        for ghost_id, _, op in self.ghosts:
            table.update_cell(ghost_id, STATUS, self.ghost_status(op))

    def update_header(self) -> None:
        running = sum(1 for e in self.shown if e.running)
        text = Text.assemble(("oo", f"bold {render.ACCENT}"), "  ·  ")
        if self.shown:
            text.append(f"{len(self.shown)} environments, {running} running")
        elif self.poll_error is None and self.last_poll:
            text.append("no environments yet")
        else:
            text.append("asking Ona…")
        if self.filter_text:
            text.append(f"  ·  / {self.filter_text} · {len(self.rows)} of {len(self.shown)}")
        if self.marks:
            text.append(f"  ·  {len(self.marks)} marked", style=render.ACCENT)
        if self.last_poll:
            ago = render.duration(time.monotonic() - self.last_poll)
            text.append(f"  ·  refreshed {ago} ago" + ("  ⟳" if self.polling else ""))
        where = handoff.WHERE[handoff.detect(self.environ, self.connect_pref)]
        text.append(f"  ·  ⏎ opens ssh {where}", style=render.FAINT)
        self.query_one("#header", Static).update(text)

    def update_banner(self) -> None:
        banner = self.query_one("#banner", Static)
        if self.poll_error is None:
            banner.set_class(True, "hidden")
            return
        e = self.poll_error
        text = Text.assemble(("✗ ", f"bold {render.ERROR}"), (str(e), "bold"))
        if e.detail:
            text.append(f"  {e.detail}", style=render.FAINT)
        if e.about_auth:
            text.append("  ·  press ! to run ona login", style=render.ACCENT)
        elif e.hint:
            text.append(f"  ·  {Text.from_markup(e.hint).plain}", style=render.FAINT)
        banner.update(text)
        banner.set_class(False, "hidden")

    def update_details(self) -> None:
        pane = self.query_one("#details", Static)
        env = self.cursor_env()
        if env is None:
            ghost = next((g for g in self.ghosts if g[0] == self.cursor_id), None)
            pane.update("Creating it; Ona takes a minute or two." if ghost else "")
            return
        first = Text.assemble((self.label(env), "bold"))
        if env.repo:
            first.append(f"  ·  {env.repo}")
        if env.branch and env.branch != self.label(env):
            first.append(f"  ·  {env.branch}")
        first.append(f"  ·  created {render.created(env.created)}  ·  id …{env.short_id}")
        if env.checkout:
            first.append(f"  ·  {env.checkout}", style=render.FAINT)
        second = Text()
        if failure := self.failures.get(env.id):
            second.append("✗ ", style=f"bold {render.ERROR}")
            second.append(str(failure), style="bold")
            if failure.detail:
                second.append(f"  {failure.detail}", style=render.FAINT)
            if failure.hint:
                second.append(f"  {Text.from_markup(failure.hint).plain}", style=render.FAINT)
        elif env.changed_count:
            shown = ", ".join(env.changes[:5])
            more = env.changed_count - len(env.changes[:5])
            second.append(f"{env.changed_count} uncommitted: {shown}", style=render.AMBER)
            if more > 0:
                second.append(f" and {more} more", style=render.AMBER)
        else:
            second.append("no uncommitted changes", style=render.FAINT)
        pane.update(Text.assemble(first, "\n", second))

    def update_keys(self) -> None:
        env = self.cursor_env()
        if self.marks:
            hints = [
                (f"{len(self.marks)} marked", render.ACCENT),
                "s start",
                "x stop",
                "D delete",
                "␣ unmark",
                "esc clear",
            ]
        else:
            toggle = "x stop" if env and env.phase in ACTIVE else "s start"
            hints = [
                "⏎ ssh",
                "o code",
                "a claude",
                "n new",
                toggle,
                "D delete",
                "␣ mark",
                "i info",
                "L log",
                "/ filter",
                "? help",
                "q quit",
            ]
        text = Text()
        for hint in hints:
            if isinstance(hint, tuple):
                text.append(hint[0], style=hint[1])
            else:
                key, _, what = hint.partition(" ")
                text.append(key, style=f"bold {render.ACCENT}")
                text.append(f" {what}")
            text.append("  ")
        self.query_one("#keys", Static).update(text)

    def update_event_for_filter(self) -> None:
        if self.filter_text and not self.rows and self.shown:
            line = Text.assemble(
                ("? ", f"bold {render.ACCENT}"), f'No environment matches "{self.filter_text}".'
            )
            if close := model.suggestion(self.shown, self.filter_text, self.prefix):
                line.append(f"  Did you mean {close}? Enter picks it.", style=render.FAINT)
            self.query_one("#event", Static).update(line)

    def log_event(
        self,
        mark: str,
        message: str,
        detail: str = "",
        hint: str = "",
        env_ids: tuple[str, ...] = (),
    ) -> None:
        """Say what just happened, on the event line and in the log."""
        style = {
            "✓": f"bold {render.GREEN}",
            "!": f"bold {render.AMBER}",
            "✗": f"bold {render.ERROR}",
            "→": render.ACCENT,
            "↩": render.ACCENT,
        }.get(mark, "")
        event = Event(time.time(), mark, message, detail, hint, env_ids)
        self.events.append(event)
        line = Text.assemble((f"{mark} ", style), message)
        if detail:
            line.append(f"  {detail}", style=render.FAINT)
        if hint:
            line.append(f"  {Text.from_markup(hint).plain}", style=render.FAINT)
        self.query_one("#event", Static).update(line)
        stamp = datetime.fromtimestamp(event.when).strftime("%H:%M:%S")
        self.query_one("#log", RichLog).write(Text.assemble((f"{stamp} ", render.FAINT), line))

    # ----- cursor and targets -----

    def cursor_env(self) -> Env | None:
        return next((e for e in self.rows if e.id == self.cursor_id), None)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value is not None:
            self.cursor_id = event.row_key.value
        self.update_details()
        self.update_keys()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_connect()

    def targets(self, verb: str) -> list[Env]:
        """The marked environments, or the one under the cursor, for `verb`."""
        if self.marks:
            # Marked rows count even when the filter hides them; the header says how many.
            return [e for e in self.shown if e.id in self.marks]
        env = self.cursor_env()
        if env is None:
            if self.cursor_id and any(g[0] == self.cursor_id for g in self.ghosts):
                self.notify("That one is still being created.", severity="warning")
            elif not self.shown:
                self.notify("No environments yet.", severity="warning")
            return []
        return [env]

    def one_target(self, verb: str) -> Env | None:
        """The environment under the cursor, for something that takes one at a time."""
        if self.marks:
            self.notify(
                f"{verb} one environment at a time: clear the marks with Esc.", severity="warning"
            )
            return None
        found = self.targets(verb)
        if not found:
            return None
        env = found[0]
        if op := self.ops.busy(env.id):
            self.notify(f"{self.label(env)} is busy: {op.verb.lower()}.", severity="warning")
            return None
        return env

    # ----- actions: navigation -----

    def action_down(self) -> None:
        self.table.action_cursor_down()

    def action_up(self) -> None:
        self.table.action_cursor_up()

    def action_top(self) -> None:
        self.table.action_scroll_top()

    def action_bottom(self) -> None:
        self.table.action_scroll_bottom()

    def action_mark(self) -> None:
        env = self.cursor_env()
        if env is None:
            return
        if env.id in self.marks:
            self.marks.discard(env.id)
        else:
            self.marks.add(env.id)
        self.table.update_cell(env.id, MARK, self.mark_cell(env.id))
        self.table.action_cursor_down()
        self.update_header()
        self.update_keys()

    def action_mark_all(self) -> None:
        self.marks = {e.id for e in self.rows}
        for env in self.rows:
            self.table.update_cell(env.id, MARK, self.mark_cell(env.id))
        self.update_header()
        self.update_keys()

    def action_clear(self) -> None:
        if self.filter_text or not self.query_one("#filter", Input).has_class("hidden"):
            self.set_filter("")
            self.query_one("#filter", Input).value = ""
            self.query_one("#filter", Input).set_class(True, "hidden")
            self.table.focus()
        elif self.marks:
            self.marks.clear()
            self.rebuild()
        elif self.log_shown:
            self.action_log()

    def action_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.set_class(False, "hidden")
        box.focus()

    def set_filter(self, text: str) -> None:
        self.filter_text = text
        self.rows = model.search(self.shown, text, self.prefix)
        self.rebuild()
        self.update_header()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self.set_filter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "filter":
            return
        if not self.rows and (close := model.suggestion(self.shown, event.value, self.prefix)):
            event.input.value = close
            return
        self.table.focus()

    def action_details(self) -> None:
        self.details_shown = not self.details_shown
        self.query_one("#details", Static).set_class(not self.details_shown, "hidden")

    def action_log(self) -> None:
        self.log_shown = not self.log_shown
        self.query_one("#log", RichLog).set_class(not self.log_shown, "hidden")

    def action_refresh(self) -> None:
        self.poll()
        self.update_header()

    def action_help(self) -> None:
        where = handoff.WHERE[handoff.detect(self.environ, self.connect_pref)]
        self.push_screen(screens.Help(where, self.connect_pref))

    def action_yank(self) -> None:
        env = self.cursor_env()
        if env is None:
            return
        self.copy_to_clipboard(env.id)
        if sys.platform == "darwin" and shutil.which("pbcopy"):
            subprocess.run(["pbcopy"], input=env.id, text=True, check=False)
        self.notify(f"Copied the ID of {self.label(env)}.")

    async def action_quit(self) -> None:
        live = self.ops.live()
        if not live:
            self.exit()
            return
        doing = [f"{op.verb} {self.names(op.env_ids)}".strip() for op in live]

        def decided(quit_anyway: bool | None) -> None:
            if quit_anyway:
                self.exit()

        self.push_screen(screens.ConfirmQuit(doing), decided)

    def names(self, env_ids: tuple[str, ...]) -> str:
        found = [e for e in self.envs if e.id in env_ids]
        if not found:
            return next((g[1] for g in self.ghosts if g[0] in env_ids), "")
        return model.names(found, self.prefix)

    # ----- actions: start, stop, delete -----

    def action_start(self) -> None:
        targets = self.targets("start")
        skipped = [e for e in targets if e.phase in ACTIVE or self.ops.busy(e.id)]
        targets = [e for e in targets if e not in skipped]
        if skipped and not targets:
            what = skipped[0].phase if len(skipped) == 1 else "running"
            self.notify(f"{model.names(skipped, self.prefix)} already {what}.")
        if not targets:
            return
        for env in targets:
            self.overlay(env, "starting")
        op = self.ops.start("Starting", tuple(e.id for e in targets))
        self.marks.clear()
        self.recompute()
        self._start_many(op, [e.id for e in targets])

    @work(thread=True, exit_on_error=False)
    def _start_many(self, op: Op, ids: list[str]) -> None:
        try:
            ops.start(ids, wait=False)
        except Exception as e:
            self.call_from_thread(self.failed, op, e)
            return
        self.call_from_thread(
            self.finished, op, "✓", f"Starting {self.names(op.env_ids)}", "in the background"
        )

    def action_stop(self) -> None:
        targets = self.targets("stop")
        skipped = [e for e in targets if e.phase not in ACTIVE or self.ops.busy(e.id)]
        targets = [e for e in targets if e not in skipped]
        if skipped and not targets:
            what = skipped[0].phase if len(skipped) == 1 else "stopped"
            self.notify(f"{model.names(skipped, self.prefix)} already {what}.")
        if not targets:
            return
        for env in targets:
            self.overlay(env, "stopping")
        op = self.ops.start("Stopping", tuple(e.id for e in targets))
        changed = sum(e.changed_count for e in targets)
        note = (
            f"{changed} uncommitted {'file stays' if changed == 1 else 'files stay'} on disk"
            if changed
            else ""
        )
        self.marks.clear()
        self.recompute()
        self._stop_many(op, [e.id for e in targets], note)

    @work(thread=True, exit_on_error=False)
    def _stop_many(self, op: Op, ids: list[str], note: str) -> None:
        try:
            ops.stop(ids)
        except Exception as e:
            self.call_from_thread(self.failed, op, e)
            return
        self.call_from_thread(self.finished, op, "✓", f"Stopping {self.names(op.env_ids)}", note)

    def action_delete(self) -> None:
        targets = self.targets("delete")
        if not targets:
            return
        if busy := [e for e in targets if self.ops.busy(e.id)]:
            self.notify(
                f"{model.names(busy, self.prefix)} is busy; wait for that to finish.",
                severity="warning",
            )
            return
        labels = {e.id: self.label(e) for e in targets}
        forgets = ops.would_forget([e.id for e in targets])

        def decided(sure: bool | None) -> None:
            if not sure:
                return
            for env in targets:
                self.overlay(env, "deleting")
            op = self.ops.start("Deleting", tuple(e.id for e in targets))
            self.marks.clear()
            self.recompute()
            self._delete_many(op, [e.id for e in targets])

        self.push_screen(screens.ConfirmDelete(targets, labels, forgets), decided)

    @work(thread=True, exit_on_error=False)
    def _delete_many(self, op: Op, ids: list[str]) -> None:
        try:
            dropped, warning = ops.delete(ids)
        except Exception as e:
            self.call_from_thread(self.failed, op, e)
            return
        note = "in the background"
        if dropped:
            note += f" · removed {', '.join(dropped)} from the Claude desktop app"
        self.call_from_thread(self.finished, op, "✓", f"Deleting {self.names(op.env_ids)}", note)
        if warning:
            self.call_from_thread(self.log_event, "!", warning)

    # ----- actions: connect -----

    def action_connect(self) -> None:
        self.connect_request(here=False)

    def action_connect_here(self) -> None:
        self.connect_request(here=True)

    def connect_request(self, *, here: bool) -> None:
        env = self.one_target("Connect to")
        if env is None:
            return
        if env.phase == "running":
            self.connect(env, here)
        elif env.phase == "stopped":
            self.start_then(env, ("connect", here))
        else:
            self.notify(
                f"{self.label(env)} is {env.phase}; try again when it's running.",
                severity="warning",
            )

    def start_then(self, env: Env, then: tuple[str, object]) -> None:
        """Start `env`, wait for it, then do `then`: connect, editor or claude."""
        self.overlay(env, "starting")
        op = self.ops.start("Starting", (env.id,))
        self.then[op.id] = then
        self.recompute()
        self._start_one(op, env)

    @work(thread=True, exit_on_error=False)
    def _start_one(self, op: Op, env: Env) -> None:
        try:
            took = ops.start([env.id], wait=True)
        except Exception as e:
            self.call_from_thread(self.failed, op, e)
            return
        self.call_from_thread(self._started, op, env, took)

    def _started(self, op: Op, env: Env, took: float) -> None:
        then = self.then.pop(op.id, None)
        self.finished(op, "✓", f"Started {self.label(env)}", f"in {render.duration(took)}")
        self.overlays.clear(env.id)
        running = replace(env, phase="running")
        if then is None:
            return
        what, payload = then
        if what == "connect":
            here = bool(payload)
            kind = "here" if here else handoff.detect(self.environ, self.connect_pref)
            doing = "to connect" if kind == "here" else f"to connect {handoff.WHERE[kind]}"
            self.push_screen(
                screens.Ready(self.label(env), doing),
                lambda go: self.connect(running, here) if go else None,
            )
        elif what == "editor":
            self.open_editor(running, payload if isinstance(payload, str) else None)
        elif what == "claude":
            self.claude_flow(running)

    def connect(self, env: Env, here: bool) -> None:
        kind = "here" if here else handoff.detect(self.environ, self.connect_pref)
        if kind == "here":
            self.connect_here(env)
            return
        op = self.ops.start("Connecting", (env.id,))
        op.stage = handoff.WHERE[kind]
        self.update_stages()
        self._connect_elsewhere(op, env, kind)

    @work(thread=True, exit_on_error=False)
    def _connect_elsewhere(self, op: Op, env: Env, kind: str) -> None:
        notes = []
        try:
            argv = ops.ssh_argv(env.id)
            host = ""
            if kind == "cmux":
                if ops.ensure_ssh(env.id):
                    notes.append("Set up SSH for Ona environments with ona environment ssh-config")
                host = ssh.host(env.id)
            handoff.open_elsewhere(kind, argv, self.label(env), host=host, checkout=env.checkout)
        except Exception as e:
            self.call_from_thread(self.failed, op, e)
            return
        for note in notes:
            self.call_from_thread(self.log_event, "✓", note)
        self.call_from_thread(
            self.finished, op, "✓", f"Opened {self.label(env)} {handoff.WHERE[kind]}", ""
        )

    def connect_here(self, env: Env) -> None:
        """Step aside and run ssh in this terminal, then come back."""
        try:
            argv = ops.ssh_argv(env.id)
        except UiError as e:
            self.log_event("✗", str(e), e.detail or "", e.hint or "")
            return
        heading = Text.assemble(("→ Connecting to ", render.ACCENT), (self.label(env), "bold"))
        heading.append(f" · {env.phase}", style=render.FAINT)
        started = time.monotonic()
        code = self.run_in_terminal(argv, heading)
        took = render.duration(time.monotonic() - started)
        if code is None:
            return
        if code:
            self.log_event(
                "!", f"Back from {self.label(env)}", f"ssh exited with {code} after {took}"
            )
        else:
            self.log_event("↩", f"Back from {self.label(env)}", f"after {took}")

    def run_in_terminal(self, argv: list[str], heading: Text) -> int | None:
        """Hand the terminal to `argv` until it exits. Returns its exit code, or None if it can't.

        Polling pauses meanwhile. Ctrl-C goes to the child, not to us: our SIGINT is ignored
        while it runs, and the child gets the default back.
        """
        self.suspended = True
        code: int | None = None
        try:
            with self.suspend():
                # Anything that escapes this block would leave Textual's writer thread dead
                # (textual#6692), so everything is caught and reported after we're back.
                try:
                    code = self._run_child(argv, heading)
                except BaseException as e:
                    print(f"\n✗ {e!r}", file=sys.__stderr__)
                    code = -1
        except SuspendNotSupported:
            self.notify(
                "This terminal can't hand over to ssh. Set $OO_CONNECT to tmux or tab.",
                severity="error",
            )
            return None
        finally:
            self.suspended = False
            self.last_poll = 0.0
            self.poll()
        return code

    def _run_child(self, argv: list[str], heading: Text) -> int:
        out = sys.__stdout__
        if out is not None:
            out.write("\n")
            out.write(heading.plain + "\n")
            out.flush()
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            done = subprocess.run(argv, preexec_fn=_default_sigint, check=False)
            if done.returncode and out is not None:
                program = Path(argv[0]).name
                out.write(
                    f"\n{program} exited with {done.returncode} · press Enter to go back to oo\n"
                )
                out.flush()
                with contextlib.suppress(EOFError, KeyboardInterrupt):
                    input()
        finally:
            signal.signal(signal.SIGINT, previous)
        return done.returncode

    def action_login(self) -> None:
        try:
            argv = ops.login_argv()
        except UiError as e:
            self.log_event("✗", str(e), e.detail or "", e.hint or "")
            return
        code = self.run_in_terminal(argv, Text("→ ona login", style=render.ACCENT))
        if code == 0:
            self.log_event("✓", "Logged in to Ona")
        elif code is not None:
            self.log_event("!", "ona login didn't finish", f"exited with {code}")

    # ----- actions: editor -----

    def action_editor(self) -> None:
        env = self.one_target("Open")
        if env is not None:
            self.open_editor(env, self.editor_default)

    def action_pick_editor(self) -> None:
        env = self.one_target("Open")
        if env is None:
            return
        op = self.ops.start("Listing editors", (env.id,))
        self._list_editors(op, env)

    @work(thread=True, exit_on_error=False)
    def _list_editors(self, op: Op, env: Env) -> None:
        try:
            found = ops.editors()
        except Exception as e:
            self.call_from_thread(self.failed, op, e)
            return
        self.call_from_thread(self._pick_editor, op, env, found)

    def _pick_editor(self, op: Op, env: Env, found: list[ops.Editor]) -> None:
        self.ops.finish(op)
        if not found:
            self.notify("Ona lists no editors.", severity="warning")
            return
        self.push_screen(
            screens.PickEditor(found, self.editor_default),
            lambda alias: self.open_editor(env, alias) if alias else None,
        )

    def open_editor(self, env: Env, editor: str | None) -> None:
        if self.ops.busy(env.id):
            self.notify(f"{self.label(env)} is busy.", severity="warning")
            return
        if env.phase == "stopped":
            self.overlay(env, "starting")
        op = self.ops.start("Opening", (env.id,))
        op.stage = f"in {editor}" if editor else "in your editor"
        self.recompute()
        self._open_editor(op, env, editor)

    @work(thread=True, exit_on_error=False)
    def _open_editor(self, op: Op, env: Env, editor: str | None) -> None:
        try:
            took = ops.open_editor(env.id, editor)
        except Exception as e:
            self.call_from_thread(self.failed, op, e)
            return
        self.call_from_thread(
            self.finished,
            op,
            "✓",
            f"Opened {self.label(env)} {op.stage}",
            f"in {render.duration(took)}",
        )

    # ----- actions: Claude desktop app -----

    def action_claude(self) -> None:
        env = self.one_target("Open in Claude")
        if env is None:
            return
        try:
            ops.check_claude()
        except UiError as e:
            self.log_event("✗", str(e), e.detail or "", e.hint or "")
            return
        if ops.claude_needs_consent():
            self.push_screen(
                screens.Consent(), lambda agreed: self.claude_flow(env) if agreed else None
            )
        else:
            self.claude_flow(env)

    def claude_flow(self, env: Env) -> None:
        if self.ops.busy(env.id):
            self.notify(f"{self.label(env)} is busy.", severity="warning")
            return
        if env.phase == "stopped":
            self.overlay(env, "starting")
        op = self.ops.start("Opening in Claude", (env.id,))
        self.recompute()
        self._open_in_claude(op, env, list(self.envs))

    @work(thread=True, exit_on_error=False)
    def _open_in_claude(self, op: Op, env: Env, envs: list[Env]) -> None:
        try:
            if env.phase == "stopped":
                self.call_from_thread(self.set_stage, op, "starting")
                ops.start([env.id], wait=True)
                self.call_from_thread(self.set_stage, op, "")
            notes = ops.open_in_claude(env, envs, self.label(env))
        except Exception as e:
            self.call_from_thread(self.failed, op, e)
            return
        for note in notes:
            self.call_from_thread(
                self.log_event,
                "✓" if "Couldn't" not in note and "instead" not in note else "!",
                note,
            )
        self.call_from_thread(
            self.finished, op, "✓", f"Opened {self.label(env)} in the Claude desktop app", ""
        )

    def set_stage(self, op: Op, stage: str) -> None:
        op.stage = stage
        self.update_stages()

    # ----- actions: new -----

    def action_new(self) -> None:
        op = self.ops.start("Asking Ona about projects")
        self._load_catalog(op)

    @work(thread=True, exit_on_error=False)
    def _load_catalog(self, op: Op) -> None:
        try:
            projects, classes = ops.catalog()
        except Exception as e:
            self.call_from_thread(self.failed, op, e)
            return
        self.call_from_thread(self._show_new_form, op, projects, classes)

    def _show_new_form(
        self,
        op: Op,
        projects: list[Project],
        classes: list[EnvClass],
        *,
        name: str = "",
        project: Project | None = None,
        env_class: EnvClass | None = None,
        then: str = "connect",
    ) -> None:
        self.ops.finish(op)
        if not projects:
            self.log_event("✗", "Ona lists no projects to create from.")
            return
        if project is None:
            project, env_class = ops.pick_defaults(projects, classes, self.envs)
        form = screens.NewEnvironment(
            projects, classes, self.envs, project=project, env_class=env_class, name=name, then=then
        )

        def decided(request: screens.NewRequest | None) -> None:
            if request is not None:
                self.create(request, projects, classes)

        self.push_screen(form, decided)

    def create(
        self, request: screens.NewRequest, projects: list[Project], classes: list[EnvClass]
    ) -> None:
        op = self.ops.start("Creating")
        ghost_id = f"ghost:{op.id}"
        op.env_ids = (ghost_id,)
        name = request.name or f"new environment from {request.project.name or request.project.id}"
        self.ghosts.append((ghost_id, name, op))
        self.cursor_id = ghost_id
        self.rebuild()
        self.log_event(
            "→",
            f"Creating {name}",
            f"from {request.project.name or request.project.id}"
            + (f" · {request.env_class}" if request.env_class else " · on Ona's default class"),
        )
        self._create(op, request, list(self.envs), projects, classes)

    @work(thread=True, exit_on_error=False)
    def _create(
        self,
        op: Op,
        request: screens.NewRequest,
        before: list[Env],
        projects: list[Project],
        classes: list[EnvClass],
    ) -> None:
        try:
            env, took = ops.create(request.project, request.env_class, request.name, before)
        except Exception as e:
            self.call_from_thread(self._create_failed, op, request, projects, classes, e)
            return
        self.call_from_thread(self._created, op, request, env, took)

    def _drop_ghost(self, op: Op) -> None:
        self.ghosts = [g for g in self.ghosts if g[2] is not op]
        if self.cursor_id and self.cursor_id.startswith("ghost:"):
            self.cursor_id = None

    def _create_failed(
        self,
        op: Op,
        request: screens.NewRequest,
        projects: list[Project],
        classes: list[EnvClass],
        e: Exception,
    ) -> None:
        # Say which one failed while its ghost row still knows the name.
        self.failed(op, e)
        self._drop_ghost(op)
        self.rebuild()
        # Back to the form, with what you typed still there.
        self._show_new_form(
            self.ops.start("Reopening the form"),
            projects,
            classes,
            name=request.name,
            project=request.project,
            env_class=request.env_class,
            then=request.then,
        )

    def _created(self, op: Op, request: screens.NewRequest, env: Env, took: float) -> None:
        self._drop_ghost(op)
        # Ona's next list has it too; until then, show the one it told us about.
        if all(e.id != env.id for e in self.envs):
            self.envs = [*self.envs, env]
        self.cursor_id = env.id
        self.finished(op, "✓", f"Created {self.label(env)}", f"in {render.duration(took)}")
        self.poll()
        if request.then == "connect":
            kind = handoff.detect(self.environ, self.connect_pref)
            doing = "to connect" if kind == "here" else f"to connect {handoff.WHERE[kind]}"
            self.push_screen(
                screens.Ready(self.label(env), doing),
                lambda go: self.connect(env, False) if go else None,
            )
        elif request.then == "editor":
            self.open_editor(env, self.editor_default)
        elif request.then == "claude":
            try:
                ops.check_claude()
            except UiError as e:
                self.log_event("✗", str(e), e.detail or "", e.hint or "")
                return
            if ops.claude_needs_consent():
                self.push_screen(
                    screens.Consent(), lambda agreed: self.claude_flow(env) if agreed else None
                )
            else:
                self.claude_flow(env)

    # ----- results -----

    def finished(self, op: Op, mark: str, message: str, note: str) -> None:
        self.ops.finish(op, message)
        self.log_event(mark, message, note, env_ids=op.env_ids)
        self.then.pop(op.id, None)
        for env_id in op.env_ids:
            self.failures.pop(env_id, None)
        self.recompute()
        self.poll()

    def failed(self, op: Op, e: Exception) -> None:
        self.ops.finish(op, str(e), failed=True)
        self.then.pop(op.id, None)
        detail = getattr(e, "detail", None) or ""
        hint = getattr(e, "hint", None) or ""
        wrapped = e if isinstance(e, UiError) else UiError(str(e), detail or None, hint or None)
        for env_id in op.env_ids:
            self.overlays.clear(env_id)
            self.failures[env_id] = wrapped
        who = self.names(op.env_ids)
        self.log_event(
            "✗", f"{op.verb} {who}: {e}".strip() if who else str(e), detail, hint, op.env_ids
        )
        if wrapped.about_auth:
            self.poll_error = wrapped
            self.update_banner()
        self.recompute()
