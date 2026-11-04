from __future__ import annotations

import json

import pytest
from conftest import raw_env
from typer.testing import CliRunner

from oo import claude, cli, github, ona, ssh
from oo.envs import Env
from oo.github import BranchStatus, GitHubError, PullRequest
from oo.ona import OnaError
from oo.projects import EnvClass, Project

RAW = [
    raw_env("11111111-aaaa", "sam/feat-10", "running", created="2026-09-20T00:00:00Z"),
    raw_env("22222222-bbbb", "sam/feat-10-2", changed=["src/a.py"]),
    raw_env("33333333-cccc", "sam/docs"),
]
FRANKFURT = EnvClass(id="c-fr", name="m7i.xlarge", runner="Frankfurt")
TOKYO = EnvClass(id="c-tk", name="m7i.xlarge", runner="Tokyo")
WIDGETS = Project(id="p-1", name="Widgets", class_ids=("c-tk", "c-fr"))


class Called(Exception):
    """Stands in for exec, which would replace the test process."""


@pytest.fixture
def envs() -> list[Env]:
    return [Env.from_json(r) for r in RAW]


@pytest.fixture
def calls(monkeypatch, envs):
    """Fake ona and GitHub: canned answers, and a record of what we asked ona to do."""
    record: list[tuple[str, ...]] = []

    def replace_with(*args):
        record.append(args)
        raise Called

    def quietly(*args, failed, on_tick=None):
        record.append(args)
        if args[:2] == ("environment", "create"):
            envs.append(Env.from_json(raw_env("99999999-new", "main", "running")))
            return 3.0, "99999999-new\n"
        return 3.0, ""

    monkeypatch.setattr(ona, "list_envs", lambda timeout=None: list(envs))
    monkeypatch.setattr(ona, "list_projects", lambda: [WIDGETS])
    monkeypatch.setattr(ona, "list_classes", lambda: [TOKYO, FRANKFURT])
    monkeypatch.setattr(ona, "replace_with", replace_with)
    monkeypatch.setattr(ona, "quietly", quietly)
    monkeypatch.setattr(github, "lookup", lambda envs: {})
    monkeypatch.setattr(ssh, "configured", lambda env_id: True)
    monkeypatch.setattr(ssh, "replace_with_scp", lambda *args: replace_with("scp", *args))
    monkeypatch.setattr(claude, "check_installed", lambda: None)
    monkeypatch.setattr(claude, "set_up_host", lambda env_id, live_ids, name="": ssh.host(env_id))
    monkeypatch.setattr(claude, "open_link", lambda url: record.append(("open", url)))
    monkeypatch.setenv("OO_BRANCH_PREFIX", "sam")
    return record


def oo(*args: str, input: str | None = None):
    return CliRunner().invoke(cli.app, list(args), input=input)


def test_lists_by_default(calls):
    result = oo()
    assert result.exit_code == 0
    assert "feat-10-2" in result.output
    assert "1 uncommitted" in result.output
    assert calls == []


def test_lists_pull_requests_and_unpushed_work(calls, monkeypatch):
    pr = PullRequest(number=42, state="merged", url="https://github.com/acme/widgets/pull/42")
    statuses = {
        "11111111-aaaa": BranchStatus(pr=pr, pushed=True),
        "22222222-bbbb": BranchStatus(pr=None, pushed=False),
    }
    monkeypatch.setattr(github, "lookup", lambda envs: statuses)
    result = oo()
    assert "#42 merged" in result.output
    assert "1 uncommitted, unpushed" in result.output


def test_lists_anyway_when_github_fails(calls, monkeypatch):
    def broken(envs):
        raise GitHubError("Bad credentials")

    monkeypatch.setattr(github, "lookup", broken)
    result = oo()
    assert result.exit_code == 0
    assert "Bad credentials" in result.output
    assert "feat-10-2" in result.output


def test_connects_by_short_branch(calls):
    result = oo("c", "feat-10")
    assert isinstance(result.exception, Called)
    assert calls == [("environment", "ssh", "11111111-aaaa")]


