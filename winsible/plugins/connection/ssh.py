# winsible: paramiko based replacement for ansible.builtin.ssh on Windows controllers.
# GNU General Public License v3.0+ (see https://www.gnu.org/licenses/gpl-3.0.txt)
from __future__ import annotations

DOCUMENTATION = r"""
    name: ssh
    short_description: connect via SSH (paramiko implementation for Windows controllers)
    description:
        - Drop-in replacement for C(ansible.builtin.ssh) used by winsible on Windows,
          where the OpenSSH based plugin cannot run (no fork/pty/ControlPersist).
        - Implemented with paramiko. Reads C(~/.ssh/config), C(~/.ssh/known_hosts),
          the Windows OpenSSH agent / Pageant, and understands C(-o ProxyJump=), C(-J) and
          C(-o ProxyCommand=) inside C(ssh_common_args) / C(ssh_extra_args).
        - Connections are kept open and shared between tasks for the whole run.
    author: winsible
    options:
      host:
          description: Hostname/IP to connect to.
          default: inventory_hostname
          type: string
          vars:
               - name: inventory_hostname
               - name: ansible_host
               - name: ansible_ssh_host
      port:
          description: Remote port.
          type: int
          default: 22
          ini:
            - section: defaults
              key: remote_port
          env:
            - name: ANSIBLE_REMOTE_PORT
          vars:
            - name: ansible_port
            - name: ansible_ssh_port
          keyword:
            - name: port
      remote_user:
          description: User name to log in as.
          type: string
          ini:
            - section: defaults
              key: remote_user
          env:
            - name: ANSIBLE_REMOTE_USER
          vars:
            - name: ansible_user
            - name: ansible_ssh_user
          keyword:
            - name: remote_user
      password:
          description: SSH password.
          type: string
          vars:
              - name: ansible_password
              - name: ansible_ssh_pass
              - name: ansible_ssh_password
      private_key_file:
          description: Path to a private key.
          type: string
          ini:
            - section: defaults
              key: private_key_file
          env:
            - name: ANSIBLE_PRIVATE_KEY_FILE
          vars:
            - name: ansible_private_key_file
            - name: ansible_ssh_private_key_file
          cli:
            - name: private_key_file
              option: '--private-key'
      private_key_passphrase:
          description: Passphrase for the private key.
          type: string
          env:
            - name: ANSIBLE_PRIVATE_KEY_PASSPHRASE
          vars:
            - name: ansible_private_key_passphrase
      host_key_checking:
          description: Verify the remote host key against known_hosts.
          type: boolean
          default: true
          ini:
              - section: defaults
                key: 'host_key_checking'
              - section: ssh_connection
                key: 'host_key_checking'
          env:
              - name: ANSIBLE_HOST_KEY_CHECKING
              - name: ANSIBLE_SSH_HOST_KEY_CHECKING
          vars:
              - name: ansible_host_key_checking
              - name: ansible_ssh_host_key_checking
      record_host_keys:
          description: Save newly accepted host keys to C(~/.ssh/known_hosts) when host key checking is off.
          type: boolean
          default: false
          env:
            - name: WINSIBLE_RECORD_HOST_KEYS
          vars:
            - name: ansible_ssh_record_host_keys
      ssh_args:
          description: Accepted for compatibility; only C(-o Key=Value) options paramiko understands are used.
          type: string
          default: ''
          ini:
              - section: 'ssh_connection'
                key: 'ssh_args'
          env:
              - name: ANSIBLE_SSH_ARGS
          vars:
              - name: ansible_ssh_args
      ssh_common_args:
          description: Extra ssh arguments; ProxyJump/ProxyCommand/User/Port options are honoured.
          type: string
          default: ''
          ini:
              - section: 'ssh_connection'
                key: 'ssh_common_args'
          env:
              - name: ANSIBLE_SSH_COMMON_ARGS
          vars:
              - name: ansible_ssh_common_args
          cli:
              - name: ssh_common_args
      ssh_extra_args:
          description: Like I(ssh_common_args).
          type: string
          default: ''
          ini:
            - key: ssh_extra_args
              section: ssh_connection
          env:
            - name: ANSIBLE_SSH_EXTRA_ARGS
          vars:
              - name: ansible_ssh_extra_args
          cli:
            - name: ssh_extra_args
      timeout:
          description: Connection timeout in seconds.
          type: int
          default: 10
          ini:
            - key: timeout
              section: defaults
            - key: timeout
              section: ssh_connection
          env:
            - name: ANSIBLE_TIMEOUT
            - name: ANSIBLE_SSH_TIMEOUT
          vars:
            - name: ansible_ssh_timeout
          cli:
            - name: timeout
      reconnection_retries:
          description: Number of connection attempts.
          type: int
          default: 0
          env:
            - name: ANSIBLE_SSH_RETRIES
          ini:
            - section: connection
              key: retries
            - section: ssh_connection
              key: retries
          vars:
            - name: ansible_ssh_retries
      look_for_keys:
          description: Try keys found in C(~/.ssh).
          type: boolean
          default: true
          env:
            - name: WINSIBLE_LOOK_FOR_KEYS
          vars:
            - name: ansible_ssh_look_for_keys
      allow_agent:
          description: Use the Windows OpenSSH agent / Pageant.
          type: boolean
          default: true
          env:
            - name: WINSIBLE_ALLOW_AGENT
          vars:
            - name: ansible_ssh_allow_agent
      use_sftp:
          description: Transfer files with SFTP (falls back to C(cat) over the shell if SFTP is unavailable).
          type: boolean
          default: true
          env:
            - name: WINSIBLE_USE_SFTP
          vars:
            - name: ansible_ssh_use_sftp
      become_success_timeout:
          type: int
          default: 30
          description: Seconds to wait for become to succeed.
          vars:
            - name: ansible_ssh_become_success_timeout
      pipelining:
          description: Send module code over stdin instead of copying a file.
          type: boolean
          default: false
          env:
            - name: ANSIBLE_PIPELINING
            - name: ANSIBLE_SSH_PIPELINING
          ini:
            - section: defaults
              key: pipelining
            - section: connection
              key: pipelining
            - section: ssh_connection
              key: pipelining
          vars:
            - name: ansible_pipelining
            - name: ansible_ssh_pipelining
"""

