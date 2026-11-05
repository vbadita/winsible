"""Runtime patches applied to ansible-core modules right after they are imported.

Each function in ``POST_IMPORT`` receives the freshly executed module.
Nothing on disk is modified, so ansible-core can be upgraded with pip.
"""
from __future__ import annotations

import contextlib
import copy
import io
import os
import shutil
import signal as _signal
import sys
import threading
import time
import types

from . import _threadproc

_tls = threading.local()


# ------------------------------------------------------------------ os level
_FAKE_LIBC_NAME = 'winsible-libc'


def _wcwidth(ch: str) -> int:
    import unicodedata
    if not ch:
        return 0
    o = ord(ch)
    if o == 0:
        return 0
    if o < 32 or 0x7f <= o < 0xa0:
        return -1
    if unicodedata.combining(ch) or unicodedata.category(ch) in ('Mn', 'Me', 'Cf') or 0x1160 <= o <= 0x11ff or o == 0x200b:
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1


class _FakeLibc:
    @staticmethod
    def wcwidth(ch):
        return _wcwidth(ch)

    @staticmethod
    def wcswidth(text, n):
        total = 0
        for ch in text[:n]:
            w = _wcwidth(ch)
            if w < 0:
                return -1
            total += w
        return total


def patch_paths() -> None:
    """Make os.path speak forward slashes ('D:/dir/file').

    ansible-core's controller code is full of ``os.sep == '/'`` assumptions
    (regexes, ``split(os.sep)``, ``startswith(os.sep)``).  Windows file APIs
    accept '/' everywhere, so normalising os.path output is far safer than
    chasing every call site.
    """
    import ntpath

    if getattr(ntpath, '_winsible', False):
        return

    def fwd(v):
        if isinstance(v, str):
            return v.replace('\\', '/')
        if isinstance(v, bytes):
            return v.replace(b'\\', b'/')
        if isinstance(v, tuple):
            return tuple(fwd(x) for x in v)
        if isinstance(v, list):
            return [fwd(x) for x in v]
        return v

    def wrap(fn):
        def wrapper(*a, **kw):
            return fwd(fn(*a, **kw))
        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        wrapper.__wrapped__ = fn
        return wrapper

    for name in ('join', 'normpath', 'abspath', 'realpath', 'dirname', 'split', 'expanduser',
                 'expandvars', 'relpath', 'commonpath', 'splitdrive', 'splitext', 'splitroot'):
        fn = getattr(ntpath, name, None)
        if fn is not None:
            setattr(ntpath, name, wrap(fn))
    ntpath.sep = '/'
    ntpath.altsep = '\\'
    os.sep = '/'
    os.altsep = '\\'
    real_getcwd, real_getcwdb = os.getcwd, os.getcwdb
    os.getcwd = lambda: real_getcwd().replace('\\', '/')
    os.getcwdb = lambda: real_getcwdb().replace(b'\\', b'/')
    import tempfile
    tempfile.tempdir = fwd(tempfile.gettempdir())
    ntpath._winsible = True


