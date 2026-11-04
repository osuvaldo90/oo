"""The dialogs `oo ui` opens over the list: a form, confirmations, pickers, and help."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    OptionList,
    RadioButton,
    RadioSet,
    Select,
    Static,
)
from textual.widgets.option_list import Option

from .. import render
from ..envs import MAX_NAME, Env, name_taken, safe_name
from ..projects import EnvClass, Project
from .model import ACTIVE
from .ops import Editor

THEN_STEPS = (
    ("connect", "SSH into it"),
    ("editor", "Open it in your editor"),
    ("claude", "Open it in the Claude desktop app"),
    ("nothing", "Just create it"),
)

DIALOG_CSS = """
ModalScreen {
    align: center middle;
}
#dialog {
    width: 72;
    max-width: 96%;
    height: auto;
    max-height: 90%;
    padding: 1 2;
    background: $surface;
    border: round $primary;
}
#dialog .title {
    text-style: bold;
    margin-bottom: 1;
}
#dialog .field {
    margin-top: 1;
    color: $text-muted;
}
#dialog .note {
    color: $text-muted;
}
#dialog .warn {
    color: #eb9a12;
}
#dialog .buttons {
    height: auto;
    margin-top: 1;
    align-horizontal: right;
}
#dialog Button {
    margin-left: 1;
}
#dialog RadioSet {
    width: 100%;
    border: none;
    padding: 0;
    background: transparent;
}
#dialog OptionList {
    height: auto;
    max-height: 16;
}
"""


@dataclass(frozen=True, slots=True)
class NewRequest:
    """What the New environment form asks for."""

    name: str
    project: Project
    env_class: EnvClass | None
    then: str  # one of THEN_STEPS


class NewEnvironment(ModalScreen[NewRequest | None]):
    """The form behind `n`: name, project, class, and what to do once it's ready."""

    CSS = DIALOG_CSS
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel", show=False)]
    AUTO_FOCUS = "#name"

    def __init__(
        self,
        projects: list[Project],
        classes: list[EnvClass],
        envs: list[Env],
        *,
        project: Project | None = None,
        env_class: EnvClass | None = None,
        name: str = "",
        then: str = "connect",
    ) -> None:
        super().__init__()
        self.projects = projects
        self.class_by_id = {c.id: c for c in classes}
        self.envs = envs
        self.initial_project = project or (projects[0] if projects else None)
        self.initial_class = env_class
        self.initial_name = name
        self.initial_then = then

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("New environment", classes="title")
            yield Label("Name", classes="field")
            yield Input(
                value=self.initial_name,
                placeholder="what oo and the Claude app call it, like login-fix",
                id="name",
            )
            yield Static("", id="name-note", classes="note")
            yield Label("Project", classes="field")
            yield Select(
                [(p.name or p.id, p.id) for p in self.projects],
                value=self.initial_project.id if self.initial_project else Select.NULL,
                allow_blank=not self.projects,
                prompt="No projects",
                id="project",
            )
            yield Label("Class", classes="field")
            yield Select(
                self._class_options(self.initial_project),
                value=self.initial_class.id if self.initial_class else Select.NULL,
                prompt="Ona's default class",
                id="class",
            )
            yield Label("Then", classes="field")
            with RadioSet(id="then"):
                for key, text in THEN_STEPS:
                    yield RadioButton(text, value=key == self.initial_then, id=f"then-{key}")
            with Horizontal(classes="buttons"):
                yield Button("Create", variant="primary", id="create")
                yield Button("Cancel", id="cancel")

    def _class_options(self, project: Project | None) -> list[tuple[str, str]]:
        """The project's classes first, in the order Ona's picker shows them, then the rest."""
        offered = [
            self.class_by_id[i]
            for i in (project.class_ids if project else ())
            if i in self.class_by_id
        ]
        rest = [c for c in self.class_by_id.values() if c not in offered]
        return [(str(c), c.id) for c in [*offered, *rest]]

    def on_mount(self) -> None:
        self._check_name(self.initial_name)

    def _project(self) -> Project | None:
        value = self.query_one("#project", Select).value
        return next((p for p in self.projects if p.id == value), None)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "project":
            return
        picker = self.query_one("#class", Select)
        current = picker.value
        picker.set_options(self._class_options(self._project()))
        if current is not Select.NULL and current in self.class_by_id:
            picker.value = current

    def on_input_changed(self, event: Input.Changed) -> None:
        self._check_name(event.value)

    def _check_name(self, name: str) -> bool:
        """Say what's up with the name, and whether it can go to Ona."""
        name = name.strip()
        note = self.query_one("#name-note", Static)
        if name and not safe_name(name):
            note.update(f"Use up to {MAX_NAME} letters, digits and hyphens, like login-fix.")
            note.set_class(True, "warn")
            return False
        taken = bool(name) and name_taken(name, self.envs)
        note.update(
            f"Another environment has this name; it'll be {name}-<end of ID>." if taken else ""
        )
        note.set_class(taken, "warn")
        return True

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._create()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "create":
            self._create()
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _create(self) -> None:
        name = self.query_one("#name", Input).value.strip()
        if not name:
            self.query_one("#name-note", Static).update("Give it a name.")
            self.query_one("#name", Input).focus()
            return
        if not self._check_name(name):
            return
        project = self._project()
        if project is None:
            self.query_one("#name-note", Static).update("Pick a project to create from.")
            return
        picked = self.query_one("#class", Select).value
        env_class = self.class_by_id.get(picked) if isinstance(picked, str) else None
        pressed = self.query_one("#then", RadioSet).pressed_button
        then = (pressed.id or "then-connect").removeprefix("then-") if pressed else "connect"
        self.dismiss(NewRequest(name, project, env_class, then))


