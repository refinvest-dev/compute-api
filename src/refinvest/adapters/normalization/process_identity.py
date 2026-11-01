"""OS process birth identity; a reused PID never identifies the original resource."""

from __future__ import annotations

import os
from pathlib import Path


def process_identity(pid: int) -> str | None:
    if pid <= 0:
        raise ValueError("Invalid process ID")
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [
            ctypes.POINTER(wintypes.FILETIME)
        ] * 4
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:
                return None
            raise OSError(error, "Process identity is unavailable")
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                raise OSError(ctypes.get_last_error(), "Process identity is unavailable")
            # A signalled process has exited, even if another handle still retains its PID.
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            # QUERY_LIMITED_INFORMATION does not grant SYNCHRONIZE; use the exit FILETIME instead.
            if times[1].dwHighDateTime or times[1].dwLowDateTime:
                return None
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except FileNotFoundError:
        return None
    if fields[0] in {"Z", "X"}:
        return None
    return fields[19]


def terminate_process(pid: int, expected_identity: str) -> None:
    """Terminate only the observed process, using an OS handle immune to PID reuse.

    Sending a signal is not absence evidence; callers must observe actual exit later.
    """
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [
            ctypes.POINTER(wintypes.FILETIME)
        ] * 4
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        handle = kernel.OpenProcess(0x1001, False, pid)  # QUERY_LIMITED_INFORMATION | TERMINATE
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:
                return
            raise OSError(error, "Process termination is unavailable")
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                raise OSError(ctypes.get_last_error(), "Process identity is unavailable")
            birth = str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
            if birth != expected_identity or times[1].dwHighDateTime or times[1].dwLowDateTime:
                return
            if not kernel.TerminateProcess(handle, 1):
                raise OSError(ctypes.get_last_error(), "Process termination failed")
        finally:
            kernel.CloseHandle(handle)
        return

    import signal

    # A pidfd pins the process before comparing /proc birth identity.
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        if process_identity(pid) == expected_identity:
            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
    except ProcessLookupError:
        pass
    finally:
        os.close(descriptor)