import getpass
import io
import os
import shlex
import socket
import threading
import time

from ansible.errors import AnsibleAuthenticationFailure, AnsibleConnectionFailure, AnsibleError, AnsibleFileNotFound
from ansible.module_utils.common.text.converters import to_bytes, to_native, to_text
from ansible.plugins.connection import ConnectionBase
from ansible.utils.display import Display

try:
    import paramiko
except ImportError as e:  # pragma: no cover
    raise AnsibleError('winsible ssh connection requires paramiko: pip install paramiko') from e

display = Display()

_CACHE: dict[tuple, paramiko.SSHClient] = {}
_CACHE_LOCK = threading.Lock()
_KEY_LOCKS: dict[tuple, threading.Lock] = {}
_SSH_CONFIG: paramiko.SSHConfig | None = None
_SFTP: dict[int, paramiko.SFTPClient] = {}

# paramiko logs harmless protocol chatter ("Success for unrequested channel!") to stderr
import logging  # noqa: E402
logging.getLogger('paramiko').addHandler(logging.NullHandler())
logging.getLogger('paramiko').propagate = False


def _ssh_config() -> paramiko.SSHConfig:
    global _SSH_CONFIG
    if _SSH_CONFIG is None:
        cfg = paramiko.SSHConfig()
        path = os.path.expanduser('~/.ssh/config')
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                cfg.parse(f)
        _SSH_CONFIG = cfg
    return _SSH_CONFIG