class ConfirmDelete(ModalScreen[bool]):
    """Deleting can't be undone, so the confirmation scales with what's at stake.

    With every target stopped and clean, `y` does it. One running or changed target wants its
    name typed. A batch with any such target wants the word `delete`.
    """

    CSS = DIALOG_CSS
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, targets: list[Env], labels: dict[str, str], forgets: list[str]) -> None:
        super().__init__()
        self.targets = targets
        self.labels = labels
        self.forgets = forgets
        risky = [t for t in targets if t.phase in ACTIVE or t.changed_count]
        if not risky:
            self.gate = "y"
        elif len(targets) == 1:
            self.gate = "name"
        else:
            self.gate = "word"

    def _expected(self, typed: str) -> bool:
        typed = typed.strip().lower()
        if self.gate == "name":
            target = self.targets[0]
            return typed in (self.labels[target.id].lower(), target.short_id.lower())
        return typed == "delete"

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            if len(self.targets) == 1:
                yield Label(f"Delete {self.labels[self.targets[0].id]}?", classes="title")
            else:
                yield Label(f"Delete {len(self.targets)} environments?", classes="title")
            for target in self.targets:
                line = Text.assemble(render.phase(target), "  ", (self.labels[target.id], "bold"))
                line.append(f"  ·  active {render.ago(target.last_active)}", style=render.FAINT)
                yield Static(line)
                if target.changed_count:
                    yield Static(
                        f"  ! {target.changed_count} uncommitted "
                        f"{'file' if target.changed_count == 1 else 'files'} will be lost:",
                        classes="warn",
                    )
                    for path in target.changes[:5]:
                        yield Static(f"      {path}", classes="warn")
                    if target.changed_count > len(target.changes[:5]):
                        yield Static(
                            f"      … and {target.changed_count - len(target.changes[:5])} more",
                            classes="warn",
                        )
            if self.forgets:
                yield Static(
                    f"Also removes {', '.join(self.forgets)} from the Claude desktop app's SSH "
                    "connections.",
                    classes="note",
                )
            yield Static("Ona deletes in the background. This can't be undone.", classes="note")
            if self.gate == "y":
                yield Static("Press y to delete, or Esc to keep it.", classes="field")
            elif self.gate == "name":
                yield Label("Type its name to confirm", classes="field")
                yield Input(placeholder=self.labels[self.targets[0].id], id="confirm")
            else:
                yield Label("Type delete to confirm", classes="field")
                yield Input(placeholder="delete", id="confirm")
            with Horizontal(classes="buttons"):
                yield Button("Delete", variant="error", id="delete", disabled=self.gate != "y")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        if self.gate == "y":
            # Cancel has focus, so a stray Enter keeps the environment.
            self.query_one("#cancel", Button).focus()
        else:
            # Enter in the box does nothing until the right word is in it.
            self.query_one("#confirm", Input).focus()

    def on_key(self, event: events.Key) -> None:
        if self.gate == "y" and event.key == "y":
            event.stop()
            self.dismiss(True)

    def on_input_changed(self, event: Input.Changed) -> None:
        self.query_one("#delete", Button).disabled = not self._expected(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._expected(event.value):
            self.dismiss(True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "delete" and not event.button.disabled:
            self.dismiss(True)
        else:
            self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


class Ready(ModalScreen[bool]):
    """An environment we started for you is running. Nothing takes the terminal without a nod."""

    CSS = DIALOG_CSS
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "go", "Connect", show=False),
        Binding("escape", "stay", "Stay", show=False),
    ]

    def __init__(self, label: str, doing: str) -> None:
        super().__init__()
        self.label = label
        self.doing = doing

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"{self.label} is running", classes="title")
            yield Static(f"Enter {self.doing} · Esc to stay here")

    def action_go(self) -> None:
        self.dismiss(True)

    def action_stay(self) -> None:
        self.dismiss(False)


