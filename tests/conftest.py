from __future__ import annotations

from typing import Any

import pytest


def raw_env(
    id: str,
    branch: str,
    phase: str = "stopped",
    *,
    active: str = "2026-09-30T12:00:00Z",
    created: str = "2026-09-01T00:00:00Z",
    repo: str = "https://github.com/acme/widgets.git",
    commit: str = "a" * 40,
    changed: list[str] | None = None,
    name: str | None = None,
    project: str = "p-1",
    env_class: str = "c-fr",
    desired: str = "running",
    checkout: str = "/workspaces/widgets",
) -> dict[str, Any]:
    """Just enough of `ona environment list --format json` to be realistic."""
    git: dict[str, Any] = {"branch": branch, "cloneUrl": repo, "latestCommit": commit}
    if changed:
        git["changedFiles"] = [{"changeType": "CHANGE_TYPE_MODIFIED", "path": p} for p in changed]
        git["totalChangedFiles"] = len(changed)
    return {
        "id": id,
        "metadata": {"name": name, "createdAt": created, "projectId": project},
        "spec": {
            "desiredPhase": f"ENVIRONMENT_PHASE_{desired.upper()}",
            "machine": {"class": env_class},
        },
        "status": {
            "phase": f"ENVIRONMENT_PHASE_{phase.upper()}",
            "activitySignal": {"timestamp": active},
            "content": {"git": git, "contentLocationInMachine": checkout},
        },
    }


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    for var in ("OO_BRANCH_PREFIX", "OO_EDITOR", "OO_PROJECT", "OO_CLASS"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def ssh_configs(monkeypatch, tmp_path):
    """Keep tests away from the real Claude desktop app's SSH connections."""
    from oo import claude

    path = tmp_path / "ssh_configs.json"
    monkeypatch.setattr(claude, "ssh_configs_path", lambda: path)
    # And away from your own ~/.ssh, which forget reads to know oo's hosts.
    monkeypatch.setattr(claude, "_ssh_dir", lambda: tmp_path / ".ssh")
    return path
