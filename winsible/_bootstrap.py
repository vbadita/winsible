"""Import hooks that make ansible-core run on a Windows controller.

Activated from ``winsible.pth`` at interpreter start-up.  Until something
imports ``ansible`` the only cost is one cheap meta-path finder.
"""
from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
import sys

_ACTIVE = False
_STUB_NAMES = frozenset(('fcntl', 'pwd', 'grp', 'termios', 'tty', 'resource', 'pty'))
_FACTORIES = None
_POST_IMPORT = None
_PRE_IMPORT: dict = {}


class _PostImportLoader(importlib.abc.Loader):
    def __init__(self, wrapped, hook, pre=None):
        self._wrapped = wrapped
        self._hook = hook
        self._pre = pre

    def create_module(self, spec):
        return self._wrapped.create_module(spec)

    def exec_module(self, module):
        if self._pre is not None:
            with self._pre(module):
                self._wrapped.exec_module(module)
        else:
            self._wrapped.exec_module(module)
        self._hook(module)

    def __getattr__(self, item):  # get_source, get_code, is_package, ...
        return getattr(self._wrapped, item)


class AnsibleHookFinder(importlib.abc.MetaPathFinder):
    """Activates winsible on the first ``import ansible`` and runs post-import patches."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname != 'ansible' and not fullname.startswith('ansible.'):
            return None
        if fullname == 'ansible':
            activate()
        if _POST_IMPORT is None:
            return None
        hook = _POST_IMPORT.get(fullname)
        if hook is None:
            return None
        spec = _find_real_spec(self, fullname, path, target)
        if spec is None or spec.loader is None:
            return None
        spec.loader = _PostImportLoader(spec.loader, hook, _PRE_IMPORT.get(fullname))
        return spec


class PosixStubFinder(importlib.abc.MetaPathFinder):
    """Serves fcntl/pwd/grp/termios/tty/resource stand-ins to the ansible package only."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in _STUB_NAMES or not _imported_from_ansible():
            return None
        return importlib.machinery.ModuleSpec(fullname, _StubLoader(), origin='winsible-stub')


class _StubLoader(importlib.abc.Loader):
    def create_module(self, spec):
        return _FACTORIES[spec.name]()

    def exec_module(self, module):
        pass


def _imported_from_ansible() -> bool:
    f = sys._getframe(1)
    while f is not None:
        name = f.f_globals.get('__name__', '') or ''
        if not name.startswith(('importlib', '_frozen_importlib', 'winsible')):
            return name == 'ansible' or name.startswith(('ansible.', 'ansible_collections.')) \
                or name.startswith('__main__') and 'ansible' in (f.f_globals.get('__file__') or '').lower()
        f = f.f_back
    return False


def _find_real_spec(skip, fullname, path, target):
    for finder in sys.meta_path:
        if finder is skip or isinstance(finder, (AnsibleHookFinder, PosixStubFinder)):
            continue
        find = getattr(finder, 'find_spec', None)
        if find is None:
            continue
        spec = find(fullname, path, target)
        if spec is not None:
            return spec
    return None


def activate() -> None:
    global _ACTIVE
    if _ACTIVE or sys.platform != 'win32':
        return
    _ACTIVE = True

    # Bind these before any stub exists so they keep choosing their Windows code paths.
    import getpass  # noqa: F401  (getpass picks win_getpass only if termios is missing)
    import subprocess  # noqa: F401
    import multiprocessing  # noqa: F401

    global _FACTORIES, _POST_IMPORT, _PRE_IMPORT
    from . import _patches, _stubs, _threadproc  # noqa: F401  (load before the finders go live)
    _FACTORIES = _stubs.FACTORIES
    _POST_IMPORT = _patches.POST_IMPORT
    _PRE_IMPORT = _patches.PRE_IMPORT
    _patches.patch_os()
    sys.meta_path.insert(0, PosixStubFinder())

    import os
    # Ansible's ssh connection plugin is POSIX-only; winsible ships a paramiko one.
    ours = _plugin_dir('connection')
    existing = os.environ.get('ANSIBLE_CONNECTION_PLUGINS', '')
    if ours not in existing.split(os.pathsep):
        os.environ['ANSIBLE_CONNECTION_PLUGINS'] = os.pathsep.join(p for p in (ours, existing) if p)
    os.environ.setdefault('PYTHONUTF8', '1')
    # Workers are threads, a dozen is plenty for the GIL.
    os.environ.setdefault('ANSIBLE_FORKS', '10')
    # One SSH exec per module instead of ~5 (mkdir/put/chmod/exec/rm). Big win on
    # high-latency links; set ANSIBLE_PIPELINING=False if a sudoers has 'requiretty'.
    os.environ.setdefault('ANSIBLE_PIPELINING', 'True')


def _plugin_dir(kind: str) -> str:
    import os
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'plugins', kind)


def install() -> None:
    if sys.platform != 'win32':
        return
    if not any(isinstance(f, AnsibleHookFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, AnsibleHookFinder())
    if 'ansible' in sys.modules:  # imported before us; activate late (best effort)
        activate()
