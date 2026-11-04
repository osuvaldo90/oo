<h1 align="center">oo</h1>

<p align="center">
  <b>Hop into your <a href="https://ona.com">Ona</a> environments by name.</b><br>
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

Name            Status     Active   Changes
login-fix       ● running  4m ago
billing-export  ○ stopped  2d ago   3 uncommitted
old-spike       ○ stopped  3w ago

$ oo c login
→ Connecting to login-fix · running
```

## Why oo

- **Names, not UUIDs.** Name an environment by its Ona name, any unique part of it, or its ID.
  `oo new login-fix` gives a new environment its name.
- **One glance at everything.** The list shows what's running, when you last used each
  environment, and any uncommitted work.
- **Knows what you mean.** `oo c`, `oo code` and `oo claude` with no name use the one running
  environment, or let you pick from a list. Typos get a "did you mean".
- **A fresh environment in one command.** `oo new NAME` creates an environment the way you did
  last time and connects you to it, or opens it in your editor or the Claude app.
- **Doesn't make you wait.** Starting, stopping and deleting return straight away while Ona
  carries on in the background.
- **Quiet.** ona's own output stays behind a spinner with a timer. If something goes wrong, you
  see ona's error and any fixes it suggests.
- **Just a wrapper.** Everything goes through the `ona` CLI you already have set up.
  oo has no config file of its own. The one exception is `oo claude`, which writes SSH host
  entries; see [docs/claude-desktop.md](docs/claude-desktop.md) for what it changes and how to
  undo it.

## Install

You'll need [uv](https://docs.astral.sh/uv/) and the [Ona CLI](https://ona.com/docs), logged in.

```sh
uv tool install git+https://github.com/adamtrain/oo
```

That puts `oo` on your `PATH`. To hack on it, clone the repo and install it in editable mode, so
your changes take effect right away:

```sh
git clone https://github.com/adamtrain/oo && cd oo
uv tool install --editable .
```

For tab completion of environment names, run `oo --install-completion` once.

## Usage

```sh
oo                            # list your environments
oo c login-fix                # SSH into the one named login-fix, starting it if needed
oo c                          # …the only running one, or pick from a list
oo c login -- git status      # run one command instead of opening a shell
oo code login -e cursor       # open it in Cursor (or set $OO_EDITOR)
oo claude login               # open a Claude Code session on it in the Claude desktop app
oo claude login --label Login # …and call it Login in the app's SSH picker
oo claude --clean             # take deleted environments out of the app's SSH connections
oo cp notes.md dist login:    # copy files and folders into the repo on login-fix
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

### In cmux