def _parse_ssh_args(*argstrings: str) -> dict[str, str]:
    """Pull the -o Key=Value / -J / -p / -i / -l options paramiko can honour."""
    opts: dict[str, str] = {}
    for argstring in argstrings:
        if not argstring:
            continue
        try:
            args = shlex.split(argstring, posix=True)
        except ValueError:
            args = argstring.split()
        i = 0
        while i < len(args):
            a = args[i]
            nxt = args[i + 1] if i + 1 < len(args) else None
            if a == '-o' and nxt:
                k, _, v = nxt.replace(' ', '=', 1).partition('=')
                opts[k.strip().lower()] = v.strip()
                i += 2
                continue
            if a.startswith('-o') and len(a) > 2:
                k, _, v = a[2:].replace(' ', '=', 1).partition('=')
                opts[k.strip().lower()] = v.strip()
            elif a == '-J' and nxt:
                opts['proxyjump'] = nxt
                i += 2
                continue
            elif a in ('-p', '-i', '-l') and nxt:
                opts[{'-p': 'port', '-i': 'identityfile', '-l': 'user'}[a]] = nxt
                i += 2
                continue
            i += 1
    return opts


class _Recorder(paramiko.MissingHostKeyPolicy):
    def __init__(self, record: bool):
        self.record = record

    def missing_host_key(self, client, hostname, key):
        display.vv(f'winsible ssh: accepting unknown host key for {hostname} ({key.get_name()})')
        client.get_host_keys().add(hostname, key.get_name(), key)
        if self.record:
            path = os.path.expanduser('~/.ssh/known_hosts')
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'a', encoding='utf-8') as f:
                f.write(f'{hostname} {key.get_name()} {key.get_base64()}\n')


class _SharedSFTP:
    """Wrapper whose close() keeps the cached SFTP session open."""

    def __init__(self, sftp):
        self._sftp = sftp

    def __getattr__(self, item):
        return getattr(self._sftp, item)

    def close(self):
        pass


