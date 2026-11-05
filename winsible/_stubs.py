"""Windows stand-ins for POSIX-only stdlib modules that ansible-core imports.

They are only handed out to importers inside the ``ansible`` package (see
``_bootstrap.PosixStubFinder``), so other libraries keep detecting Windows
correctly.
"""
from __future__ import annotations

import getpass
import msvcrt
import os
import sys
import time
import types

STUB_NAMES = ('fcntl', 'pwd', 'grp', 'termios', 'tty', 'resource', 'pty')


# --------------------------------------------------------------------- fcntl
def _make_fcntl() -> types.ModuleType:
    m = types.ModuleType('fcntl', 'winsible stand-in for fcntl')
    m.LOCK_SH, m.LOCK_EX, m.LOCK_NB, m.LOCK_UN = 1, 2, 4, 8
    m.F_GETFD, m.F_SETFD, m.F_GETFL, m.F_SETFL = 1, 2, 3, 4
    m.FD_CLOEXEC = 1

    def _fd(f):
        return f if isinstance(f, int) else f.fileno()

    def flock(f, op):
        fd = _fd(f)
        pos = os.lseek(fd, 0, os.SEEK_CUR)
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            if op & m.LOCK_UN:
                try:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
                return None
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    return None
                except OSError:
                    if op & m.LOCK_NB:
                        raise BlockingIOError(11, 'Resource temporarily unavailable')
                    time.sleep(0.05)
        finally:
            os.lseek(fd, pos, os.SEEK_SET)

    def lockf(f, op, *_a):
        return flock(f, op)

    def fcntl(f, cmd, arg=0):
        os.fstat(_fd(f))  # raises OSError for an invalid fd, like the real thing
        return 0

    def ioctl(f, request, arg=0, mutate_flag=True):
        if request == 0x5413:  # TIOCGWINSZ: answer from the Windows console
            import shutil
            import struct
            size = shutil.get_terminal_size((80, 24))
            return struct.pack('HHHH', size.lines, size.columns, 0, 0)
        raise OSError(25, 'Inappropriate ioctl for device')

    m.flock, m.lockf, m.fcntl, m.ioctl = flock, lockf, fcntl, ioctl
    return m


# ---------------------------------------------------------------- pwd / grp
class _Struct(tuple):
    _fields: tuple = ()

    def __new__(cls, *values):
        return super().__new__(cls, values)

    def __getattr__(self, item):
        try:
            return self[self._fields.index(item)]
        except ValueError:
            raise AttributeError(item) from None


class struct_passwd(_Struct):
    _fields = ('pw_name', 'pw_passwd', 'pw_uid', 'pw_gid', 'pw_gecos', 'pw_dir', 'pw_shell')


class struct_group(_Struct):
    _fields = ('gr_name', 'gr_passwd', 'gr_gid', 'gr_mem')


FAKE_UID = 1000
FAKE_GID = 1000


def _me() -> struct_passwd:
    try:
        user = getpass.getuser()
    except Exception:
        user = 'user'
    return struct_passwd(user, 'x', FAKE_UID, FAKE_GID, user, os.path.expanduser('~'), 'cmd.exe')


def _make_pwd() -> types.ModuleType:
    m = types.ModuleType('pwd', 'winsible stand-in for pwd')
    m.struct_passwd = struct_passwd

    def getpwuid(uid):
        me = _me()
        if uid in (FAKE_UID, 0, -1) or uid == getattr(os, 'getuid', lambda: FAKE_UID)():
            return me
        raise KeyError(f'getpwuid(): uid not found: {uid}')

    def getpwnam(name):
        me = _me()
        if name == me.pw_name:
            return me
        raise KeyError(f'getpwnam(): name not found: {name!r}')

    m.getpwuid, m.getpwnam, m.getpwall = getpwuid, getpwnam, lambda: [_me()]
    return m


def _make_grp() -> types.ModuleType:
    m = types.ModuleType('grp', 'winsible stand-in for grp')
    m.struct_group = struct_group
    me = lambda: struct_group(_me().pw_name, 'x', FAKE_GID, [])  # noqa: E731

    def getgrgid(gid):
        if gid == FAKE_GID:
            return me()
        raise KeyError(f'getgrgid(): gid not found: {gid}')

    def getgrnam(name):
        if name == me().gr_name:
            return me()
        raise KeyError(f'getgrnam(): name not found: {name!r}')

    m.getgrgid, m.getgrnam, m.getgrall = getgrgid, getgrnam, lambda: [me()]
    return m


# ------------------------------------------------------------ termios / tty
def _make_termios() -> types.ModuleType:
    m = types.ModuleType('termios', 'winsible stand-in for termios')

    class error(Exception):
        pass

    m.error = error
    for i, name in enumerate((
        'TCSANOW', 'TCSADRAIN', 'TCSAFLUSH', 'TCIFLUSH', 'TCOFLUSH', 'TCIOFLUSH',
    )):
        setattr(m, name, i)
    for name, val in dict(
        BRKINT=2, ICRNL=256, INPCK=16, ISTRIP=32, IXON=1024, OPOST=1, CSIZE=48,
        PARENB=256, CS8=48, ECHO=8, ICANON=2, IEXTEN=32768, ISIG=1, VMIN=6,
        VTIME=5, VINTR=0, VERASE=2, TIOCGWINSZ=0x5413, ECHONL=64,
    ).items():
        setattr(m, name, val)

    def _unsupported(*_a, **_k):
        raise error(25, 'Inappropriate ioctl for device')

    for fn in ('tcgetattr', 'tcsetattr', 'tcflush', 'tcdrain', 'tcflow', 'tcsendbreak'):
        setattr(m, fn, _unsupported)
    return m


def _make_tty() -> types.ModuleType:
    m = types.ModuleType('tty', 'winsible stand-in for tty')
    m.IFLAG, m.OFLAG, m.CFLAG, m.LFLAG, m.ISPEED, m.OSPEED, m.CC = range(7)

    def _unsupported(*_a, **_k):
        raise sys.modules['termios'].error(25, 'Inappropriate ioctl for device')

    m.setraw = m.setcbreak = _unsupported
    return m


# ------------------------------------------------------------------ resource
def _make_resource() -> types.ModuleType:
    m = types.ModuleType('resource', 'winsible stand-in for resource')
    m.RLIM_INFINITY = -1
    for i, name in enumerate(('RLIMIT_CORE', 'RLIMIT_CPU', 'RLIMIT_FSIZE', 'RLIMIT_DATA',
                              'RLIMIT_STACK', 'RLIMIT_RSS', 'RLIMIT_NPROC', 'RLIMIT_NOFILE',
                              'RLIMIT_MEMLOCK', 'RLIMIT_AS')):
        setattr(m, name, i)
    m.error = OSError
    m.getrlimit = lambda res: (8192, 8192) if res == m.RLIMIT_NOFILE else (-1, -1)
    m.setrlimit = lambda res, limits: None
    m.getpagesize = lambda: 4096
    return m


def _make_pty() -> types.ModuleType:
    m = types.ModuleType('pty', 'winsible stand-in for pty')

    def _unsupported(*_a, **_k):
        raise OSError(38, 'pseudo terminals are not available on Windows')

    m.openpty = m.fork = m.spawn = _unsupported
    m.STDIN_FILENO, m.STDOUT_FILENO, m.STDERR_FILENO = 0, 1, 2
    return m


FACTORIES = {
    'pty': _make_pty,
    'fcntl': _make_fcntl,
    'pwd': _make_pwd,
    'grp': _make_grp,
    'termios': _make_termios,
    'tty': _make_tty,
    'resource': _make_resource,
}
