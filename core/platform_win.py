"""Windows platform implementation for ModelSmith core (stdlib only).

Only OS-specific primitives live here: child-death guard, tree kill, system
memory, and the GPU file lock.  The shared core must import these names.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
from pathlib import Path

PLATFORM = "win"


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOB_BASIC_LIMIT(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JOB_EXTENDED_LIMIT(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOB_BASIC_LIMIT),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class ChildDeathGuard:
    def __init__(self, enabled=True):
        self.enabled = False
        self.reason = None
        self.handle = None
        if not enabled:
            self.reason = "disabled by --no-job-object"
            return
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.restype = ctypes.c_void_p
            kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            kernel32.SetInformationJobObject.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32
            ]
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                raise OSError(ctypes.get_last_error(), "CreateJobObjectW")
            info = _JOB_EXTENDED_LIMIT()
            info.BasicLimitInformation.LimitFlags = 0x00002000
            if not kernel32.SetInformationJobObject(
                handle, 9, ctypes.byref(info), ctypes.sizeof(info)
            ):
                raise OSError(ctypes.get_last_error(), "SetInformationJobObject")
            if not kernel32.AssignProcessToJobObject(handle, kernel32.GetCurrentProcess()):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject")
            self.handle = handle
            self.enabled = True
        except Exception as exc:
            self.reason = "%s: %s" % (type(exc).__name__, exc)
            if self.handle:
                try:
                    ctypes.WinDLL("kernel32").CloseHandle(self.handle)
                except Exception:
                    pass


def install_child_death_guard(enabled=True):
    return ChildDeathGuard(enabled=enabled)


def kill_tree(pid):
    if not pid:
        return
    try:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        pass


def memory_info(probe=None):
    try:
        if probe is not None:
            total_bytes, avail_bytes = probe()
        else:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            status = _MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                raise OSError(ctypes.get_last_error(), "GlobalMemoryStatusEx")
            total_bytes, avail_bytes = int(status.ullTotalPhys), int(status.ullAvailPhys)
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
    """Exclusive OS lock on a file; released by the OS if the process dies."""

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
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
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
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            try:
                fh.close()
            except OSError:
                pass
