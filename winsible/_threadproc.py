"""A thread-backed stand-in for ``multiprocessing.Process``.

ansible-core forks one worker process per (host, task).  Windows has no fork,
and ``spawn`` cannot pickle the controller state the workers rely on, so the
workers run as threads instead.  ``winsible._patches`` gives each worker
private copies of the objects a forked child would have had to itself.
"""
from __future__ import annotations

import ctypes
import itertools
import multiprocessing
import os
import sys
import threading
import traceback

_tls = threading.local()
_counter = itertools.count(1)


def current() -> 'ThreadProcess | None':
    return getattr(_tls, 'proc', None)


def raise_in_thread(thread: threading.Thread, exc_type: type[BaseException]) -> bool:
    """Asynchronously raise ``exc_type`` inside ``thread`` (best effort)."""
    if not thread.is_alive() or thread.ident is None:
        return False
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(thread.ident), ctypes.py_object(exc_type))
    if res > 1:  # should never happen; undo
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(thread.ident), None)
        return False
    return res == 1


class ThreadProcess:
    """Implements the subset of the ``multiprocessing.Process`` API ansible uses."""

    def __init__(self, group=None, target=None, name=None, args=(), kwargs=None, *, daemon=None):
        self._target = target
        self._args = tuple(args)
        self._kwargs = dict(kwargs or {})
        self.name = name or f'WinsibleWorker-{next(_counter)}'
        self.daemon = True if daemon is None else daemon
        self._thread: threading.Thread | None = None
        self.exitcode: int | None = None
        self._closed = False

    # -- multiprocessing.Process API -------------------------------------
    @property
    def pid(self) -> int:
        return os.getpid()

    @property
    def ident(self):
        return self._thread.ident if self._thread else None

    def run(self):
        if self._target:
            self._target(*self._args, **self._kwargs)

    def start(self):
        if self._thread is not None:
            raise AssertionError('cannot start a process twice')
        self._thread = threading.Thread(target=self._bootstrap, name=self.name, daemon=True)
        self._thread.start()

    def _bootstrap(self):
        _tls.proc = self
        code = 0
        try:
            self.run()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        except BaseException:
            code = 1
            try:
                sys.__stderr__.write(f'[winsible] worker {self.name} crashed:\n{traceback.format_exc()}\n')
            except Exception:
                pass
        finally:
            _tls.proc = None
            self.exitcode = code

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)

    def terminate(self):
        if self._thread and self._thread.is_alive():
            raise_in_thread(self._thread, SystemExit)

    kill = terminate

    def close(self):
        if self.is_alive():
            raise ValueError('Cannot close a process while it is still running.')
        self._closed = True

    @property
    def sentinel(self):
        raise NotImplementedError('sentinel is not available for thread workers')


class _ParentProcessMarker:
    name = 'MainProcess'
    pid = os.getpid()


class ThreadContext:
    """A multiprocessing context whose ``Process`` is a thread.

    Everything else (Queue, Lock, Event, ...) comes from the ``spawn`` context,
    which works fine when producers and consumers live in one process.
    """

    Process = ThreadProcess

    def __init__(self):
        self._real = multiprocessing.get_context('spawn')

    def __getattr__(self, item):
        return getattr(self._real, item)

    def get_start_method(self, allow_none=False):
        return 'winsible-thread'

    def parent_process(self):
        # Inside a worker thread we behave like a child process.
        return _ParentProcessMarker() if current() is not None else None

    def current_process(self):
        return current() or self._real.current_process()


context = ThreadContext()