class Connection(ConnectionBase):
    """SSH via paramiko (winsible)."""

    transport = 'ssh'
    has_pipelining = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client: paramiko.SSHClient | None = None
        self._key: tuple | None = None

    # ------------------------------------------------------------ connect
    def _resolve(self):
        host = self.get_option('host') or self._play_context.remote_addr
        sshargs = _parse_ssh_args(self.get_option('ssh_args'), self.get_option('ssh_common_args'),
                                  self.get_option('ssh_extra_args'))
        cfg = _ssh_config().lookup(host)
        user = self.get_option('remote_user') or sshargs.get('user') or cfg.get('user') or getpass.getuser()
        port = int(self.get_option('port') or sshargs.get('port') or cfg.get('port') or 22)
        hostname = cfg.get('hostname', host)
        key_file = self.get_option('private_key_file') or sshargs.get('identityfile')
        key_files = [os.path.expanduser(key_file)] if key_file else [
            os.path.expanduser(p) for p in cfg.get('identityfile', []) if os.path.exists(os.path.expanduser(p))]
        proxy_jump = sshargs.get('proxyjump') or cfg.get('proxyjump')
        proxy_cmd = sshargs.get('proxycommand') or cfg.get('proxycommand')
        if proxy_jump and proxy_jump.lower() == 'none':
            proxy_jump = None
        if proxy_cmd and proxy_cmd.lower() == 'none':
            proxy_cmd = None
        return hostname, port, user, key_files, proxy_jump, proxy_cmd

    def _connect(self):
        if self._connected and self._client:
            return self
        hostname, port, user, key_files, proxy_jump, proxy_cmd = self._resolve()
        password = self.get_option('password') or self._play_context.password
        key = (hostname, port, user, tuple(key_files), proxy_jump, proxy_cmd, bool(password))
        with _CACHE_LOCK:
            lock = _KEY_LOCKS.setdefault(key, threading.Lock())
        with lock:
            client = _CACHE.get(key)
            if client is None or not client.get_transport() or not client.get_transport().is_active():
                client = self._open(hostname, port, user, key_files, password, proxy_jump, proxy_cmd)
                _CACHE[key] = client
        self._client, self._key = client, key
        self._connected = True
        return self

    def _sock_for(self, hostname, port, proxy_jump, proxy_cmd):
        timeout = self.get_option('timeout')
        if proxy_cmd:
            cmd = proxy_cmd.replace('%h', hostname).replace('%p', str(port)).replace('%%', '%')
            display.vvv(f'winsible ssh: ProxyCommand {cmd}')
            return paramiko.ProxyCommand(cmd)
        if proxy_jump:
            # ProxyJump may be a chain a,b,c -- connect through each hop in turn
            sock = None
            hops = [h.strip() for h in proxy_jump.split(',') if h.strip()]
            for hop in hops:
                hu, _, hh = hop.rpartition('@')
                hh, _, hp = hh.partition(':')
                hcfg = _ssh_config().lookup(hh)
                jump = paramiko.SSHClient()
                self._load_host_keys(jump)
                display.vvv(f'winsible ssh: jumping via {hop}')
                jump.connect(hcfg.get('hostname', hh), port=int(hp or hcfg.get('port', 22)),
                             username=hu or hcfg.get('user') or getpass.getuser(), sock=sock,
                             timeout=timeout, key_filename=[os.path.expanduser(p) for p in hcfg.get('identityfile', [])
                                                            if os.path.exists(os.path.expanduser(p))] or None,
                             allow_agent=self.get_option('allow_agent'), look_for_keys=self.get_option('look_for_keys'))
                sock = jump.get_transport()
                self._jumps = getattr(self, '_jumps', []) + [jump]
            return sock.open_channel('direct-tcpip', (hostname, port), ('127.0.0.1', 0), timeout=timeout)
        return None

    def _load_host_keys(self, client):
        try:
            client.load_system_host_keys()
        except Exception:
            pass
        kh = os.path.expanduser('~/.ssh/known_hosts')
        if os.path.exists(kh):
            try:
                client.load_host_keys(kh)
            except Exception as e:
                display.warning(f'winsible ssh: could not read {kh}: {e}')
        if self.get_option('host_key_checking'):
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            client.set_missing_host_key_policy(_Recorder(self.get_option('record_host_keys')))

    def _open(self, hostname, port, user, key_files, password, proxy_jump, proxy_cmd):
        display.vvv(f'ESTABLISH SSH (winsible/paramiko) CONNECTION FOR USER: {user} ON PORT {port} TO {hostname}',
                    host=hostname)
        retries = max(0, int(self.get_option('reconnection_retries') or 0))
        last = None
        for attempt in range(retries + 1):
            client = paramiko.SSHClient()
            self._load_host_keys(client)
            try:
                client.connect(
                    hostname, port=port, username=user, password=password,
                    key_filename=key_files or None,
                    passphrase=self.get_option('private_key_passphrase'),
                    timeout=self.get_option('timeout'), banner_timeout=self.get_option('timeout') * 3,
                    auth_timeout=self.get_option('timeout') * 3,
                    allow_agent=self.get_option('allow_agent'),
                    look_for_keys=self.get_option('look_for_keys') and not password,
                    sock=self._sock_for(hostname, port, proxy_jump, proxy_cmd),
                )
                client.get_transport().set_keepalive(30)
                # like OpenSSH: no Nagle delay, otherwise small SFTP/request packets wait an extra RTT
                for t in [client.get_transport()] + [j.get_transport() for j in getattr(self, '_jumps', [])]:
                    if isinstance(getattr(t, 'sock', None), socket.socket):
                        try:
                            t.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        except OSError:
                            pass
                return client
            except paramiko.ssh_exception.BadHostKeyException as e:
                raise AnsibleConnectionFailure(f'host key mismatch for {hostname}: {e}') from e
            except paramiko.ssh_exception.SSHException as e:
                msg = str(e)
                if 'not found in known_hosts' in msg:
                    raise AnsibleConnectionFailure(
                        f'Host key for {hostname} is not in ~/.ssh/known_hosts. Connect once with '
                        f'"ssh {user}@{hostname}" to accept it, or set ANSIBLE_HOST_KEY_CHECKING=False.') from e
                if isinstance(e, paramiko.AuthenticationException):
                    raise AnsibleAuthenticationFailure(f'Authentication failed for {user}@{hostname}: {e}') from e
                last = e
            except (OSError, socket.timeout, EOFError) as e:
                last = e
            if attempt < retries:
                time.sleep(min(2 ** attempt, 10))
        raise AnsibleConnectionFailure(f'Failed to connect to {hostname} port {port}: {to_native(last)}')

    # ------------------------------------------------------------ execute
    def exec_command(self, cmd, in_data=None, sudoable=True):
        super().exec_command(cmd, in_data=in_data, sudoable=sudoable)
        self._connect()
        transport = self._client.get_transport()
        try:
            chan = transport.open_session()
        except Exception as e:
            # stale cached transport: reconnect once
            self.close(force=True)
            self._connect()
            chan = self._client.get_transport().open_session()

        become = self.become if sudoable and self.become and getattr(self.become, '_id', None) else None
        # like OpenSSH -tt in the stock plugin: a tty for privileged commands without piped
        # stdin (su always runs this way, Ansible never pipelines su). Pipelined sudo reads
        # the password from stdin (-S) and must not get a tty, or stdin data would be echoed.
        use_pty = bool(become and not in_data)
        if use_pty:
            chan.get_pty(term=os.environ.get('TERM', 'vt100'), width=0, height=0)

        display.vvv(f'EXEC {cmd}', host=self._play_context.remote_addr)
        chan.exec_command(to_bytes(cmd, errors='surrogate_or_strict'))

        out, err = bytearray(), bytearray()

        def pump(block=0.05):
            got = False
            if chan.recv_ready():
                out.extend(chan.recv(65536))
                got = True
            if chan.recv_stderr_ready():
                err.extend(chan.recv_stderr(65536))
                got = True
            if not got:
                time.sleep(block)
            return got

        if become:
            out_b, err_b = self._await_become(chan, out, err, pump)
            out[:] = out_b
            err[:] = err_b

        if in_data:
            view = memoryview(in_data)
            while view:
                n = chan.send(view[:32768])
                view = view[n:]
        try:
            chan.shutdown_write()
        except Exception:
            pass

        while not chan.exit_status_ready() or chan.recv_ready() or chan.recv_stderr_ready():
            pump(0.01)
        # drain whatever is left
        while chan.recv_ready() or chan.recv_stderr_ready():
            pump(0)
        rc = chan.recv_exit_status()
        chan.close()
        return rc, bytes(out), bytes(err)

    def _await_become(self, chan, out, err, pump):
        become = self.become
        timeout = max(1, int(self.get_option('become_success_timeout') or 30))
        deadline = time.time() + timeout
        prompted = False
        while not become.check_success(bytes(out)):
            pump()
            if become.check_success(bytes(out)):
                break
            if become.expect_prompt() and (become.check_password_prompt(bytes(err)) or
                                           become.check_password_prompt(bytes(out))):
                display.vvvv('%s password prompt seen' % become.name, host=self._play_context.remote_addr)
                if prompted:
                    raise AnsibleError('Incorrect %s password' % become.name)
                passwd = become.get_option('become_pass', playcontext=self._play_context)
                if not passwd:
                    raise AnsibleError('Missing %s password' % become.name)
                chan.sendall(to_bytes(passwd, errors='surrogate_or_strict') + b'\n')
                prompted = True
                out.clear()
                err.clear()
                continue
            if become.check_incorrect_password(bytes(out) + bytes(err)):
                raise AnsibleError('Incorrect %s password' % become.name)
            if become.check_missing_password(bytes(out) + bytes(err)):
                raise AnsibleError('Missing %s password' % become.name)
            if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
                raise AnsibleError('%s failed (rc=%s): %s' % (become.name, chan.recv_exit_status(),
                                                             to_text(bytes(out) + bytes(err)).strip()))
            if time.time() > deadline:
                raise AnsibleError('Timeout (%ss) waiting for privilege escalation prompt: %s'
                                   % (timeout, to_text(bytes(out) + bytes(err))))
        # strip everything up to and including the BECOME-SUCCESS line
        data = bytes(out)
        marker = to_bytes(become.success)
        idx = data.find(marker)
        nl = data.find(b'\n', idx)
        rest = data[nl + 1:] if nl != -1 else b''
        return bytearray(rest), bytearray()

    # ------------------------------------------------------------ files
    def _sftp(self):
        """One SFTP session per SSH connection, reused for every transfer."""
        if not self.get_option('use_sftp'):
            return None
        key = id(self._client)
        with _CACHE_LOCK:
            sftp = _SFTP.get(key)
        if sftp is not None and sftp.get_channel() is not None and not sftp.get_channel().closed:
            return _SharedSFTP(sftp)
        try:
            sftp = self._client.open_sftp()
        except Exception as e:
            display.vv(f'winsible ssh: SFTP unavailable ({e}); falling back to shell transfer')
            return None
        with _CACHE_LOCK:
            _SFTP[key] = sftp
        return _SharedSFTP(sftp)

    def put_file(self, in_path, out_path):
        super().put_file(in_path, out_path)
        self._connect()
        display.vvv(f'PUT {in_path} TO {out_path}', host=self._play_context.remote_addr)
        if not os.path.exists(to_bytes(in_path, errors='surrogate_or_strict')):
            raise AnsibleFileNotFound(f'file or module does not exist: {to_native(in_path)}')
        sftp = self._sftp()
        if sftp:
            try:
                sftp.put(in_path, out_path, confirm=True)
                return
            except IOError as e:
                raise AnsibleError(f'failed to transfer file to {out_path}: {e}') from e
            finally:
                sftp.close()
        with open(in_path, 'rb') as f:
            data = f.read()
        rc, _o, e = self.exec_command(f'cat > {shlex.quote(out_path)}', in_data=data or b'', sudoable=False)
        if rc != 0:
            raise AnsibleError(f'failed to transfer file to {out_path}: {to_text(e)}')

    def fetch_file(self, in_path, out_path):
        super().fetch_file(in_path, out_path)
        self._connect()
        display.vvv(f'FETCH {in_path} TO {out_path}', host=self._play_context.remote_addr)
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        sftp = self._sftp()
        if sftp:
            try:
                sftp.get(in_path, out_path)
                return
            except IOError as e:
                raise AnsibleError(f'failed to transfer file from {in_path}: {e}') from e
            finally:
                sftp.close()
        rc, o, e = self.exec_command(f'cat {shlex.quote(in_path)}', sudoable=False)
        if rc != 0:
            raise AnsibleError(f'failed to transfer file from {in_path}: {to_text(e)}')
        with open(out_path, 'wb') as f:
            f.write(o)

    # ------------------------------------------------------------ lifecycle
    def reset(self):
        self.close(force=True)
        self._connect()

    def close(self, force=False):
        # Keep the shared connection for the next task unless asked to drop it.
        if force and self._key is not None:
            with _CACHE_LOCK:
                client = _CACHE.pop(self._key, None)
                sftp = _SFTP.pop(id(client), None) if client else None
            if client:
                try:
                    if sftp:
                        sftp.close()
                    client.close()
                except Exception:
                    pass
        self._client = None
        self._connected = False


def _close_all():
    for c in list(_CACHE.values()):
        try:
            c.close()
        except Exception:
            pass
    _CACHE.clear()


import atexit  # noqa: E402

atexit.register(_close_all)