In a [cmux](https://cmux.dev) terminal, `oo c`, `oo ssh` and `oo new` open the environment as a
cmux workspace instead of SSH in the current tab (`oo new` only when `ssh` is one of its
`--then` steps, which it is by default). oo makes a tmux session named after the
environment, starting in its repo, and runs `cmux ssh-tmux` to mirror it. Each tmux window
becomes a cmux tab. This needs cmux's "Remote tmux" beta setting. `oo c NAME -- COMMAND` still
runs the command over plain SSH.

oo also starts an idle reaper in tmux. It detaches clients with no typing for an hour, so an idle
environment can stop. Set `OO_IDLE_TIMEOUT` to change how many seconds that takes. The `oc` shell
function shares the same reaper.

### Naming an environment

Wherever oo asks for an environment, you can give:

1. Its whole name or its ID
2. The start of its name, like `login` for `login-fix`
3. Any part of its name, like `fix`
4. The start or end of its ID

Names match without regard to case. oo tries these in order and stops at the first that matches
anything, so `login` means `login` even when `login-fix` exists too. If a name still matches more
than one environment, oo lists them and asks which one you mean. In a script, with nobody to ask,
it lists them and stops instead.

An environment with no name, such as one made outside oo, shows the last 6 characters of its ID
instead. When more than one environment has the same name, lists also show when each was created
and the last 6 characters of its ID. Type those characters to name one directly, like
`oo c 0ffee1`.

### Copying files

`oo cp` works like `scp`, with an environment name where the host would go. The last path is
where to copy to, and paths in an environment are written `name:path`:

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
| **ID**, **Created** | Only when two environments share a name: the end of each one's ID, and when it was created. |
| **Active** | When Ona last saw activity in the environment. |
| **Changes** | Uncommitted files. |

Ona updates git details while an environment runs, so a stopped environment shows how things
were when it stopped.

### Creating environments

`oo new NAME` gives the environment that name: letters, digits and hyphens. If another
environment has it, oo adds the end of the new one's ID, like `login-fix-0ffee1`.

It also needs a project to create from and an environment class (a machine in a region) to run
on. Each defaults to whatever your newest environment used, so it usually does what you did last
time. To choose, use the options or set them in your shell profile:

| | Option | Variable | Takes |
| --- | --- | --- | --- |
| **Project** | `-p, --project` | `OO_PROJECT` | An ID or name from `ona project list` |
| **Class** | `-c, --class` | `OO_CLASS` | An ID from `ona environment list-classes`, or words that pick one out, like `frankfurt` or `tokyo m7i` |

Class words prefer the classes the project offers, the same choices as Ona's region picker. Once
Ona has started the environment, oo connects to it. It starts on the project's default branch.

## Settings

| Variable | |
| --- | --- |
| `OO_EDITOR` | Default editor for `oo code` and `oo new -t code`: an alias from `ona environment list-editors`, like `cursor` |
| `OO_PROJECT` | Default project for `oo new` |
| `OO_CLASS` | Default environment class for `oo new` |
| `OO_IDLE_TIMEOUT` | Seconds without typing before cmux's tmux clients detach. Defaults to 3600 (`ONA_IDLE_TIMEOUT` works too) |

## How it works

oo reads your environments with `ona environment list --format json`, then passes the matching
ID to `ona environment ssh`, `open`, `start`, `stop` or `delete`. It runs these with their output
hidden behind a spinner, and if one fails, it shows the `Error:` and "Possible resolutions" ona
printed. `oo c` and `oo code` start a stopped environment that way first, then replace themselves
with the ona process, so SSH gets your terminal to itself.

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
its long host name. From then on, `oo claude` names it after the environment's Ona name (or
the end of its ID, if it has none), so you see `login-fix` there instead. A name you gave it in
the app stays. To choose one, pass `--label`.

The app remembers each environment it has connected to in its SSH picker. `oo rm` takes the
ones it deletes out of that list, and `oo claude --clean` takes out any left from environments
deleted some other way, after showing you which and asking (`--yes` skips that). Both edit the
app's `ssh_configs.json`, and only touch connections to environments (`*.claude.oo` or
`*.ona.environment`) that no longer exist. Stopped environments stay, with any names you gave
them. If the app is open, quit and reopen it to see the list update.

`oo new` notes which environments exist, runs `ona environment create --name`, and connects to
the one that wasn't there before. With `-b BRANCH`, it then runs `ona environment exec` to
fetch that branch and `git switch` to it before the `--then` steps. If that fails, oo warns and
carries on.

## Development

```sh
uv sync                         # set up the environment
uv run pytest                   # run the tests
uv run ruff check . && uv run ruff format .
uv run ty check                 # type-check
```

The tests fake the ona CLI, so they run without Ona.

```
src/oo/
├── cli.py       # the commands
├── envs.py      # environments, and finding one from what you typed
├── projects.py  # choosing a project and environment class for `oo new`
├── ona.py       # runs the ona CLI
├── ssh.py       # copies files with scp, for `oo cp`
├── cmux.py      # mirrors an environment's tmux session as a cmux workspace
├── claude.py    # hands an environment to the Claude desktop app, for `oo claude`
└── render.py    # everything you see
docs/
└── claude-desktop.md  # why `oo claude` needs its SSH workaround, and how to undo it
```

## License

[CC0 1.0](LICENSE). oo is dedicated to the public domain.

oo is an independent project and isn't affiliated with or endorsed by Ona.