def patch_os() -> None:
    patch_paths()
    real_get_blocking = os.get_blocking

    def get_blocking(fd):
        try:
            return real_get_blocking(fd)
        except OSError:
            return True  # Windows console handles / pipes: always blocking for our purposes

    os.get_blocking = get_blocking

    def _set_blocking(fd, blocking, _real=getattr(os, 'set_blocking', None)):
        try:
            if _real:
                _real(fd, blocking)
        except OSError:
            pass

    os.set_blocking = _set_blocking

    # ansible insists on a UTF-8 locale; Windows (10 1803+) supports '<lang>.UTF-8' locales.
    import locale
    real_setlocale = locale.setlocale

    def setlocale(category, loc=None):
        res = real_setlocale(category, loc)
        if loc == '' and res and 'utf' not in res.lower().replace('-', ''):
            base = res.split(';')[0].split('.')[0].split('=')[-1]
            for candidate in (f'{base}.UTF-8', '.UTF-8', 'en_US.UTF-8'):
                try:
                    return real_setlocale(category, candidate)
                except locale.Error:
                    continue
        return res

    locale.setlocale = setlocale

    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and (stream.encoding or '').lower().replace('-', '') != 'utf8':
                stream.reconfigure(encoding='utf-8')
        except Exception:
            pass

    # 'fork' does not exist on Windows; hand out the thread based context instead.
    import multiprocessing
    real_get_context = multiprocessing.get_context

    def get_context(method=None):
        if method == 'fork':
            return _threadproc.context
        return real_get_context(method)

    multiprocessing.get_context = get_context

    # display.py loads libc for wcwidth()/wcswidth(); provide pure-Python equivalents.
    import ctypes
    import ctypes.util
    real_find_library = ctypes.util.find_library
    real_load = ctypes.cdll.LoadLibrary

    def find_library(name):
        if name == 'c':
            return _FAKE_LIBC_NAME
        return real_find_library(name)

    def load_library(name, *a, **kw):
        if name == _FAKE_LIBC_NAME:
            return _FakeLibc()
        return real_load(name, *a, **kw)

    ctypes.util.find_library = find_library
    ctypes.cdll.LoadLibrary = load_library

    from ._stubs import FAKE_GID, FAKE_UID
    defaults = dict(
        getuid=lambda: FAKE_UID, geteuid=lambda: FAKE_UID,
        getgid=lambda: FAKE_GID, getegid=lambda: FAKE_GID,
        getgroups=lambda: [FAKE_GID],
        getpgid=lambda pid=0: os.getpid(), getpgrp=lambda: os.getpid(),
        tcgetpgrp=lambda fd: os.getpid(),
        setsid=lambda: None, setpgid=lambda pid, pgrp: None,
        killpg=lambda pgid, sig: None,
        sync=lambda: None,
    )
    if not hasattr(os, 'uname'):
        import platform
        u = platform.uname()  # computed (and cached) before os.uname exists

        class uname_result(tuple):
            sysname = property(lambda s: s[0])
            nodename = property(lambda s: s[1])
            release = property(lambda s: s[2])
            version = property(lambda s: s[3])
            machine = property(lambda s: s[4])

        _u = uname_result((u.system, u.node, u.release, u.version, u.machine))
        defaults['uname'] = lambda: _u
    for name, fn in defaults.items():
        if not hasattr(os, name):
            setattr(os, name, fn)


# ----------------------------------------------------- thread-aware stdio
class ThreadStdio:
    """sys.stdout/sys.stderr proxy: worker threads get a private StringIO
    (exactly what a forked worker gets), everybody else gets the console."""

    def __init__(self, real, slot):
        object.__setattr__(self, '_real', real)
        object.__setattr__(self, '_slot', slot)

    def _target(self):
        return getattr(_tls, self._slot, None) or self._real

    def __getattr__(self, item):
        return getattr(self._target(), item)

    def __setattr__(self, key, value):
        setattr(self._target(), key, value)

    def write(self, s):
        return self._target().write(s)

    def flush(self):
        return self._target().flush()

    def __iter__(self):
        return iter(self._target())


def install_stdio_proxy() -> None:
    if not isinstance(sys.stdout, ThreadStdio) and sys.stdout is not None:
        sys.stdout = ThreadStdio(sys.stdout, 'stdout')
    if not isinstance(sys.stderr, ThreadStdio) and sys.stderr is not None:
        sys.stderr = ThreadStdio(sys.stderr, 'stderr')


# ------------------------------------------------- ansible.utils.multiprocessing
def patch_multiprocessing(mod: types.ModuleType) -> None:
    mod.context = _threadproc.context


