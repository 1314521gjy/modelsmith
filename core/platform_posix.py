"""POSIX/Linux platform implementation for ModelSmith core (stdlib only).

Linux is the AutoDL target; this module is imported on POSIX and is not
exercised on Windows.  Functions mirror platform_win names/signatures.
"""

from __future__ import annotations

import ctypes
import fcntl
import os
import signal
from pathlib import Path

PLATFORM = "posix"


class ChildDeathGuard:
    """PR_SET_PDEATHSIG + setsid as a pgid fallback."""

    PR_SET_PDEATHSIG = 1

    def __init__(self, enabled=True):
        self.enabled = False
        self.reason = None
        self.pgid = None
        if not enabled:
            self.reason = "disabled"
            return
        try:
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            if libc.prctl(self.PR_SET_PDEATHSIG, int(signal.SIGKILL)) != 0:
                raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG)")
            self.pgid = os.getpgid(0)
            self.enabled = True
        except Exception as exc:
            self.reason = "%s: %s" % (type(exc).__name__, exc)


def install_child_death_guard(enabled=True):
    return ChildDeathGuard(enabled=enabled)


def kill_tree(pid):
    if not pid:
        return
    try:
        os.killpg(os.getpgid(int(pid)), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        os.kill(int(pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def memory_info(probe=None):
    try:
        if probe is not None:
            total_bytes, avail_bytes = probe()
        else:
            values = {}
            with open("/proc/meminfo", "r", encoding="ascii") as fh:
                for line in fh:
                    key, _, rest = line.partition(":")
                    if key in ("MemTotal", "MemAvailable"):
                        values[key] = int(rest.strip().split()[0]) * 1024
            total_bytes = values["MemTotal"]
            avail_bytes = values["MemAvailable"]
        total_mib = int(total_bytes) // (1024 * 1024)
        used_mib = max(0, int(total_bytes) - int(avail_bytes)) // (1024 * 1024)
        if total_mib <= 0 or used_mib > total_mib:
            raise ValueError("invalid memory readings")
        return {
            "ram_used_mib": used_mib,
            "ram_total_mib": total_mib,
            "ram_pct": round((used_mib / total_mib) * 100.0, 1),
        }
    except Exception:
        return {"ram_used_mib": None, "ram_total_mib": None, "ram_pct": None}


class GpuLock:
    """POSIX flock; released by the OS when the process dies."""

    def __init__(self, path):
        self.path = Path(path)
        self._fh = None
        self.owner = None

    def acquire(self, owner=""):
        if self._fh is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fh = open(self.path, "r+b")
        except FileNotFoundError:
            fh = open(self.path, "w+b")
        try:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(b"0")
                fh.flush()
            fh.seek(0)
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                fh.close()
            except OSError:
                pass
            return False
        self._fh = fh
        self.owner = owner
        try:
            raw = ("pid=%d job=%s acquired_at=%s\n" % (os.getpid(), owner or "-", __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat())).encode("utf-8")
            fh.seek(0)
            fh.write(raw)
            fh.flush()
            fh.truncate(len(raw))
        except OSError:
            pass
        return True

    def release(self):
        fh = self._fh
        if fh is None:
            return
        self._fh = None
        try:
            fh.seek(0)
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                fh.close()
            except OSError:
                pass
