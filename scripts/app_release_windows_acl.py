"""Checked native ownership changes for the trusted release snapshot only.

Unlike icacls /C, native security APIs report each failed ownership change.
The runner must already hold SeRestorePrivilege; no elevation is requested.
"""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from pathlib import Path


class Luid(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.LONG)]


class LuidAttributes(ctypes.Structure):
    _fields_ = [("luid", Luid), ("attributes", wintypes.DWORD)]


class TokenPrivileges(ctypes.Structure):
    _fields_ = [("count", wintypes.DWORD), ("privileges", LuidAttributes * 1)]


def native():
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    security.LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(Luid)]
    security.AdjustTokenPrivileges.argtypes = [wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(TokenPrivileges),
        wintypes.DWORD, ctypes.POINTER(TokenPrivileges), ctypes.POINTER(wintypes.DWORD)]
    security.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(pointer), pointer, pointer, pointer, ctypes.POINTER(pointer)]
    security.GetNamedSecurityInfoW.restype = wintypes.DWORD
    security.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        pointer, pointer, pointer, pointer]
    security.SetNamedSecurityInfoW.restype = wintypes.DWORD
    security.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(pointer)]
    security.ConvertSidToStringSidW.argtypes = [pointer, ctypes.POINTER(pointer)]
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR,
        wintypes.DWORD, ctypes.POINTER(pointer), ctypes.POINTER(wintypes.DWORD)]
    security.GetSecurityDescriptorDacl.argtypes = [pointer, ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(pointer), ctypes.POINTER(wintypes.BOOL)]
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    return security, kernel


@contextmanager
def restore_privilege(security, kernel):
    token = wintypes.HANDLE()
    # GetCurrentProcess's documented pseudo-handle, without pointer truncation.
    if not security.OpenProcessToken(wintypes.HANDLE(-1), 0x28, ctypes.byref(token)):
        raise RuntimeError("Runner process token could not be opened for snapshot ownership")
    previous = TokenPrivileges()
    changed = False
    try:
        luid = Luid()
        if not security.LookupPrivilegeValueW(None, "SeRestorePrivilege", ctypes.byref(luid)):
            raise RuntimeError("Windows restore privilege is unavailable")
        requested = TokenPrivileges(1, (LuidAttributes * 1)(LuidAttributes(luid, 2)))
        size = wintypes.DWORD()
        ctypes.set_last_error(0)
        ok = security.AdjustTokenPrivileges(token, False, ctypes.byref(requested), ctypes.sizeof(previous),
                                           ctypes.byref(previous), ctypes.byref(size))
        if not ok or ctypes.get_last_error():
            raise RuntimeError("Windows runner lacks the existing restore privilege required for SYSTEM ownership")
        changed = True
        yield
    finally:
        try:
            if changed and not security.AdjustTokenPrivileges(token, False, ctypes.byref(previous), 0, None, None):
                raise RuntimeError("Snapshot ownership privilege could not be restored")
        finally:
            kernel.CloseHandle(token)


def owner_sid(path):
    security, kernel = native()
    owner, descriptor, text = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    status = security.GetNamedSecurityInfoW(str(path), 1, 1, ctypes.byref(owner), None, None, None,
                                          ctypes.byref(descriptor))
    if status:
        raise RuntimeError("Actual Windows snapshot owner could not be inspected")
    try:
        if not security.ConvertSidToStringSidW(owner, ctypes.byref(text)):
            raise RuntimeError("Windows snapshot owner SID could not be decoded")
        return ctypes.wstring_at(text)
    finally:
        if text:
            kernel.LocalFree(text)
        kernel.LocalFree(descriptor)


def set_owners(destination, sid):
    paths = [Path(destination), *Path(destination).rglob("*")]
    if any(path.is_symlink() or path.is_junction() for path in paths):
        raise RuntimeError("Ownership changes cannot follow snapshot links")
    pending = [path for path in paths if owner_sid(path) != sid]
    if not pending:
        return
    security, kernel = native()
    owner = ctypes.c_void_p()
    if not security.ConvertStringSidToSidW(sid, ctypes.byref(owner)):
        raise RuntimeError("Requested snapshot owner SID is invalid")
    try:
        with restore_privilege(security, kernel):
            for path in pending:
                if security.SetNamedSecurityInfoW(str(path), 1, 1, owner, None, None, None):
                    raise RuntimeError("Native Windows snapshot ownership assignment failed")
                if owner_sid(path) != sid:
                    raise RuntimeError("Windows snapshot owner does not match the required SID")
    finally:
        kernel.LocalFree(owner)


def set_permissions(destination, sid, *, readonly):
    """Replace the complete DACL; do not retain image-specific explicit grants."""
    security, kernel = native()
    paths = sorted(Path(destination).rglob("*"), key=lambda path: len(path.parts), reverse=True)
    for path in [*paths, Path(destination)]:
        if path.is_symlink() or path.is_junction():
            raise RuntimeError("Snapshot permissions cannot follow links")
        inherit = "OICI" if path.is_dir() else ""
        rights = "FRFX" if readonly else "FA"
        sddl = f"D:P(A;{inherit};FA;;;SY)(A;{inherit};{rights};;;{sid})"
        descriptor, acl = ctypes.c_void_p(), ctypes.c_void_p()
        if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            raise RuntimeError("Exact snapshot permissions could not be constructed")
        try:
            present, defaulted = wintypes.BOOL(), wintypes.BOOL()
            if not security.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)) or not present:
                raise RuntimeError("Exact snapshot DACL is unavailable")
            if security.SetNamedSecurityInfoW(str(path), 1, 0x80000004, None, None, acl, None):
                raise RuntimeError("Exact protected Windows snapshot DACL could not be applied")
        finally:
            kernel.LocalFree(descriptor)