# ------------------------------------------------------- ansible.utils.display
def patch_display(mod: types.ModuleType) -> None:
    Display = mod.Display

    def _get_q(self):
        return getattr(_tls, 'final_q', None)

    def _set_q(self, value):
        _tls.final_q = value

    Display._final_q = property(_get_q, _set_q)

    def set_queue(self, queue):
        if _threadproc.current() is None:
            raise RuntimeError('queue cannot be set in parent process')
        _tls.final_q = queue

    Display.set_queue = set_queue

    def _set_column_width(self):
        try:
            cols = shutil.get_terminal_size((80, 24)).columns if sys.__stdout__ and sys.__stdout__.isatty() else 0
        except Exception:
            cols = 0
        self.columns = max(79, cols - 1)

    Display._set_column_width = _set_column_width

    orig_prompt_until = Display.prompt_until

    def prompt_until(self, msg, private=False, seconds=None, interrupt_input=None, complete_input=None):
        if self._final_q:  # inside a worker: proxy to the main thread like upstream
            return orig_prompt_until(self, msg, private, seconds, interrupt_input, complete_input)
        return _windows_prompt_until(self, mod, msg, private, seconds, interrupt_input, complete_input)

    Display.prompt_until = prompt_until
    install_stdio_proxy()


def _windows_prompt_until(display, mod, msg, private, seconds, interrupt_input, complete_input) -> bytes:
    import msvcrt

    if not (sys.__stdin__ and sys.__stdin__.isatty()):
        raise mod.AnsiblePromptNoninteractive('stdin is not interactive')

    display.display(msg)
    out = sys.__stdout__
    result = b''
    start = time.time()
    with display._lock:
        while msvcrt.kbhit():  # flush type-ahead
            msvcrt.getwch()
        while seconds is None or time.time() - start < seconds:
            if not msvcrt.kbhit():
                time.sleep(0.05)
                continue
            ch = msvcrt.getwch()
            if ch in ('\x00', '\xe0'):  # function / arrow key prefix
                msvcrt.getwch()
                continue
            key = ch.encode('utf-8', 'replace')
            if (interrupt_input is None and key == b'\x03') or (interrupt_input is not None and key.lower() in interrupt_input):
                out.write('\n')
                raise mod.AnsiblePromptInterrupt('user interrupt')
            if (complete_input is None and key in (b'\r', b'\n')) or (complete_input is not None and key.lower() in complete_input):
                out.write('\n')
                break
            if key in (b'\x08', b'\x7f'):
                if result:
                    result = result[:-1]
                    if not private:
                        out.write('\b \b')
            else:
                result += key
                if not private:
                    out.write(ch)
            out.flush()
    return result


# ------------------------------------------------- ansible.executor.process.worker
class _SignalShim(types.ModuleType):
    """`signal` as seen by worker.py: handlers can only be set on the main thread."""

    def __getattr__(self, item):
        return getattr(_signal, item)

    @staticmethod
    def signal(signum, handler):
        if threading.current_thread() is threading.main_thread():
            return _signal.signal(signum, handler)
        return None


def patch_worker(mod: types.ModuleType) -> None:
    import inspect

    WorkerProcess = mod.WorkerProcess
    orig_init = WorkerProcess.__init__
    sig = inspect.signature(orig_init)
    modern = hasattr(WorkerProcess, '_detach')  # ansible-core >= 2.19 style worker

    def __init__(self, *args, **kwargs):
        # A forked worker owns private copies of everything; give the thread the same.
        bound = sig.bind(self, *args, **kwargs)
        a = bound.arguments
        task = a['task']
        try:
            a['task'] = task.copy(exclude_tasks=True)
        except TypeError:
            a['task'] = task.copy()
        for name in ('task_vars', 'host', 'play_context', 'loader'):
            if name in a:
                a[name] = copy.copy(a[name])
        orig_init(*bound.args, **bound.kwargs)

    def _detach(self):
        # Threads share the process stdio; capture direct writes per thread instead.
        _tls.stdout = io.StringIO()
        _tls.stderr = io.StringIO()

    def _hard_exit(self, e):
        try:
            mod.display.error(str(e))
        except BaseException:
            pass
        raise SystemExit(1)

    def start(self):
        if hasattr(self, '_save_stdin'):  # older cores hand a dup of stdin to the child
            self._save_stdin()
        _threadproc.ThreadProcess.start(self)

    WorkerProcess.__init__ = __init__
    WorkerProcess._hard_exit = _hard_exit
    WorkerProcess.start = start
    if modern:
        WorkerProcess._detach = _detach
    else:
        def run(self):
            _detach(self)
            try:
                return self._run()
            except BaseException as e:
                self._hard_exit(e)

        WorkerProcess.run = run

    shim = _SignalShim('signal')
    mod.signal = shim

    # `current_worker` is a module global upstream; make it per thread.
    class _WorkerModule(type(mod)):
        @property
        def current_worker(self):
            return _threadproc.current()

        @current_worker.setter
        def current_worker(self, value):
            pass

    mod.__class__ = _WorkerModule