def test_passes_a_remote_command_through(calls):
    oo("c", "feat-10", "--", "git", "status", "-sb")
    assert calls == [("environment", "ssh", "11111111-aaaa", "--", "git", "status", "-sb")]


def test_starts_a_stopped_one_quietly_before_connecting(calls):
    result = oo("c", "docs")
    assert "started in 3s" in result.output
    assert calls == [
        ("environment", "start", "33333333-cccc"),
        ("environment", "ssh", "33333333-cccc"),
    ]


def test_ona_failures_get_shown_nicely(calls, monkeypatch):
    def broken(*args, failed, on_tick=None):
        raise OnaError(failed, detail="not_found: environment not found", hint="- try again")

    monkeypatch.setattr(ona, "quietly", broken)
    result = oo("rm", "docs")
    assert result.exit_code == 1
    assert "✗ Ona couldn't delete sam/docs." in result.output
    assert "not_found: environment not found" in result.output
    assert "- try again" in result.output


def test_connect_with_no_branch_picks_the_only_running_one(calls):
    oo("c")
    assert calls == [("environment", "ssh", "11111111-aaaa")]


def test_picks_from_a_list_when_its_not_obvious(calls, monkeypatch, envs):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    envs.pop(0)  # nothing running now
    result = oo("c", input="9\n2\n")
    assert "Which one?" in result.output
    assert calls[-1] == ("environment", "ssh", "33333333-cccc")


def test_ambiguous_branch_is_an_error(calls):
    result = oo("c", "feat")
    assert result.exit_code == 1
    assert "matches 2 environments" in result.output
    assert calls == []


def test_code_uses_the_editor_from_the_environment(calls, monkeypatch):
    monkeypatch.setenv("OO_EDITOR", "cursor")
    oo("code", "docs")
    assert calls[-1] == ("environment", "open", "33333333-cccc", "--start", "--editor", "cursor")


def test_open_is_now_code(calls):
    result = oo("open", "docs")
    assert result.exit_code == 2
    assert calls == []


def test_claude_opens_the_only_running_one_in_the_desktop_app(calls):
    result = oo("claude")
    assert result.exit_code == 0
    link = "claude://code/new?ssh_host=11111111-aaaa.ona.environment&ssh_folder=%2Fworkspaces%2Fwidgets"
    assert calls == [("open", link)]


def own_host(env_id, live_ids, name=""):
    """A fake set_up_host that works, as it would with the box's own sshd running."""
    return claude.own_host(env_id, name)


def test_claude_uses_the_environments_own_sshd_when_it_can(calls, monkeypatch):
    monkeypatch.setattr(claude, "set_up_host", own_host)
    result = oo("claude")
    assert result.exit_code == 0
    link = "claude://code/new?ssh_host=11111111-aaaa.claude.oo&ssh_folder=%2Fworkspaces%2Fwidgets"
    assert calls == [("open", link)]


def test_claude_falls_back_to_onas_host_and_says_why(calls, monkeypatch):
    def unavailable(env_id, live_ids, name=""):
        raise claude.ClaudeError("Nothing is listening on port 2222 in the environment.")

    monkeypatch.setattr(claude, "set_up_host", unavailable)
    result = oo("claude")
    assert result.exit_code == 0
    assert "Nothing is listening on port 2222" in result.output
    assert "Opening with Ona's own" in result.output
    assert calls[0][1].startswith("claude://code/new?ssh_host=11111111-aaaa.ona.environment")


def test_claude_starts_a_stopped_one_and_sets_up_ssh_first(calls, monkeypatch):
    answers = iter([False, True])
    monkeypatch.setattr(ssh, "configured", lambda env_id: next(answers))
    result = oo("claude", "docs")
    assert "Set up SSH for Ona environments" in result.output
    assert calls[0] == ("environment", "ssh-config")
    assert calls[1] == ("environment", "start", "33333333-cccc")
    assert calls[2][1].startswith("claude://code/new?ssh_host=33333333-cccc.ona.environment")


def test_claude_says_when_the_app_wont_open(calls, monkeypatch):
    def broken(url):
        raise claude.ClaudeError("Couldn't open the Claude desktop app.")

    monkeypatch.setattr(claude, "open_link", broken)
    result = oo("claude", "docs")
    assert result.exit_code == 1
    assert "Couldn't open the Claude desktop app." in result.output


