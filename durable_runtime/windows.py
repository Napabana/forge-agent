"""Windows process identity based on the process creation FILETIME."""

import ctypes
import os
import re
import subprocess
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path

from .store import OwnerConflict


@lru_cache(maxsize=1)
def current_user_sid() -> str:
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "whoami.exe"
    output = subprocess.check_output(
        [str(executable), "/user", "/fo", "csv", "/nh"], timeout=5,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    sid = re.search(rb'"(S-1-(?:[0-9]+-)*[0-9]+)"\s*$', output)
    if sid is None:
        raise OSError("Windows account SID could not be verified")
    return sid[1].decode("ascii")


def restrict_access(path: Path, *, directory: bool = False) -> None:
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    flags = "OICI" if directory else ""
    sddl = f"D:P(A;{flags};FA;;;{current_user_sid()})"
    descriptor = ctypes.c_void_p()
    convert = api.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), wintypes.LPDWORD)
    convert.restype = wintypes.BOOL
    if not convert(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present, defaulted, acl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
        get_acl = api.GetSecurityDescriptorDacl
        get_acl.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
                            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL))
        get_acl.restype = wintypes.BOOL
        if not get_acl(descriptor, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not present.value or not acl.value:
            raise OSError("private Windows ACL could not be constructed")
        set_acl = api.SetNamedSecurityInfoW
        set_acl.argtypes = (wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD) + (ctypes.c_void_p,) * 4
        set_acl.restype = wintypes.DWORD
        status = set_acl(str(path), 1, 0x80000004, None, None, acl, None)
        if status:
            raise ctypes.WinError(status)
    finally:
        free = ctypes.WinDLL("kernel32").LocalFree
        free.argtypes = (ctypes.c_void_p,)
        free.restype = ctypes.c_void_p
        free(descriptor)


def process_creation_time(pid: int) -> int | None:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    process = kernel.OpenProcess(0x00101000, False, pid)  # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER, PID no longer exists
            return None
        raise OwnerConflict("previous Windows process identity could not be verified")
    try:
        state = kernel.WaitForSingleObject(process, 0)
        if state == 0:  # WAIT_OBJECT_0: a terminated process can still have an open handle
            return None
        if state != 258:  # WAIT_TIMEOUT
            raise OwnerConflict("Windows process liveness could not be verified")
        created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(process, *(ctypes.byref(t) for t in (created, exited, kernel_time, user_time))):
            raise OwnerConflict("Windows process times could not be verified")
        # Unix microseconds fit exactly in the existing SQLite REAL column.
        return (((created.dwHighDateTime << 32) | created.dwLowDateTime) - 116444736000000000) // 10
    finally:
        kernel.CloseHandle(process)