# ------------------------------------------------------ alarm based timeouts
class _AlarmFired(BaseException):
    pass


def patch_alarm_timeout(mod: types.ModuleType) -> None:
    cls = mod.AnsibleTimeoutError

    @classmethod
    @contextlib.contextmanager
    def alarm_timeout(klass, timeout):
        if timeout is not None:
            if not isinstance(timeout, int):
                raise TypeError(f"Timeout requires 'int' argument, not {type(timeout).__name__!r}.")
            if timeout < 0 or timeout > klass._MAX_TIMEOUT:
                raise ValueError(f'Timeout {timeout} is invalid, it must be between 0 and {klass._MAX_TIMEOUT}.')
        if not timeout:
            yield
            return

        target = threading.current_thread()
        lock = threading.Lock()
        state = {'done': False}

        def fire():
            with lock:
                if not state['done']:
                    _threadproc.raise_in_thread(target, _AlarmFired)

        timer = threading.Timer(timeout, fire)
        timer.daemon = True
        timer.start()
        try:
            yield
        except _AlarmFired:
            raise klass(timeout) from None
        finally:
            try:
                with lock:
                    state['done'] = True
                timer.cancel()
            except _AlarmFired:
                raise klass(timeout) from None

    cls.alarm_timeout = alarm_timeout


# ------------------------------------------------------------- plugin loader
def patch_plugin_loader(mod: types.ModuleType) -> None:
    """Workers used to load plugins in their own process; serialize it now."""
    lock = threading.RLock()
    PluginLoader = mod.PluginLoader

    def locked(fn):
        def wrapper(*a, **kw):
            with lock:
                return fn(*a, **kw)
        wrapper.__wrapped__ = fn
        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        return wrapper

    for name in ('get_with_context', 'find_plugin_with_context', '_load_module_source', 'all'):
        if hasattr(PluginLoader, name):
            setattr(PluginLoader, name, locked(getattr(PluginLoader, name)))


# --------------------------------------------------- task_queue_manager
def patch_tqm(mod: types.ModuleType) -> None:
    TQM = mod.TaskQueueManager

    def _signal_handler(self, signum, frame):
        # upstream os.kill()s every worker pid -- ours are threads of this very process.
        _signal.signal(signum, _signal.SIG_DFL)
        for w in getattr(self, '_workers', []) or []:
            if w is not None and w.is_alive():
                w.terminate()
        if signum == _signal.SIGINT:
            raise KeyboardInterrupt()
        raise SystemExit(128 + int(signum))

    TQM._signal_handler = _signal_handler


# --------------------------------------------------------- path lists
def split_pathspec(value: str) -> list[str]:
    r"""Split a search path on ';' and on POSIX ':' without breaking drive letters.

    'C:/a:/usr/share/b;D:\c' -> ['C:/a', '/usr/share/b', 'D:\c']
    """
    out, cur, i = [], '', 0
    while i < len(value):
        ch = value[i]
        if ch == ';':
            out.append(cur)
            cur = ''
        elif ch == ':' and not (len(cur) == 1 and cur.isalpha()):
            out.append(cur)
            cur = ''
        else:
            cur += ch
        i += 1
    out.append(cur)
    return [p for p in out if p]


