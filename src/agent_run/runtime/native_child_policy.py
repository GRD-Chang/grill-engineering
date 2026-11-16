"""Read the live Linux child policy without creating an admission child."""
from __future__ import annotations

import ctypes
import errno
import os
import signal
import sys
from functools import lru_cache


class _Sigaction(ctypes.Structure):
    # glibc Linux x86_64 LP64: bits/sigaction.h and bits/types/__sigset_t.h.
    _fields_ = [("handler", ctypes.c_void_p), ("mask", ctypes.c_ulong * 16),
                ("flags", ctypes.c_int), ("restorer", ctypes.c_void_p)]


@lru_cache(maxsize=1)
def _libc() -> ctypes.CDLL | None:
    # Cache only the ABI binding, never a signal disposition or capability.
    if (sys.platform != "linux" or os.uname().machine != "x86_64"
            or ctypes.sizeof(ctypes.c_void_p) != 8 or ctypes.sizeof(ctypes.c_ulong) != 8
            or ctypes.sizeof(ctypes.c_int) != 4):
        return None
    library = ctypes.CDLL(None, use_errno=True)
    try:
        version = library.gnu_get_libc_version
        version.argtypes = []
        version.restype = ctypes.c_char_p
        if not version().startswith(b"2."):
            return None
        library.sigaction.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                      ctypes.POINTER(_Sigaction)]
        library.sigaction.restype = ctypes.c_int
    except AttributeError:
        return None
    return library


def require_native_waitable_children() -> bool:
    """Return False for an unknown ABI; reject a known but unusable host.

    Linux validates waitid options and the pidfd before child eligibility.
    Self cannot be our child, so ECHILD proves the live syscall is available.
    This assumes ordinary kernel errno semantics, not syscall-result emulation.
    """
    library = _libc()
    if library is None:
        return False
    action = _Sigaction()
    if library.sigaction(signal.SIGCHLD, None, ctypes.byref(action)) != 0:
        raise OSError(ctypes.get_errno(), "cannot read SIGCHLD disposition")
    if action.handler is not None or action.flags & 2:  # SIG_DFL / SA_NOCLDWAIT
        raise ValueError("SIGCHLD must be default without SA_NOCLDWAIT")
    descriptor = os.pidfd_open(os.getpid())
    try:
        try:
            os.waitid(os.P_PIDFD, descriptor, os.WEXITED | os.WNOWAIT | os.WNOHANG)
        except ChildProcessError as error:
            if error.errno != errno.ECHILD:
                raise
        else:
            raise ValueError("self pidfd unexpectedly produced a child wait result")
    except BaseException as error:
        try:
            os.close(descriptor)
        except BaseException as cleanup_error:
            raise error from cleanup_error
        raise
    else:
        os.close(descriptor)
    return True
