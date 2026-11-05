# winsible — Ansible with a Windows control node

Upstream ansible-core refuses to run on Windows. winsible is a small layer
that patches ansible-core while it is being imported, so the stock `ansible`,
`ansible-playbook`, `ansible-vault`, `ansible-galaxy`, `ansible-doc` and
`ansible-inventory` commands run natively on Windows with no WSL, Cygwin or Docker.
ansible-core's own files are never modified, so you can still upgrade it with pip.

Tested on Windows 11 with Python 3.12, using ansible-core **2.21.6** and **2.16.20**,
against RHEL 8.10 (Python 3.6) over SSH.

## Install

```powershell
.\install.ps1                                   # .venv, latest ansible-core
.\install.ps1 -Venv .venv216 -AnsibleVersion '2.16.*'
```

Then use `.venv\Scripts\ansible-playbook.exe …`, or activate the venv.

**Which ansible-core version?** Each core release supports a different range of
*target* Python versions (this is an upstream rule):

| ansible-core | target Python |
|---|---|
| 2.16 | 2.7, 3.6+ (RHEL 7/8 system Python works) |
| 2.18+ | 3.8+ |
| 2.21 | 3.9+ |

Servers running RHEL 8 with the stock `/usr/libexec/platform-python` (3.6) need 2.16.

## What winsible changes (all in `winsible/`)

| Problem on Windows | Fix |
|---|---|
| Workers are `fork()`ed processes | Workers run as threads (`_threadproc.py`). Each one gets private copies of the host, task, play context, vars and loader, matching what a forked child had |
| `fcntl`, `pwd`, `grp`, `termios`, `tty`, `resource`, `pty` missing | Windows stand-ins (`_stubs.py`), served **only** to imports coming from `ansible.*` |
| Code assumes `/` paths (`split(os.sep)`, regexes, `startswith('/')`) | `os.path` returns forward-slash paths (`D:/dir/file`) inside the Ansible process |
| `ssh` plugin needs OpenSSH ControlPersist and pty | Replacement `ssh` connection built on paramiko (`plugins/connection/ssh.py`) |
| Locale must be UTF-8 | Switches to the `<lang>.UTF-8` Windows locale |
| `SIGALRM` task timeouts | Timer threads that raise the timeout inside the worker thread |
| Search paths written as `a:b` | Split on `;` and on `:`, keeping drive letters intact |
| Package files bundled as `pkg.py` instead of `pkg/__init__.py` | importlib origins normalised before module_common checks them |
| Symlinks in collection tarballs (need admin/Dev Mode) | Falls back to copying the target |
| `pause` and other prompts use termios | Prompts read from the Windows console (`msvcrt`) |

Activation: `winsible.pth` installs one meta-path hook at interpreter start-up.
Nothing else happens until something imports `ansible`.

## SSH connection

The connection is still called `ssh`, so existing inventories work unchanged. Supported:

- `ansible_host`, `ansible_port`, `ansible_user`, `ansible_password`/`--ask-pass`,
  `ansible_ssh_private_key_file`/`--private-key`, key passphrase
- `~/.ssh/config` (HostName, User, Port, IdentityFile, ProxyJump, ProxyCommand)
- `-o ProxyJump=`, `-J`, `-o ProxyCommand=` in `ansible_ssh_common_args`/`ansible_ssh_extra_args`
- Windows OpenSSH agent and Pageant; `~/.ssh/known_hosts`
- become (sudo/su) with password prompts, pipelining, SFTP with a shell (`cat`) fallback
- One SSH connection and one SFTP session per host, shared by every task in the run
- TCP_NODELAY on (as OpenSSH does); without it every small SFTP/request packet waits one extra round trip

Host key checking is **on**, as in Ansible. An unknown host fails with a clear
message: run `ssh user@host` once to accept the key, or set
`ANSIBLE_HOST_KEY_CHECKING=False` (`ansible_ssh_record_host_keys=true` saves
accepted keys).

### Defaults winsible sets (you can override them with environment variables)

- `ANSIBLE_PIPELINING=True`: one SSH round trip per module instead of about five.
  On a 450 ms link the end-to-end test went 5m03s → 2m18s, and 1m46s with TCP_NODELAY. Set it to
  `False` if a target's sudoers has `requiretty`. Note that Ansible never pipelines `su`,
  so `become_method: su` costs about 4 extra round trips per task.
- `ANSIBLE_FORKS=10`
- Tip: set `ansible_python_interpreter` per group to skip interpreter discovery
  (saves 2 SSH round trips per host per run).

## Tests

```powershell
.\tests\run_tests.ps1           # controller-side tests, every venv found
.\tests\run_tests.ps1 -Remote   # + tests\remote\site.yml and parallel.yml against your own host
```

For `-Remote`, first copy `tests\remote\inventory.example.ini` to `inventory.ini` and
`parallel.example.ini` to `parallel.ini`, then point them at a Linux host you own:

```powershell
Copy-Item tests\remote\inventory.example.ini tests\remote\inventory.ini
Copy-Item tests\remote\parallel.example.ini tests\remote\parallel.ini
```

`tests/remote/site.yml` only touches `/tmp/winsible_test` on the target and
deletes it at the end. It covers facts, ping, command, shell, raw, script,
copy (including idempotence), template, lineinfile, file, stat, slurp, fetch,
loops, async/poll, task `timeout:` and pipelining.

`tests/remote/become.yml` (run by hand, it prompts for the password) checks become with and
without pipelining, a root-owned file in `/tmp`, and cleanup:

```powershell
ansible-playbook -i inventory.ini become.yml -K --become-method su   # root password
ansible-playbook -i inventory.ini become.yml -K                      # sudo, the login user's password
```

### Why is it slow on a far-away host?

Each remote command costs at least 2 network round trips (open channel + exec), whatever
the control node OS. On a link with a 450 ms ping that is ~0.9 s per command; a task
is 1 command with pipelining, ~5 without (tmp dir, upload, chmod, run, cleanup). Linux
Ansible with ControlPersist pays the same round trips over the same link; it is faster
only when the Linux machine sits closer to the targets. Fewer round trips: keep pipelining,
prefer sudo over su, set `ansible_python_interpreter`, `gather_facts: false` when not needed,
raise `forks` to run hosts in parallel.

## Limitations

- **Modules cannot run on the Windows machine itself.** `connection: local` and
  `delegate_to: localhost` work only for tasks that run on the controller (debug,
  set_fact, assert, add_host, lookups, `template`/`file` lookups, uri via lookups).
  Modules like `copy` or `command` targeting localhost would need a POSIX shell.
- Tasks share one Python process (GIL). Hosts still run in parallel because the
  work is network-bound, but CPU-heavy templating does not scale with forks.
- `ansible-pull`, `ansible-console` and persistent network connections
  (`network_cli`, `ansible-connection`) are untested.
- become: `su` is tested end to end (`tests/remote/become.yml -K --become-method su`). sudo is
  implemented the same way; its password exchange over paramiko was verified, the full playbook
  with `-K` (sudo) still needs a run with the right password.
- Not tested yet: WinRM/PSRP to Windows targets (pywinrm is installed).

## License

GPL-3.0-or-later (see `LICENSE`), the same license as ansible-core, which winsible patches at runtime.