def patch_config_manager(mod: types.ModuleType) -> None:
    for name in ('_ensure_type', 'ensure_type'):
        orig = getattr(mod, name, None)
        if orig is None:
            continue

        def wrapper(value, value_type, *a, _orig=orig, **kw):
            if value_type == 'pathspec' and isinstance(value, str):
                value = split_pathspec(value)
            return _orig(value, value_type, *a, **kw)

        wrapper.__name__ = name
        setattr(mod, name, wrapper)


def patch_option_helpers(mod: types.ModuleType) -> None:
    unfrackpath = mod.unfrackpath

    def unfrack_path(pathsep=False, follow=True):
        def inner(value):
            if pathsep:
                return [unfrackpath(x, follow=follow) for x in split_pathspec(value)]
            if value == '-':
                return value
            return unfrackpath(value, follow=follow)
        return inner

    mod.unfrack_path = unfrack_path


@contextlib.contextmanager
def _display_pre_exec(mod):
    # display.py calls os.register_at_fork() while instantiating its singleton;
    # provide it only for that moment (stdlib modules treat its presence as "can fork").
    added = not hasattr(os, 'register_at_fork')
    if added:
        os.register_at_fork = lambda **kw: None
    try:
        yield
    finally:
        if added:
            del os.register_at_fork


PRE_IMPORT = {
    'ansible.utils.display': _display_pre_exec,
}

# ------------------------------------------------- '/'-only absolute checks
def patch_datatag_tags(mod: types.ModuleType) -> None:
    Origin = mod.Origin

    def _post_validate(self) -> None:
        if self.path:
            if not os.path.isabs(self.path):
                raise RuntimeError('The `src` field must be an absolute path.')
        elif not self.description:
            raise RuntimeError('The `src` or `description` field must be specified.')

    Origin._post_validate = _post_validate


def patch_collection_finder(mod: types.ModuleType) -> None:
    base = mod._AnsibleCollectionPkgLoaderBase

    def get_data(self, path):
        if not path:
            raise ValueError('a path must be specified')
        if not os.path.isabs(path):
            raise ValueError('relative resource paths not supported')
        b_path = mod._to_bytes(path)
        if os.path.isfile(b_path):
            with open(b_path, 'rb') as fd:
                return fd.read()
        if b_path.endswith(b'__init__.py') and os.path.isdir(os.path.dirname(b_path)):
            return b''
        return None

    base.get_data = get_data


# ------------------------------------------- SIGALRM task timeouts (<= 2.18)
class _AlarmSignalShim(types.ModuleType):
    """`signal` for modules that implement timeouts with SIGALRM/alarm().

    alarm() starts a timer that raises the handler's exception in the calling
    thread.  The handler must be a function raising a no-arg exception class.
    """

    SIGALRM = 14

    def __init__(self, name, exc_for_handler):
        super().__init__(name)
        self._exc_for_handler = exc_for_handler
        self._local = threading.local()

    def __getattr__(self, item):
        return getattr(_signal, item)

    def signal(self, signum, handler):
        if signum == self.SIGALRM:
            old = getattr(self._local, 'handler', _signal.SIG_DFL)
            self._local.handler = handler
            return old
        if threading.current_thread() is threading.main_thread():
            return _signal.signal(signum, handler)
        return None

    def alarm(self, seconds):
        timer = getattr(self._local, 'timer', None)
        if timer is not None:
            timer.cancel()
            self._local.timer = None
        if seconds:
            target = threading.current_thread()
            exc = self._exc_for_handler(getattr(self._local, 'handler', None))
            if exc is not None:
                t = threading.Timer(seconds, _threadproc.raise_in_thread, (target, exc))
                t.daemon = True
                t.start()
                self._local.timer = t
        return 0