def test_claude_checks_for_the_app_before_touching_the_environment(calls, monkeypatch):
    def missing():
        raise claude.ClaudeError("The Claude desktop app isn't installed.")

    monkeypatch.setattr(claude, "check_installed", missing)
    result = oo("claude", "docs")
    assert result.exit_code == 1
    assert "The Claude desktop app isn't installed." in result.output
    assert calls == []


def _ona_connections(path, *env_ids: str) -> None:
    configs = [{"id": i, "name": f"conn {i}", "sshHost": f"{i}.ona.environment"} for i in env_ids]
    path.write_text(json.dumps({"configs": configs, "trustedHosts": []}))


def _hosts_left(path) -> list[str]:
    return [c["sshHost"] for c in json.loads(path.read_text())["configs"]]


def _names(path) -> dict[str, str]:
    return {c["sshHost"]: c["name"] for c in json.loads(path.read_text())["configs"]}


def _unnamed(path, *hosts: str) -> None:
    configs = [{"id": h, "name": h, "sshHost": h} for h in hosts]
    path.write_text(json.dumps({"configs": configs, "trustedHosts": []}))


def test_claude_names_the_connection_after_the_branch(calls, ssh_configs):
    _unnamed(ssh_configs, "33333333-cccc.ona.environment")
    result = oo("claude", "docs")
    assert "Shows as docs in the app's SSH connections" in result.output
    assert _names(ssh_configs) == {"33333333-cccc.ona.environment": "docs"}
    assert calls[-1][0] == "open"


def test_claude_leaves_the_file_alone_before_the_app_has_connected(calls, ssh_configs):
    result = oo("claude", "docs")
    assert "Shows as" not in result.output
    assert not ssh_configs.exists()
    assert calls[-1][0] == "open"


def test_claude_names_the_connection_the_link_opens(calls, ssh_configs, monkeypatch):
    monkeypatch.setattr(
        claude, "set_up_host", lambda env_id, live_ids, name="": f"{env_id}.claude.oo"
    )
    _unnamed(ssh_configs, "33333333-cccc.ona.environment", "33333333-cccc.claude.oo")
    oo("claude", "docs")
    assert _names(ssh_configs) == {
        "33333333-cccc.ona.environment": "33333333-cccc.ona.environment",
        "33333333-cccc.claude.oo": "docs",
    }
    assert "ssh_host=33333333-cccc.claude.oo" in calls[-1][1]


def test_claude_label_option_renames_it(calls, ssh_configs):
    _ona_connections(ssh_configs, "33333333-cccc")
    oo("claude", "docs", "--label", "Docs refresh")
    assert _names(ssh_configs) == {"33333333-cccc.ona.environment": "Docs refresh"}


def test_claude_keeps_a_name_you_gave_it(calls, ssh_configs):
    _ona_connections(ssh_configs, "33333333-cccc")
    result = oo("claude", "docs")
    assert "Shows as" not in result.output
    assert _names(ssh_configs) == {"33333333-cccc.ona.environment": "conn 33333333-cccc"}


def test_claude_still_opens_when_it_cant_name_the_connection(calls, ssh_configs):
    ssh_configs.write_text("{not json")
    result = oo("claude", "docs")
    assert result.exit_code == 0
    assert "Couldn't name the connection" in result.output
    assert calls[-1][0] == "open"


def test_claude_clean_forgets_deleted_environments(calls, ssh_configs):
    _ona_connections(ssh_configs, "33333333-cccc", "gone-1")
    result = oo("claude", "--clean", "--yes")
    assert result.exit_code == 0
    assert "Removed conn gone-1 from the Claude desktop app" in result.output
    assert _hosts_left(ssh_configs) == ["33333333-cccc.ona.environment"]
    assert calls == []


