"""What the screen keeps track of between polls: names, matching, marks, and work in flight."""

from __future__ import annotations

import difflib
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from ..envs import Env, branch_prefix, matches

# Phases a row can be in while Ona is changing it. These poll faster.
TRANSITIONAL = ("starting", "creating", "updating", "stopping", "deleting")
ACTIVE = ("running", "starting", "creating", "updating")
PHASE_WORDS = ("running", "starting", "stopping", "stopped", "deleting", "creating", "updating")

# How long to show what we asked Ona for before trusting what it says instead.
OVERLAY_TIMEOUT = 90.0


def prefix() -> str:
    """The branch prefix the list leaves off, from the environment."""
    return branch_prefix()


def label(env: Env, prefix: str) -> str:
    """What the screen calls an environment: the same name `oo` uses."""
    return env.short_branch(prefix)


def search(envs: list[Env], query: str, prefix: str) -> list[Env]:
    """The rows `query` picks out.

    A phase word like `stopped` picks every environment in that phase. Anything else goes
    through the same tiers `oo c NAME` uses, so what you'd type there works here.
    """
    q = query.strip().lower()
    if not q:
        return list(envs)
    if q in PHASE_WORDS:
        return [e for e in envs if e.phase == q]
    return matches(envs, query, prefix)


def suggestion(envs: list[Env], query: str, prefix: str) -> str | None:
    """A name close to `query`, for a did-you-mean."""
    close = difflib.get_close_matches(query.strip(), [label(e, prefix) for e in envs], n=1)
    return close[0] if close else None


def names(envs: list[Env], prefix: str) -> str:
    """e.g. `a`, `a and b`, `a, b and c`, by the names the screen shows."""
    named = [label(e, prefix) for e in envs]
    if not named:
        return ""
    return named[0] if len(named) == 1 else ", ".join(named[:-1]) + f" and {named[-1]}"


def twins(envs: list[Env], prefix: str) -> bool:
    """Whether two rows would show the same name, so IDs are needed to tell them apart."""
    return len({label(e, prefix).lower() for e in envs}) < len(envs)


@dataclass(frozen=True, slots=True)
class Overlay:
    """A phase we asked Ona for, shown until Ona's own phase moves on."""

    phase: str
    was: str  # the phase Ona reported when we asked
    since: float


class Overlays:
    """Phases to show ahead of Ona, so a keypress shows at once.

    After `oo ui` asks Ona to start, stop or delete an environment, Ona's list keeps reporting
    the old phase for a few seconds. An overlay shows the new phase in the meantime. It goes
    away when Ona reports any other phase, when the environment is gone, or after
    OVERLAY_TIMEOUT seconds, in which case `apply` reports it as expired.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._by_id: dict[str, Overlay] = {}

    def set(self, env: Env, phase: str) -> None:
        self._by_id[env.id] = Overlay(phase, env.phase, self._clock())

    def clear(self, env_id: str) -> None:
        self._by_id.pop(env_id, None)

    def __contains__(self, env_id: str) -> bool:
        return env_id in self._by_id

    def __len__(self) -> int:
        return len(self._by_id)

    def apply(self, envs: list[Env]) -> tuple[list[Env], list[str]]:
        """`envs` with overlays in place of stale phases, and the IDs of overlays that expired."""
        now = self._clock()
        expired = []
        shown = []
        seen = set()
        for env in envs:
            seen.add(env.id)
            overlay = self._by_id.get(env.id)
            if overlay is None:
                shown.append(env)
            elif env.phase != overlay.was:
                del self._by_id[env.id]
                shown.append(env)
            elif now - overlay.since > OVERLAY_TIMEOUT:
                del self._by_id[env.id]
                expired.append(env.id)
                shown.append(env)
            else:
                shown.append(replace(env, phase=overlay.phase))
        for env_id in list(self._by_id):
            if env_id not in seen:
                del self._by_id[env_id]
        return shown, expired


@dataclass(slots=True)
class Op:
    """One thing Ona is doing for us, from a keypress until it reports back."""

    id: int
    verb: str  # e.g. "Starting"
    env_ids: tuple[str, ...]
    started: float
    stage: str = ""  # what it's up to right now, if more than the verb
    done: bool = False
    failed: bool = False
    result: str = ""

    def elapsed(self, now: float) -> float:
        return now - self.started


class Ops:
    """Everything in flight, so the screen can show it and refuse a second go at a busy row."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._next = 1
        self._ops: dict[int, Op] = {}

    def start(self, verb: str, env_ids: tuple[str, ...] = ()) -> Op:
        op = Op(self._next, verb, env_ids, self._clock())
        self._ops[op.id] = op
        self._next += 1
        return op

    def finish(self, op: Op, result: str = "", *, failed: bool = False) -> None:
        op.done = True
        op.failed = failed
        op.result = result
        del self._ops[op.id]

    def busy(self, env_id: str) -> Op | None:
        """The op working on `env_id`, if any."""
        return next((op for op in self._ops.values() if env_id in op.env_ids), None)

    def live(self) -> list[Op]:
        return list(self._ops.values())

    def __len__(self) -> int:
        return len(self._ops)


@dataclass(slots=True)
class Event:
    """A line for the activity log."""

    when: float
    mark: str  # ✓ ! ✗ → ↩
    message: str
    detail: str = ""
    hint: str = ""
    env_ids: tuple[str, ...] = field(default_factory=tuple)