def patch_task_executor(mod: types.ModuleType) -> None:
    if not hasattr(mod, 'task_timeout') or not hasattr(mod, 'TaskTimeoutError'):
        return  # modern core: uses _alarm_timeout, patched separately
    mod.signal = _AlarmSignalShim(
        'signal', lambda h: mod.TaskTimeoutError if h is mod.task_timeout else None)


# ------------------------------------------------ module bundling (AnsiballZ)
def _fwd_spec(spec):
    if spec is not None:
        if isinstance(spec.origin, str):
            spec.origin = spec.origin.replace('\\', '/')
        if spec.submodule_search_locations is not None:
            spec.submodule_search_locations[:] = [p.replace('\\', '/') for p in spec.submodule_search_locations]
    return spec


def patch_module_common(mod: types.ModuleType) -> None:
    """module_common detects packages via ``origin.endswith('/__init__.py')``;
    importlib reports Windows origins with backslashes, which made every package
    get packed as ``pkg.py`` instead of ``pkg/__init__.py``."""
    import importlib as _importlib
    import importlib.machinery as _machinery

    class _PathFinder:
        def __getattr__(self, item):
            return getattr(_machinery.PathFinder, item)

        @staticmethod
        def find_spec(fullname, path=None, target=None):
            return _fwd_spec(_machinery.PathFinder.find_spec(fullname, path, target))

    machinery = types.ModuleType('importlib.machinery')
    machinery.__dict__.update(_machinery.__dict__)
    machinery.PathFinder = _PathFinder()

    shim = types.ModuleType('importlib')
    shim.__dict__.update(_importlib.__dict__)
    shim.machinery = machinery
    mod.importlib = shim


# ------------------------------------------------- symlinks without privilege
_pending_links: list = []


def _copy_for_link(src, dst) -> bool:
    import shutil
    target = os.path.join(os.path.dirname(dst), src) if not os.path.isabs(src) else src
    if os.path.isdir(target):
        shutil.copytree(target, dst, symlinks=False)
        return True
    if os.path.isfile(target):
        shutil.copy2(target, dst)
        return True
    return False


def safe_symlink(src, dst, target_is_directory=False, *, dir_fd=None, _real=os.symlink):
    """os.symlink that falls back to copying when Windows refuses
    (no Developer Mode / SeCreateSymbolicLinkPrivilege)."""
    try:
        return _real(src, dst, target_is_directory, dir_fd=dir_fd)
    except OSError as e:
        if getattr(e, 'winerror', None) != 1314:
            raise
    if not _copy_for_link(src, dst):
        _pending_links.append((src, dst))  # target not extracted yet


def resolve_pending_links() -> None:
    progress = True
    while _pending_links and progress:
        progress = False
        for item in list(_pending_links):
            if _copy_for_link(*item):
                _pending_links.remove(item)
                progress = True
    _pending_links.clear()


def patch_galaxy_collection(mod: types.ModuleType) -> None:
    os_shim = types.ModuleType('os')
    os_shim.__dict__.update(os.__dict__)
    os_shim.symlink = safe_symlink
    mod.os = os_shim
    orig = mod.install_artifact

    def install_artifact(*a, **kw):
        try:
            return orig(*a, **kw)
        finally:
            resolve_pending_links()

    mod.install_artifact = install_artifact


POST_IMPORT = {
    'ansible.galaxy.collection': patch_galaxy_collection,
    'ansible.executor.module_common': patch_module_common,
    'ansible.executor.task_executor': patch_task_executor,
    'ansible._internal._datatag._tags': patch_datatag_tags,
    'ansible.utils.collection_loader._collection_finder': patch_collection_finder,
    'ansible.config.manager': patch_config_manager,
    'ansible.cli.arguments.option_helpers': patch_option_helpers,
    'ansible.utils.multiprocessing': patch_multiprocessing,
    'ansible.utils.display': patch_display,
    'ansible.executor.process.worker': patch_worker,
    'ansible._internal._errors._alarm_timeout': patch_alarm_timeout,
    'ansible.plugins.loader': patch_plugin_loader,
    'ansible.executor.task_queue_manager': patch_tqm,
}
