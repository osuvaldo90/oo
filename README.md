<h1 align="center">oo</h1>

<p align="center">
  <b>Hop into your <a href="https://ona.com">Ona</a> environments by branch name.</b><br>
  No more copying UUIDs out of <code>ona environment list</code>: run <code>oo c login-fix</code> and you're in.
</p>

<p align="center">
  <a href="https://github.com/adamtrain/oo/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/adamtrain/oo/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python 3.13+" src="https://img.shields.io/badge/python-3.13%2B-3776ab?logo=python&logoColor=white">
  <a href="https://github.com/astral-sh/uv"><img alt="uv" src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json"></a>
  <a href="https://github.com/astral-sh/ruff"><img alt="Ruff" src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json"></a>
  <a href="LICENSE"><img alt="License: CC0-1.0" src="https://img.shields.io/badge/license-CC0--1.0-lightgrey"></a>
</p>

```console
$ oo

Branch               Status     Active   PR               Changes
adam/login-fix       ● running  4m ago   #1234 open       unpushed
adam/billing-export  ○ stopped  2d ago   #1198 approved   3 uncommitted
adam/old-spike       ○ stopped  3w ago   #1102 merged

$ oo c login
→ Connecting to adam/login-fix · running
```

## Why oo

- **Branches, not UUIDs.** Name an environment by its branch, any unique part of it, its name or
  its ID. Leave your personal branch prefix off.
- **One glance at everything.** The list shows what's running, when you last used each
  environment, its pull request, and any work that isn't on GitHub yet.
- **Knows what you mean.** `oo c`, `oo code` and `oo claude` with no branch use the one running
  environment, or let you pick from a list. Typos get a "did you mean".
- **A fresh environment in one command.** `oo new NAME` creates an environment the way you did
  last time and connects you to it, or opens it in your editor or the Claude app.
- **Doesn't make you wait.** Starting, stopping and deleting return straight away while Ona
  carries on in the background.
- **Quiet.** ona's own output stays behind a spinner with a timer. If something goes wrong, you
  see ona's error and any fixes it suggests.
- **Just a wrapper.** Everything goes through the `ona` and `gh` CLIs you already have set up.
  oo has no config file of its own. The one exception is `oo claude`, which writes SSH host
  entries; see [docs/claude-desktop.md](docs/claude-desktop.md) for what it changes and how to
  undo it.

## Install

