# `oo claude` and the SSH workaround

`oo claude <name>` opens a Claude Code session on an Ona environment in the
[Claude desktop app](https://claude.ai/download). The app connects over SSH and installs a small
helper server on the environment. On Ona environments that install failed until oo started
using a second SSH server on the box. This document explains why, what oo changes on your
machine and on the environment, what that exposes, and how to undo it.

## Why it's needed

The app uploads its files over SFTP. On Ona, the SFTP server is installed setuid root:

```
-rwsr-sr-x root root /usr/local/gitpod/openssh/libexec/sftp-server
```

sshd runs that server for every SFTP session, so every file the app uploads is created as
`root:root`. Two uploads then fail:

- The helper server binary (`~/.claude/remote/srv/<version>/server`) lands as `root:root 644`, so
  the box user can't run it. The app reports `NO_INSTALL_RESULT`.
- The connection token (`~/.claude/remote/run/<client>/token.*`) is new on every connect and lands
  as `root:root 600`, so the server can't read it. The app reports "Couldn't start the remote
  server".

The app has no option to upload without SFTP, and copying files in advance doesn't help because
the token changes on every connect. The root cause is the setuid bit on Ona's SFTP server, which
oo doesn't touch. Removing it or chowning files with sudo is a security change that isn't ours to
make.

`oo cp` doesn't hit this. It runs `scp -O`, the original SCP protocol, which runs as your login
user over an ordinary SSH session and never uses the SFTP server.

## The workaround

Each Ona environment already runs its own standard OpenSSH server on port 2222 (not setuid), with
the normal SFTP server. Files written through it are owned by the box user. `oo claude` points the
app at that server, tunnelled through Ona's SSH connection:

1. Creates a dedicated key, `~/.ssh/oo_claude` (ed25519, no passphrase), the first time it runs.
   Your other keys aren't touched.
2. Over the normal Ona connection, finds the box user (`id -un`) and checks that something is
   listening on 127.0.0.1:2222. If nothing is, it falls back to Ona's own host and prints why.
3. Appends `~/.ssh/oo_claude.pub` to the box user's `~/.ssh/authorized_keys`, only if it's
   missing. It keeps `~/.ssh` at 700 and `authorized_keys` at 600, which sshd requires.
4. Writes a host entry to `~/.ssh/oo/config`, and makes sure `~/.ssh/config` includes that file.
   The entry for an environment `<id>` is:

   ```
   Host <id>.claude.oo
     HostName localhost
     Port 2222
     User <box user>
     IdentityFile ~/.ssh/oo_claude
     IdentitiesOnly yes
     HostKeyAlias <id>.claude.oo
     ProxyJump <id>.ona.environment
     StrictHostKeyChecking accept-new
     # last used 2026-10-15
   ```

5. Opens the app with `ssh_host=<id>.claude.oo`.

Host-key checking stays on. oo never sets `StrictHostKeyChecking no` or
`UserKnownHostsFile /dev/null`. The first connection records the box's key with `accept-new`,
and later connections must match it.

### Changes to `~/.ssh/config`

The first time, oo adds this line to the top of `~/.ssh/config`, above any `Host` block, because
ssh only reads an `Include` before the first `Host`:

```
Include oo/config
```

Before changing the file it saves a copy as `~/.ssh/config.oo-backup`, and it never overwrites an
existing backup. If `~/.ssh/config` doesn't exist, oo creates it with mode 600.

### Housekeeping

Each run rewrites `~/.ssh/oo/config`. It keeps entries whose `# last used` date is within the
last 30 days and whose environment still exists, and it drops the rest. Entries without a date
count as unused and are dropped. The current environment is always written with today's date.

The app keeps its own list of SSH connections, which this doesn't change. `oo rm` and
`oo claude --clean` take deleted environments out of that list.

That list is the app's `ssh_configs.json`, a file of the app's own rather than a documented
format, so a new version of the app could change it. oo only removes or names entries the app
made for environment hosts, never adds any, keeps every other part of the file as it was, and
leaves the file alone if it isn't laid out the way oo expects. `--clean` counts an environment as deleted when Ona doesn't list it, and
Ona only lists the environments in your current context, so it shows you which it would remove
and asks first.

## What this exposes

This is the part to understand before using it.

- **A second login path to the box.** Anyone who can use `~/.ssh/oo_claude` on your Mac can log in
  as the box user through port 2222. To get there they also need to get through Ona's SSH
  connection, which requires your Ona credentials, so the key alone isn't enough. The access is
  the same user you already get through `oo c`. It doesn't add privileges, but it's a second way
  in.
- **An unencrypted private key on your Mac.** `oo_claude` has no passphrase, because the app
  can't prompt for one. Its security depends on your user account and disk encryption.
- **The setuid SFTP server is still there.** This workaround routes around it for the app. It
  doesn't remove it, so any other tool that uses Ona's SFTP still writes root-owned files.
- **Your `~/.ssh/config` is edited.** oo adds one line and keeps a backup, but any tool that
  rewrites that file may drop the line.

The fix that actually removes the problem belongs to Ona: an SFTP server that writes files as the
connecting user. Until then, this workaround is what lets the app work.

## Cleaning up

To stop using this and remove everything oo set up, do these in order.

1. **Stop running `oo claude`.** Nothing else depends on the host entries, so the steps below
   can be done in any order once you've stopped.
2. **Remove the key from each environment it was added to.** The key is the contents of
   `~/.ssh/oo_claude.pub`. Check the matching lines first, then delete them:

   ```sh
   ssh <id>.ona.environment "grep -F ' oo' ~/.ssh/authorized_keys"
   ssh <id>.ona.environment "sed -i '/ oo$/d' ~/.ssh/authorized_keys"
   ```

   The first command shows the lines that match; check they're only the `oo_claude` key before
   running the second. Keep the other lines.
3. **Delete the local key:**

   ```sh
   rm ~/.ssh/oo_claude ~/.ssh/oo_claude.pub
   ```

4. **Remove the host entries and their host keys:**

   ```sh
   rm -r ~/.ssh/oo
   ssh-keygen -R <id>.claude.oo     # once per environment you connected to
   ```

5. **Remove the `Include` line** from `~/.ssh/config`. Either delete the line, or restore the
   backup if it's still the only change you've made:

   ```sh
   mv ~/.ssh/config.oo-backup ~/.ssh/config
   ```

   Check the restored file first. Restoring it discards any other edits made since the backup.
6. **Delete the backup** once you're happy: `rm ~/.ssh/config.oo-backup`.

The box's own sshd on port 2222 is part of Ona's environment image, not something oo installed, so
these steps don't remove it.

## Testing it

On a fresh environment with no EFS home link (`ls -ld ~/.claude` should be a plain folder, and
`/efs-home` shouldn't exist), `oo claude <name>` should connect. The app's log is
`~/Library/Logs/Claude/ssh.log`. Success shows "Connected to remote server". Failure shows
`NO_INSTALL_RESULT` or "Couldn't start the remote server". On the box,
`find ~/.claude/remote -user root` should print nothing.