def test_claude_clean_asks_first(calls, ssh_configs, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    _ona_connections(ssh_configs, "33333333-cccc", "gone-1")
    result = oo("claude", "--clean", input="y\n")
    assert "doesn't list 1 of the Claude desktop app's environments: conn gone-1" in result.output
    assert _hosts_left(ssh_configs) == ["33333333-cccc.ona.environment"]


def test_claude_clean_leaves_them_when_you_say_no(calls, ssh_configs, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    _ona_connections(ssh_configs, "33333333-cccc", "gone-1")
    before = ssh_configs.read_text()
    result = oo("claude", "--clean", input="n\n")
    assert "Cancelled" in result.output
    assert ssh_configs.read_text() == before


def test_claude_clean_wont_remove_without_asking(calls, ssh_configs):
    _ona_connections(ssh_configs, "gone-1")
    before = ssh_configs.read_text()
    result = oo("claude", "--clean")
    assert result.exit_code == 1
    assert "--yes" in result.output
    assert ssh_configs.read_text() == before


def test_claude_clean_tidies_stray_trusted_hosts_without_asking(calls, ssh_configs):
    ssh_configs.write_text(json.dumps({"configs": [], "trustedHosts": ["gone-1.ona.environment"]}))
    result = oo("claude", "--clean")
    assert result.exit_code == 0
    assert json.loads(ssh_configs.read_text())["trustedHosts"] == []


def test_claude_clean_stops_when_the_file_looks_wrong(calls, ssh_configs):
    ssh_configs.write_text(json.dumps({"configs": "nope"}))
    result = oo("claude", "--clean", "--yes")
    assert result.exit_code == 1
    assert "left it alone" in result.output


def test_claude_yes_goes_only_with_clean(calls):
    assert oo("claude", "docs", "--yes").exit_code == 1


def test_claude_clean_says_when_theres_nothing_to_do(calls, ssh_configs):
    _ona_connections(ssh_configs, "33333333-cccc")
    result = oo("claude", "--clean")
    assert "nothing left over" in result.output


def test_claude_clean_takes_no_branch_or_label(calls):
    assert oo("claude", "docs", "--clean").exit_code == 1
    assert oo("claude", "--clean", "--label", "x").exit_code == 1


def test_id_prints_just_the_id(calls):
    result = oo("id", "docs")
    assert result.stdout == "33333333-cccc\n"


def test_start_skips_running_ones(calls):
    oo("start", "feat-10", "docs")
    assert calls == [("environment", "start", "33333333-cccc", "--dont-wait")]


def test_stop_with_no_branch_stops_the_only_running_one(calls):
    oo("stop")
    assert calls == [("environment", "stop", "11111111-aaaa", "--dont-wait")]


def test_stop_skips_stopped_ones(calls):
    result = oo("stop", "docs")
    assert "already stopped" in result.output
    assert calls == []


def test_stop_with_several_running_asks_for_names(calls, envs):
    envs[:] = [Env.from_json(raw_env(f"{i}{i}", f"sam/b{i}", "running")) for i in range(2)]
    assert oo("stop").exit_code == 1
    oo("stop", "--all")
    assert calls == [("environment", "stop", "00", "11", "--dont-wait")]


def test_rm_deletes_without_asking(calls):
    result = oo("rm", "feat-10-2", "docs")
    assert result.exit_code == 0
    assert "Deleting sam/feat-10-2 and sam/docs in the background." in result.output
    assert calls == [("environment", "delete", "22222222-bbbb", "33333333-cccc", "--dont-wait")]


def test_rm_forgets_them_in_the_claude_app_too(calls, ssh_configs):
    _ona_connections(ssh_configs, "33333333-cccc", "11111111-aaaa")
    result = oo("rm", "docs")
    assert "Removed conn 33333333-cccc from the Claude desktop app" in result.output
    assert _hosts_left(ssh_configs) == ["11111111-aaaa.ona.environment"]


def test_rm_still_succeeds_when_the_claude_app_file_is_broken(calls, ssh_configs):
    ssh_configs.write_text("{not json")
    result = oo("rm", "docs")
    assert result.exit_code == 0
    assert "Couldn't tidy up the Claude desktop app." in result.output


TWINS = [
    raw_env("01a0-0000-aaaaaa", "sam/twin", "running", created="2026-09-28T10:00:00Z"),
    raw_env("01a0-0000-bbbbbb", "sam/twin", created="2026-09-29T10:00:00Z"),
]


def test_twins_without_a_terminal_are_an_error_listing_them(calls, envs):
    envs[:] = [Env.from_json(r) for r in TWINS]
    result = oo("rm", "twin")
    assert result.exit_code == 1
    assert "Pass one of the IDs above instead" in result.output
    assert "Created" in result.output
    assert "aaaaaa" in result.output and "bbbbbb" in result.output
    assert calls == []


@pytest.mark.parametrize("command", ["c", "rm", "stop"])
def test_twins_get_asked_about(calls, monkeypatch, envs, command):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    envs[:] = [Env.from_json(r) for r in TWINS]
    result = oo(command, "twin", input="1\n")
    assert '"twin" matches 2 environments' in result.output
    assert "Which one?" in result.output
    assert calls[0][2] == "01a0-0000-aaaaaa"


def test_the_end_of_an_id_is_enough(calls, envs):
    envs[:] = [Env.from_json(r) for r in TWINS]
    oo("c", "bbbbbb")
    assert calls[-1] == ("environment", "ssh", "01a0-0000-bbbbbb")


def test_vague_names_get_asked_about_too(calls, monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    oo("c", "feat", input="2\n")
    assert calls[-1] == ("environment", "ssh", "22222222-bbbb")


def test_new_reuses_your_latest_project_and_class_then_connects(calls):
    result = oo("new", "auth-fix")
    assert "from Widgets" in result.output
    assert "m7i.xlarge in Frankfurt" in result.output
    assert calls == [
        ("environment", "create", "p-1", "--class-id", "c-fr", "--name", "auth-fix"),
        ("environment", "ssh", "99999999-new"),
    ]


def test_new_needs_a_name(calls):
    result = oo("new")
    assert result.exit_code == 2
    assert "Missing argument 'name'" in result.output
    assert calls == []


def made_alongside_another(envs, printed: str):
    """A fake create, during which an `oo new` in another terminal makes a newer environment."""

    def quietly(*args, failed, on_tick=None):
        envs.append(Env.from_json(raw_env("99999999-new", "main", created="2026-10-01T00:00:00Z")))
        envs.append(
            Env.from_json(raw_env("88888888-rival", "main", created="2026-10-02T00:00:00Z"))
        )
        return 3.0, printed

    return quietly


def test_new_connects_to_its_own_environment_when_another_is_made_alongside(
    calls, monkeypatch, envs
):
    monkeypatch.setattr(ona, "quietly", made_alongside_another(envs, "99999999-new\n"))
    oo("new", "x")
    assert calls == [("environment", "ssh", "99999999-new")]


def test_new_wont_guess_between_new_environments(calls, monkeypatch, envs):
    monkeypatch.setattr(ona, "quietly", made_alongside_another(envs, "Environment created\n"))
    result = oo("new", "x")
    assert result.exit_code == 1
    assert "can't tell which environment is yours" in result.output
    assert calls == []


def test_new_takes_project_and_class_from_the_environment(calls, monkeypatch):
    monkeypatch.setenv("OO_PROJECT", "widgets")
    monkeypatch.setenv("OO_CLASS", "tokyo")
    oo("new", "x")
    assert calls[0] == ("environment", "create", "p-1", "--class-id", "c-tk", "--name", "x")


def test_new_complains_about_unknown_classes(calls):
    result = oo("new", "x", "--class", "mars")
    assert result.exit_code == 1
    assert "m7i.xlarge in Tokyo, m7i.xlarge in Frankfurt" in result.output
    assert calls == []


CREATE = ("environment", "create", "p-1", "--class-id", "c-fr", "--name", "auth-fix")
NEW_LINK = (
    "claude://code/new?ssh_host=99999999-new.ona.environment&ssh_folder=%2Fworkspaces%2Fwidgets"
)


@pytest.mark.parametrize(
    "then", [["-t", "code", "-t", "ssh"], ["-t", "ssh,code"], ["--then", "SSH, code"]]
)
def test_new_then_opens_the_editor_before_connecting(calls, monkeypatch, then):
    monkeypatch.delenv("OO_EDITOR", raising=False)
    result = oo("new", "auth-fix", *then)
    assert isinstance(result.exception, Called)
    assert calls == [
        CREATE,
        ("environment", "open", "99999999-new"),
        ("environment", "ssh", "99999999-new"),
    ]


def test_new_then_code_uses_the_editor_you_name(calls, monkeypatch):
    monkeypatch.setenv("OO_EDITOR", "vscode")
    oo("new", "auth-fix", "-t", "code", "-e", "cursor")
    assert calls[-1] == ("environment", "open", "99999999-new", "--editor", "cursor")


def test_new_then_code_uses_oo_editor(calls, monkeypatch):
    monkeypatch.setenv("OO_EDITOR", "cursor")
    result = oo("new", "auth-fix", "-t", "code")
    assert result.exit_code == 0
    assert calls == [CREATE, ("environment", "open", "99999999-new", "--editor", "cursor")]


def test_new_then_does_each_step_once(calls, monkeypatch):
    monkeypatch.delenv("OO_EDITOR", raising=False)
    oo("new", "auth-fix", "-t", "code", "-t", "code,code")
    assert calls == [CREATE, ("environment", "open", "99999999-new")]


def test_new_then_claude_opens_the_desktop_app_and_doesnt_connect(calls):
    result = oo("new", "auth-fix", "-t", "claude")
    assert result.exit_code == 0
    assert "Opening in Claude" in result.output
    assert calls == [CREATE, ("open", NEW_LINK)]


def test_new_then_none_only_creates(calls):
    result = oo("new", "auth-fix", "-t", "none")
    assert result.exit_code == 0
    assert "Created main" in result.output
    assert calls == [CREATE]


@pytest.mark.parametrize(
    ("then", "says"),
    [
        (["-t", "none", "-t", "ssh"], "--then none can't go with other steps."),
        (["-t", "none,code"], "--then none can't go with other steps."),
        (["-t", "vim"], "oo new can't --then vim."),
    ],
)
def test_new_then_refuses_bad_steps_before_creating(calls, then, says):
    result = oo("new", "auth-fix", *then)
    assert result.exit_code == 1
    assert says in result.output
    assert calls == []


def test_new_then_claude_checks_for_the_app_before_creating(calls, monkeypatch):
    def missing():
        raise claude.ClaudeError("The Claude desktop app isn't installed.")

    monkeypatch.setattr(claude, "check_installed", missing)
    result = oo("new", "auth-fix", "-t", "claude")
    assert result.exit_code == 1
    assert "isn't installed" in result.output
    assert calls == []


def test_new_carries_on_when_the_editor_wont_open(calls, monkeypatch):
    monkeypatch.delenv("OO_EDITOR", raising=False)
    fake = ona.quietly

    def quietly(*args, failed, on_tick=None):
        if args[:2] == ("environment", "open"):
            raise OnaError(failed, detail="no editor found")
        return fake(*args, failed=failed, on_tick=on_tick)

    monkeypatch.setattr(ona, "quietly", quietly)
    result = oo("new", "auth-fix", "-t", "code,claude,ssh")
    assert "Ona couldn't open your editor." in result.output
    assert "no editor found" in result.output
    assert calls == [CREATE, ("open", NEW_LINK), ("environment", "ssh", "99999999-new")]


def test_new_carries_on_when_the_claude_app_wont_open(calls, monkeypatch):
    def broken(url):
        raise claude.ClaudeError("Couldn't open the Claude desktop app.")

    monkeypatch.setattr(claude, "open_link", broken)
    result = oo("new", "auth-fix", "-t", "claude", "-t", "ssh")
    assert "Couldn't open the Claude desktop app." in result.output
    assert calls == [CREATE, ("environment", "ssh", "99999999-new")]


CHECK_OUT = (
    "environment",
    "exec",
    "99999999-new",
    "--timeout",
    "600",
    "--working-dir",
    "/workspaces/widgets",
    "--",
    "sh",
    "-c",
    cli.CHECK_OUT,
    "sh",
    "pr-123-branch",
)


def test_new_branch_checks_it_out_even_with_then_none(calls):
    result = oo("new", "auth-fix", "-b", "pr-123-branch", "-t", "none")
    assert result.exit_code == 0
    assert "Checking out" in result.output
    assert calls == [CREATE, CHECK_OUT]


def test_new_branch_checks_out_before_the_then_steps(calls, monkeypatch):
    monkeypatch.delenv("OO_EDITOR", raising=False)
    result = oo("new", "auth-fix", "--branch", "pr-123-branch", "-t", "code,ssh")
    assert isinstance(result.exception, Called)
    assert calls == [
        CREATE,
        CHECK_OUT,
        ("environment", "open", "99999999-new"),
        ("environment", "ssh", "99999999-new"),
    ]


def test_new_carries_on_when_the_checkout_fails(calls, monkeypatch):
    fake = ona.quietly

    def quietly(*args, failed, on_tick=None):
        if args[:2] == ("environment", "exec"):
            calls.append(args)
            raise OnaError(failed, detail="invalid reference: pr-123-branch")
        return fake(*args, failed=failed, on_tick=on_tick)

    monkeypatch.setattr(ona, "quietly", quietly)
    result = oo("new", "auth-fix", "-b", "pr-123-branch")
    assert isinstance(result.exception, Called)
    assert "Ona couldn't check out pr-123-branch." in result.output
    assert "invalid reference" in result.output
    assert calls == [CREATE, CHECK_OUT, ("environment", "ssh", "99999999-new")]


@pytest.mark.parametrize("branch", ["", "-x", "a b"])
def test_new_refuses_bad_branches_before_creating(calls, branch):
    result = oo("new", "auth-fix", "-b", branch)
    assert result.exit_code == 1
    assert "can't be a branch name" in result.output
    assert calls == []


def test_complete_then_offers_the_steps_after_a_comma():
    assert cli._complete_then("c") == ["code", "claude"]
    assert cli._complete_then("code,s") == ["code,ssh"]


def test_start_and_stop_say_what_they_did(calls):
    assert "Starting sam/docs in the background." in oo("start", "docs").output
    assert "Stopped sam/feat-10 in 3s" in oo("stop", "feat-10", "--wait").output


def test_cp_copies_into_the_repo(calls):
    result = oo("cp", "notes.md", "dist", "feat-10:")
    assert "Copying to sam/feat-10" in result.output
    assert calls == [
        (
            "scp",
            "-O",
            "-r",
            "--",
            "notes.md",
            "dist",
            "11111111-aaaa.ona.environment:/workspaces/widgets",
        )
    ]


def test_cp_copies_out_starting_stopped_ones(calls):
    result = oo("cp", "docs:out/a.csv", "docs:/tmp/b.csv", ".")
    assert "Copying from sam/docs · started in 3s" in result.output
    host = "33333333-cccc.ona.environment"
    assert calls == [
        ("environment", "start", "33333333-cccc"),
        (
            "scp",
            "-O",
            "-r",
            "--",
            f"{host}:/workspaces/widgets/out/a.csv",
            f"{host}:/tmp/b.csv",
            ".",
        ),
    ]


def test_cp_with_no_branch_uses_the_only_running_one(calls):
    oo("cp", ":~/.bashrc", "./odd:name")
    assert calls == [
        ("scp", "-O", "-r", "--", "11111111-aaaa.ona.environment:~/.bashrc", "./odd:name")
    ]


def test_cp_sets_up_ssh_the_first_time(calls, monkeypatch):
    answers = iter([False, True])
    monkeypatch.setattr(ssh, "configured", lambda env_id: next(answers))
    result = oo("cp", "notes.md", "feat-10:")
    assert "Set up SSH for Ona environments" in result.output
    assert calls[0] == ("environment", "ssh-config")
    assert calls[1][0] == "scp"


@pytest.mark.parametrize(
    "args",
    [
        ["notes.md"],
        ["notes.md", "elsewhere.md"],
        ["feat-10:a", "docs:"],
        ["feat-10:a", "local.md", "."],
        ["feat-10:a", "docs:b", "."],
    ],
)
def test_cp_refuses_what_it_cant_do(calls, args):
    result = oo("cp", *args)
    assert result.exit_code == 1
    assert calls == []


def test_rm_wait_sees_a_delete_through_when_ona_gives_up_at_the_stop(calls, monkeypatch, envs):
    def stops(*args, failed, on_tick=None):
        calls.append(args)
        envs.pop(0)
        raise OnaError(failed, detail="environment has unexpectedly stopped")

    monkeypatch.setattr(ona, "quietly", stops)
    result = oo("rm", "feat-10", "--wait")
    assert result.exit_code == 0
    assert "Deleted sam/feat-10" in result.output
    assert calls == [("environment", "delete", "11111111-aaaa")]


def test_rm_wait_still_fails_on_real_errors(calls, monkeypatch):
    def broken(*args, failed, on_tick=None):
        raise OnaError(failed, detail="permission denied")

    monkeypatch.setattr(ona, "quietly", broken)
    result = oo("rm", "feat-10", "--wait")
    assert result.exit_code == 1
    assert "permission denied" in result.output


def named(envs, *names):
    """Give the environments in `envs` these names, in order."""
    raws = [
        dict(r, metadata={**r["metadata"], "name": n}) for r, n in zip(RAW, names, strict=False)
    ]
    envs[: len(raws)] = [Env.from_json(r) for r in raws]


def test_claude_names_the_host_after_the_environment(calls, monkeypatch, envs):
    monkeypatch.setattr(claude, "set_up_host", own_host)
    named(envs, "login-fix")
    oo("claude")
    assert calls[0][1].startswith("claude://code/new?ssh_host=login-fix.claude.oo&")


@pytest.mark.parametrize("names", [("login fix",), ("login-fix", "Login-Fix")])
def test_claude_goes_by_id_when_the_name_wont_do_as_a_host(calls, monkeypatch, envs, names):
    monkeypatch.setattr(claude, "set_up_host", own_host)
    named(envs, *names)
    oo("claude", "feat-10")
    assert calls[0][1].startswith("claude://code/new?ssh_host=11111111-aaaa.claude.oo&")


@pytest.mark.parametrize("name", ["login fix", "-x", "x-", "a_b", "a.b", "a" * 57])
def test_new_refuses_names_that_wont_do_as_a_host(calls, name):
    result = oo("new", "--", name)
    assert result.exit_code == 1
    assert "Use up to 56 letters, digits and hyphens" in result.output
    assert calls == []


def test_new_adds_the_end_of_the_id_when_the_name_is_taken(calls, envs):
    named(envs, "Auth-Fix")
    result = oo("new", "auth-fix")
    assert calls[1:] == [
        ("environment", "update", "99999999-new", "--name", "auth-fix-99-new"),
        ("environment", "ssh", "99999999-new"),
    ]
    assert "as auth-fix-99-new" in result.output


def test_new_doesnt_count_names_of_environments_being_deleted(calls, envs):
    envs.append(Env.from_json(raw_env("44444444-dddd", "main", "deleting", name="auth-fix")))
    oo("new", "auth-fix")
    assert [c[1] for c in calls] == ["create", "ssh"]


def test_new_still_connects_when_the_rename_fails(calls, monkeypatch, envs):
    named(envs, "auth-fix")
    fake = ona.quietly

    def quietly(*args, failed, on_tick=None):
        if args[:2] == ("environment", "update"):
            raise OnaError(failed, detail="permission denied")
        return fake(*args, failed=failed, on_tick=on_tick)

    monkeypatch.setattr(ona, "quietly", quietly)
    result = oo("new", "auth-fix")
    assert "Ona couldn't rename the environment. It keeps the name" in result.output
    assert calls[-1] == ("environment", "ssh", "99999999-new")


def test_new_then_claude_names_the_host_after_the_final_name(calls, monkeypatch, envs):
    named(envs, "auth-fix")
    hosts = []

    def set_up_host(env_id, live_ids, name=""):
        hosts.append((env_id in live_ids, name))
        return ssh.host(env_id)

    monkeypatch.setattr(claude, "set_up_host", set_up_host)
    oo("new", "auth-fix", "-t", "claude")
    assert hosts == [(True, "auth-fix-99-new")]