You'll need [uv](https://docs.astral.sh/uv/) and the [Ona CLI](https://ona.com/docs), logged in.
The [GitHub CLI](https://cli.github.com) is optional; with it, the list shows each branch's pull
request.

```sh
uv tool install git+https://github.com/adamtrain/oo
```

That puts `oo` on your `PATH`. To hack on it, clone the repo and install it in editable mode, so
your changes take effect right away:

```sh
git clone https://github.com/adamtrain/oo && cd oo
uv tool install --editable .
```

If your branches share a prefix, tell oo in your shell profile so you can leave it off:

```sh
export OO_BRANCH_PREFIX=adam/
```

For tab completion of branch names, run `oo --install-completion` once.

## Usage

```sh
oo                            # list your environments
oo c login-fix                # SSH into the one on adam/login-fix, starting it if needed
oo c                          # …the only running one, or pick from a list
oo c login -- git status      # run one command instead of opening a shell
oo code login -e cursor       # open it in Cursor (or set $OO_EDITOR)
oo claude login               # open a Claude Code session on it in the Claude desktop app
oo claude login --label Login # …and call it Login in the app's SSH picker
oo claude --clean             # take deleted environments out of the app's SSH connections
oo cp notes.md dist login:    # copy files and folders into the repo on adam/login-fix
oo cp login:out/report.csv .  # …or out of it
oo new login-fix              # create an environment named login-fix and SSH into it
oo new login-fix -t code,ssh  # …open it in your editor, then SSH into it
oo new login-fix -t claude    # …open it in the Claude desktop app instead
oo new login-fix -t none      # …just create it
oo new pr-9 -b pr-9 -t code   # …check out branch pr-9, then open your editor
oo start login billing        # warm some up in the background
oo stop                       # stop the only running one (--all for every one)
oo rm old-spike               # delete one
ona env logs $(oo id login)   # hand an ID to any other ona command
```

### Naming an environment

Wherever oo asks for a branch, you can give:

1. An environment's ID or name
2. The full branch, like `adam/login-fix`, or the branch without `$OO_BRANCH_PREFIX`, like
   `login-fix`
3. The part after any other prefix, like `their-branch` for `sam/their-branch`
4. Any part of a branch or environment name, like `login`
5. The start or end of an ID

oo tries these in order and stops at the first that matches anything, so `login` means
`adam/login` even when `adam/login-fix` exists too. If a name still matches more than one
environment, oo lists them and asks which one you mean. In a script, with nobody to ask, it lists
them and stops instead.

When you have more than one environment on the same branch, lists also show when each was
created and the last 6 characters of its ID. Type those characters to name one directly, like
`oo c 0ffee1`.

### Copying files

`oo cp` works like `scp`, with a branch where the host would go. The last path is where to copy
to, and paths in an environment are written `branch:path`:

```sh
oo cp notes.md login:              # into the repo's top folder
oo cp notes.md login:docs/         # into a folder in the repo
oo cp login:out/report.csv .       # out of the repo, into this folder
oo cp a.py dist/ login:tmp/        # several files and folders at once
oo cp :out.csv .                   # the only running environment, or pick from a list
```

Relative paths in an environment start in the repo, where `oo c` puts you, rather than in the
home folder. `~/` and `/` work as usual, and so do wildcards like `login:logs/*.log`. Folders copy without needing `-r`. A path starting with
`./`, `/` or `~` is always on your machine, so `./odd:name` names a local file with a colon in it.
Copies go one way at a time, between your machine and one environment.

### Reading the list

| | |
| --- | --- |
| **Status** | `●` running · `◐` starting · `○` stopped. Running environments come first, then the rest by when you last used them. |
| **Name** | Only when an environment on a branch has a name, like one made with `oo new -n`. An environment with no branch shows its name under Branch instead. |
| **ID**, **Created** | Only when two environments share a branch: the end of each one's ID, and when it was created. |
| **Active** | When Ona last saw activity in the environment. |
| **PR** | The newest pull request from the branch: draft, open, approved, changes req., merged or closed. The number links to it. |
| **Changes** | Uncommitted files, and `unpushed` when the environment's latest commit isn't on GitHub. |

Ona updates git details while an environment runs, so a stopped environment shows how things
were when it stopped.

### Creating environments

`oo new` needs a project to create from and an environment class (a machine in a region) to run
on. Each defaults to whatever your newest environment used, so it usually does what you did last
time. To choose, use the options or set them in your shell profile:

| | Option | Variable | Takes |
| --- | --- | --- | --- |
| **Project** | `-p, --project` | `OO_PROJECT` | An ID or name from `ona project list` |
| **Class** | `-c, --class` | `OO_CLASS` | An ID from `ona environment list-classes`, or words that pick one out, like `frankfurt` or `tokyo m7i` |

Class words prefer the classes the project offers, the same choices as Ona's region picker. Once
Ona has started the environment, oo connects to it. It starts on the project's default branch.

### Keeping the list open

`oo ui` keeps the list on screen all day and acts on it with one key each. It needs oo's `ui`
extra, which brings in [Textual](https://textual.textualize.io):

```sh
uv tool install 'oo[ui] @ git+https://github.com/adamtrain/oo'   # or, from a checkout:
uv tool install --editable '.[ui]'
```

```console
$ oo ui

 oo  ·  8 environments, 2 running  ·  refreshed 4s ago  ·  ⏎ opens ssh in a new tmux window
   Branch               Status                 Active     Changes
 ▸ adam/login-fix       ● running              4m ago
   adam/billing-export  ◐ starting · 12s       2d ago     3 uncommitted
 ✓ adam/old-spike       ○ stopped              3w ago     1 uncommitted
   adam/scratch         ✕ deleting · 3s        1w ago
 ──────────────────────────────────────────────────────────────────────────────────────
   login-fix  ·  https://github.com/acme/widgets.git  ·  created Nov 2 10:14  ·  id …0ffee1
   no uncommitted changes
 ──────────────────────────────────────────────────────────────────────────────────────
 ✓ Stopping adam/old-spike  1 uncommitted file stays on disk
 ⏎ ssh  o code  a claude  n new  s start  D delete  ␣ mark  i info  L log  / filter  ? help  q quit
```

| Key | |
| --- | --- |
| `⏎` `c` | SSH into the environment. A stopped one is started first, and you're asked before the session opens. `C` always opens it here. |
| `o` `O` | Open it in your editor (`$OO_EDITOR`, else Ona's default), or in one you pick. |
| `a` | Open a Claude Code session on it in the Claude desktop app. The first time on a machine, oo shows what it sets up and asks. |
| `n` | Create an environment: name, project and class default as for `oo new`, then connect to it, open it, or just leave it. |
| `s` `x` | Start, stop. Ona carries on in the background; the list shows `starting` or `stopping` right away. |
| `D` | Delete, after confirming. Stopped and clean environments take a `y`. One that's running or has uncommitted changes wants its name typed; a batch with one wants the word `delete`. |
| `␣` `*` | Mark the row, or every row shown. `s`, `x` and `D` then act on all marked rows in one `ona` call. |
| `/` | Filter rows by any part of a name, as `oo c` takes it, or by a phase like `stopped`. |
| `i` `L` `y` | Toggle the details pane, toggle the activity log, copy the environment's ID. |
| `!` | Run `ona login` here when Ona says your session expired. |

The list refreshes every 15 seconds, every 3 while something is starting, stopping or being
created, and every minute while the terminal is in the background. Nothing takes over the
terminal without a keypress, and an environment with work in flight refuses a second one.

Where SSH sessions open depends on the terminal `oo ui` runs in. In cmux, a session becomes a
cmux workspace mirroring a tmux session on the environment. In tmux, it opens in a new window.
In iTerm2, WezTerm, kitty and Terminal.app, it opens in a new tab or window. Anywhere else,
`oo ui` steps aside, runs ssh in its own terminal, and comes back when the session ends. Set
`OO_CONNECT` to `here`, `cmux`, `tmux` or `tab` to pick one instead of `auto`.

## Settings

| Variable | |
| --- | --- |
| `OO_BRANCH_PREFIX` | Your branch prefix, e.g. `adam/`. Dimmed in the list and optional when typing. |
| `OO_EDITOR` | Default editor for `oo code`, `oo new -t code` and `o` in `oo ui`: an alias from `ona environment list-editors`, like `cursor` |
| `OO_PROJECT` | Default project for `oo new` |
| `OO_CLASS` | Default environment class for `oo new` |
| `OO_CONNECT` | Where `oo ui` opens SSH sessions: `auto` (the default), `here`, `cmux`, `tmux` or `tab` |

## How it works

oo reads your environments with `ona environment list --format json`, then passes the matching
ID to `ona environment ssh`, `open`, `start`, `stop` or `delete`. It runs these with their output
hidden behind a spinner, and if one fails, it shows the `Error:` and "Possible resolutions" ona
printed. `oo c` and `oo code` start a stopped environment that way first, then replace themselves
with the ona process, so SSH gets your terminal to itself.

Pull requests and push status come from a single GitHub GraphQL query per repository, made
through `gh api`. It asks for each branch's newest pull request and whether each environment's
latest commit exists on GitHub. If `gh` isn't installed, oo skips that column. If the query
fails, oo says so and lists everything else.

`oo cp` hands the copy to `scp`, using the `<id>.ona.environment` host names that
`ona environment ssh-config` sets up. It uses scp's original protocol (`-O`) rather than SFTP,
because Ona's SFTP server would leave the files owned by root. If ssh doesn't know those names yet, oo runs
`ona environment ssh-config` once and tells you it did.

`oo claude` sets up SSH the same way, then opens a `claude://code/new?ssh_host=…&ssh_folder=…`
link. That starts a new Claude Code session in the [Claude desktop app](https://claude.ai/download),
connected over SSH to the environment and starting in its repo. The app can't install its helper
over Ona's SFTP server, so `oo claude` sets up a second SSH host for each environment, with its own
key. [docs/claude-desktop.md](docs/claude-desktop.md) explains why, the security trade-offs, and how
to clean up.

The app adds a connection to its SSH picker the first time it opens an environment, called by
its long host name. From then on, `oo claude` names it after the environment's branch (or its
Ona name, if it has no branch), so you see `login-fix` there instead. A name you gave it in the
app stays. To choose one, pass `--label`.

The app remembers each environment it has connected to in its SSH picker. `oo rm` takes the
ones it deletes out of that list, and `oo claude --clean` takes out any left from environments
deleted some other way, after showing you which and asking (`--yes` skips that). Both edit the
app's `ssh_configs.json`, and only touch connections to environments (`*.claude.oo` or
`*.ona.environment`) that no longer exist. Stopped environments stay, with any names you gave
them. If the app is open, quit and reopen it to see the list update.

`oo new` notes which environments exist, runs `ona environment create`, and connects to the one
that wasn't there before. With `-b BRANCH`, it then runs `ona environment exec` to fetch that
branch and `git switch` to it before the `--then` steps. If that fails, oo warns and carries on.

## Development

```sh
uv sync                         # set up the environment
uv run pytest                   # run the tests
uv run ruff check . && uv run ruff format .
uv run ty check                 # type-check
```

The tests fake both CLIs, so they run without Ona or GitHub.

```
src/oo/
├── cli.py       # the commands
├── envs.py      # environments, and finding one from what you typed
├── projects.py  # choosing a project and environment class for `oo new`
├── ona.py       # runs the ona CLI
├── github.py    # asks GitHub about branches through gh
├── ssh.py       # copies files with scp, for `oo cp`
├── claude.py    # hands an environment to the Claude desktop app, for `oo claude`
├── render.py    # everything you see
└── ui/          # `oo ui`: the screen (app.py), its dialogs (screens.py), what it asks Ona
                 # for (ops.py), where ssh opens (handoff.py), and what it tracks (model.py)
docs/
└── claude-desktop.md  # why `oo claude` needs its SSH workaround, and how to undo it
```

## License

[CC0 1.0](LICENSE). oo is dedicated to the public domain.

oo is an independent project and isn't affiliated with or endorsed by Ona.
