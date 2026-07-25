"""Portable process birth/argv identity for liveness fencing.

A PID alone is reusable; a PID plus its birth stamp is a process identity.
Linux reads /proc (tick-precision start time, exact NUL-separated argv).
macOS uses sysctl syscalls only — KERN_PROC_PID for the microsecond birth
timestamp and KERN_PROCARGS2 for exact argv — never a ps(1) subprocess, so
probing stays cheap, atomic, and immune to subprocess monkeypatching or
fork pressure. Any other platform fails loud: returning "unknown" there
would make every liveness fence silently treat live owners as dead
(fail open).

INTENTIONAL DUPLICATION: `common/herdr/herdr_transport.py` carries the same
implementation for the runner stack. The upagent directory must stay
self-contained — the canonical-source re-exec and deployment flows ship it
without sibling directories — so it cannot load the herdr copy. If you change
this logic, change it in both files.
"""

from __future__ import annotations

import sys
from pathlib import Path


class ProcessIdentityError(RuntimeError):
    """Process identity cannot be determined safely on this platform."""


def process_start_time(pid: object) -> str | None:
    """Return the process birth stamp, or None when it is absent or a zombie."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if sys.platform == "linux":
        return _start_time_linux(pid)
    if sys.platform == "darwin":
        return _start_time_darwin(pid)
    raise ProcessIdentityError(
        f"process birth identity is not implemented on {sys.platform!r}; "
        "refusing to guess (liveness fencing would silently fail open)"
    )


def _start_time_linux(pid: int) -> str | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    close = text.rfind(")")
    if close < 0:
        return None
    fields = text[close + 2 :].split()
    # The suffix begins at field 3 (state); zombies can no longer mutate or create panes.
    if not fields or fields[0] == "Z":
        return None
    # starttime is field 22.
    return fields[19] if len(fields) > 19 else None


def _start_time_darwin(pid: int) -> str | None:
    """Read kinfo_proc via sysctl(KERN_PROC_PID): extern_proc starts the
    struct, and its first member is the p_starttime timeval (tv_sec int64 at
    offset 0, tv_usec int32 at offset 8); p_stat (SZOMB == 5) is at offset 36.
    Verified against ps(1) lstart. No subprocess is involved."""
    raw = _kinfo_proc_darwin(pid)
    if raw is None or len(raw) < 40:
        return None
    if raw[36] == 5:  # SZOMB: a zombie can no longer mutate or create panes.
        return None
    tv_sec = int.from_bytes(raw[0:8], sys.byteorder, signed=True)
    tv_usec = int.from_bytes(raw[8:12], sys.byteorder, signed=True)
    if tv_sec <= 0:
        return None
    return f"{tv_sec}.{tv_usec:06d}"


def _kinfo_proc_darwin(pid: int) -> bytes | None:
    import ctypes

    libc = _libc_darwin()
    ctl_kern, kern_proc, kern_proc_pid = 1, 14, 1
    mib = (ctypes.c_int * 4)(ctl_kern, kern_proc, kern_proc_pid, pid)
    buffer = ctypes.create_string_buffer(4096)
    size = ctypes.c_size_t(len(buffer))
    if libc.sysctl(mib, 4, buffer, ctypes.byref(size), None, 0) != 0:
        return None
    if size.value == 0:
        # The kernel answers success with zero bytes for a missing PID.
        return None
    return buffer.raw[: size.value]


def _libc_darwin(): # type: ignore[no-untyped-def]
    import ctypes
    import ctypes.util

    libc_path = ctypes.util.find_library("c")
    if libc_path is None:
        raise ProcessIdentityError("could not locate libc for sysctl process identity")
    return ctypes.CDLL(libc_path, use_errno=True)


def process_cmdline(pid: object) -> list[str]:
    """Return the exact argv for a live process, or [] when it is absent."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return []
    if sys.platform == "linux":
        return _cmdline_linux(pid)
    if sys.platform == "darwin":
        return _cmdline_darwin(pid)
    raise ProcessIdentityError(
        f"process command-line identity is not implemented on {sys.platform!r}; "
        "refusing to guess (ownership fencing would silently fail open)"
    )


def _cmdline_linux(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError):
        return []
    return [part.decode(errors="replace") for part in raw.split(b"\0") if part]


def _cmdline_darwin(pid: int) -> list[str]:
    """Read exact argv via sysctl(KERN_PROCARGS2): int argc, exec_path, NUL
    padding, then argc NUL-terminated arguments. A partial parse returns []
    (same contract as a dead process) instead of a truncated argv."""
    import ctypes

    libc = _libc_darwin()
    ctl_kern, kern_argmax, kern_procargs2 = 1, 8, 49
    argmax = ctypes.c_int(0)
    size = ctypes.c_size_t(ctypes.sizeof(argmax))
    mib2 = (ctypes.c_int * 2)(ctl_kern, kern_argmax)
    if libc.sysctl(mib2, 2, ctypes.byref(argmax), ctypes.byref(size), None, 0) != 0:
        raise ProcessIdentityError("sysctl(KERN_ARGMAX) failed")
    buffer = ctypes.create_string_buffer(argmax.value)
    size = ctypes.c_size_t(argmax.value)
    mib3 = (ctypes.c_int * 3)(ctl_kern, kern_procargs2, pid)
    if libc.sysctl(mib3, 3, buffer, ctypes.byref(size), None, 0) != 0:
        # EINVAL/ESRCH: the process is gone (or a zombie) — same as Linux OSError.
        return []
    raw = buffer.raw[: size.value]
    if len(raw) < 4:
        return []
    argc = int.from_bytes(raw[:4], sys.byteorder)
    if argc <= 0:
        return []
    exec_path_end = raw.find(b"\0", 4)
    if exec_path_end < 0:
        return []
    offset = exec_path_end
    while offset < len(raw) and raw[offset] == 0:
        offset += 1
    args: list[str] = []
    while len(args) < argc and offset < len(raw):
        end = raw.find(b"\0", offset)
        if end < 0:
            break
        args.append(raw[offset:end].decode(errors="replace"))
        offset = end + 1
    return args if len(args) == argc else []