class Consent(ModalScreen[bool]):
    """What opening in the Claude desktop app sets up, the first time on this machine."""

    CSS = DIALOG_CSS
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "no", "Not now", show=False)]

    CHANGES = (
        "Make an SSH key with no passphrase at ~/.ssh/oo_claude, for the app's use only.",
        "Add `Include oo/config` at the top of ~/.ssh/config, keeping a copy at config.oo-backup.",
        "Write a host entry per environment to ~/.ssh/oo/config, reaching its own sshd on "
        "port 2222 through Ona's SSH.",
        "Add the key to ~/.ssh/authorized_keys on each environment you open this way.",
        "Name the connection in the app's SSH picker after the environment.",
    )

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Set up SSH for the Claude desktop app?", classes="title")
            yield Static(
                "The app can't install its helper over Ona's SFTP server, so oo gives it a "
                "second way in. This is what it changes:",
                classes="note",
            )
            for change in self.CHANGES:
                yield Static(f"  • {change}")
            yield Static(
                "docs/claude-desktop.md in the oo repo explains why, what this exposes, and how "
                "to undo it. You'll only be asked once.",
                classes="note field",
            )
            with Horizontal(classes="buttons"):
                yield Button("Set it up", variant="primary", id="yes")
                yield Button("Not now", id="no")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_no(self) -> None:
        self.dismiss(False)


class PickEditor(ModalScreen[str | None]):
    """Which editor to open an environment in, from `ona environment list-editors`."""

    CSS = DIALOG_CSS
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, editors: list[Editor], default: str | None = None) -> None:
        super().__init__()
        self.editors = editors
        self.default = default

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Open in which editor?", classes="title")
            options = [
                Option(Text.assemble((e.alias, "bold"), f"  {e.name}"), id=e.alias)
                for e in self.editors
            ]
            yield OptionList(*options, id="editors")

    def on_mount(self) -> None:
        picker = self.query_one("#editors", OptionList)
        picker.focus()
        aliases = [e.alias for e in self.editors]
        if self.default in aliases:
            picker.highlighted = aliases.index(self.default)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option_id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmQuit(ModalScreen[bool]):
    """Something is still in flight. Ona finishes it either way; say so before leaving."""

    CSS = DIALOG_CSS
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "stay", "Stay", show=False)]

    def __init__(self, doing: list[str]) -> None:
        super().__init__()
        self.doing = doing

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Quit while Ona is still working?", classes="title")
            for line in self.doing:
                yield Static(f"  ◐ {line}")
            yield Static("Ona finishes these on its own. Run oo to check on them.", classes="note")
            with Horizontal(classes="buttons"):
                yield Button("Quit", variant="error", id="quit")
                yield Button("Stay", id="stay")

    def on_mount(self) -> None:
        self.query_one("#stay", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "quit")

    def action_stay(self) -> None:
        self.dismiss(False)


class Help(ModalScreen[None]):
    """Every key, and where sessions open."""

    CSS = DIALOG_CSS
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,question_mark,q", "close", "Close", show=False),
    ]

    KEYS = (
        ("⏎  c", "SSH into the environment, starting it first if it's stopped"),
        ("C", "…always here, in this terminal, whatever $OO_CONNECT says"),
        ("o", "Open it in your editor ($OO_EDITOR, else Ona's default)"),
        ("O", "Open it in an editor you pick"),
        ("a", "Open a Claude Code session on it in the Claude desktop app"),
        ("n", "New environment"),
        ("s  x", "Start · stop the environment, or every marked one"),
        ("D", "Delete it, or every marked one, after confirming"),
        ("␣  *", "Mark the row · mark every row shown"),
        ("/", "Filter rows by name, or by a phase like stopped"),
        ("i  L", "Toggle the details pane · the activity log"),
        ("y", "Copy the environment's ID"),
        ("r", "Refresh now"),
        ("!", "Run ona login here, then refresh"),
        ("j  k  g  G", "Move the cursor, jump to the top or bottom"),
        ("esc", "Clear the filter, the marks, or close the log"),
        ("q", "Quit"),
    )

    def __init__(self, where: str, connect: str) -> None:
        super().__init__()
        self.where = where
        self.connect = connect

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("oo ui", classes="title")
            for key, text in self.KEYS:
                yield Static(Text.assemble((f"{key:<12}", f"bold {render.ACCENT}"), text))
            yield Static(
                f"SSH sessions open {self.where} ($OO_CONNECT={self.connect}). Set it to auto, "
                "here, cmux, tmux or tab.",
                classes="note field",
            )
            yield Static("Esc closes this.", classes="note")

    def action_close(self) -> None:
        self.dismiss(None)
