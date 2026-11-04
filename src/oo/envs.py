"""What an Ona environment looks like to us, and how to find one from a few typed characters."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# IDs are UUIDv7s, which share a timestamp up front, so the end is what tells them apart.
SHORT_ID_LENGTH = 6

# Names double as SSH host names for the Claude app, so they must be one host name label: at
# most 63 characters, less room for the `-` and short ID that `oo new` adds when one is taken.
SAFE_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?")
MAX_NAME = 63 - 1 - SHORT_ID_LENGTH

GONE = ("deleting", "deleted")

# Most settled first, so the list shows what you're working on at the top.
PHASE_ORDER = ["running", "starting", "creating", "updating", "stopping", "stopped"]


def safe_name(name: str) -> bool:
    """Whether `name` works as an SSH host name: letters, digits and inner hyphens."""
    return len(name) <= MAX_NAME and SAFE_NAME.fullmatch(name) is not None


def name_taken(name: str, envs: list[Env], but: str = "") -> bool:
    """Whether an environment other than `but` has `name`, ignoring case as host names do."""
    return any(e.name.lower() == name.lower() for e in envs if e.id != but and e.phase not in GONE)


def _dig(data: Any, *path: str | int) -> Any:
    for key in path:
        try:
            data = data[key]
        except (KeyError, IndexError, TypeError):
            return None
    return data


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    # Ona can send nanoseconds, which fromisoformat won't take.
    value = re.sub(r"(\.\d{6})\d+", r"\1", value)
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class Env:
    id: str
    name: str
    repo_url: str
    phase: str
    last_active: datetime | None
    changes: tuple[str, ...] = ()
    changed_count: int = 0
    project_id: str = ""
    class_id: str = ""
    created: datetime | None = None
    checkout: str = ""

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Env:
        git = _dig(data, "status", "content", "git") or {}
        # Before the content is cloned, only the spec knows what's coming.
        initializer = _dig(data, "spec", "content", "initializer", "specs", 0, "git") or {}
        changes = tuple(c.get("path", "") for c in git.get("changedFiles") or [])
        phase = (_dig(data, "status", "phase") or "").removeprefix("ENVIRONMENT_PHASE_").lower()
        # Ona marks a stop or deletion in the spec straight away, but the phase lags behind.
        desired = (_dig(data, "spec", "desiredPhase") or "").removeprefix("ENVIRONMENT_PHASE_")
        if desired == "DELETED" and phase != "deleted":
            phase = "deleting"
        elif desired == "STOPPED" and phase in ("running", "starting", "creating", "updating"):
            phase = "stopping"
        created = _timestamp(_dig(data, "metadata", "createdAt"))
        checkout = _dig(data, "status", "content", "contentLocationInMachine") or ""
        if not checkout and initializer.get("checkoutLocation"):
            checkout = f"/workspaces/{initializer['checkoutLocation']}"
        return cls(
            id=data.get("id", ""),
            name=_dig(data, "metadata", "name") or "",
            repo_url=git.get("cloneUrl") or initializer.get("remoteUri") or "",
            phase=phase if phase and phase != "unspecified" else "unknown",
            last_active=_timestamp(_dig(data, "status", "activitySignal", "timestamp"))
            or _timestamp(_dig(data, "metadata", "lastStartedAt"))
            or created,
            changes=changes,
            changed_count=max(git.get("totalChangedFiles") or 0, len(changes)),
            project_id=_dig(data, "metadata", "projectId") or "",
            class_id=_dig(data, "spec", "machine", "class") or "",
            created=created,
            checkout=checkout,
        )

    @property
    def running(self) -> bool:
        return self.phase == "running"

    @property
    def short_id(self) -> str:
        return self.id[-SHORT_ID_LENGTH:]

    @property
    def repo(self) -> str:
        """Just the repository's name, e.g. `web`."""
        return self.repo_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")

    @property
    def label(self) -> str:
        """What oo calls this environment: its name, or the end of its ID if it has none."""
        return self.name or self.short_id


def newest(envs: list[Env]) -> Env | None:
    """The most recently created environment."""
    dated = [e for e in envs if e.created]
    return max(dated, key=lambda e: e.created) if dated else None


def sort_envs(envs: list[Env]) -> list[Env]:
    """Running first, then everything else by how recently it was used."""

    def key(env: Env) -> tuple[int, float]:
        rank = PHASE_ORDER.index(env.phase) if env.phase in PHASE_ORDER else len(PHASE_ORDER)
        return rank, -(env.last_active.timestamp() if env.last_active else 0)

    return sorted(envs, key=key)


class NoMatch(Exception):
    def __init__(self, query: str):
        super().__init__(query)
        self.query = query


class Ambiguous(Exception):
    def __init__(self, query: str, candidates: list[Env]):
        super().__init__(query)
        self.query = query
        self.candidates = candidates


def matches(envs: list[Env], query: str) -> list[Env]:
    """Environments matching `query`, from the tightest kind of match that finds any.

    Names are compared ignoring case, as host names are.
    """
    q = query.strip().lower()
    if not q:
        return []
    tiers = [
        lambda e: q in (e.id, e.name.lower()),
        lambda e: e.name.lower().startswith(q),
        lambda e: q in e.name.lower(),
        lambda e: e.id.startswith(q) or e.id.endswith(q),
    ]
    for tier in tiers:
        if hits := [e for e in envs if tier(e)]:
            return hits
    return []


def pick(envs: list[Env], query: str) -> Env:
    """The one environment `query` means, or Ambiguous if it could be several."""
    hits = matches(envs, query)
    if not hits:
        raise NoMatch(query)
    if len(hits) > 1:
        raise Ambiguous(query, sort_envs(hits))
    return hits[0]
