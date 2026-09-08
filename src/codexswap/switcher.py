"""Reading, capturing and activating Codex credentials."""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Set

from . import errors, identity, paths
from .locking import FileLock
from .models import Account, AccountIdentity
from .store import AccountStore


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    name: str
    cmdline: str


def _windows_parents() -> Dict[int, int]:
    """Read parent PIDs without relying on the optional WMIC executable."""
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        function = getattr(kernel, name)
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        function.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise OSError("Cannot read process ancestry")
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        parents = {}
        success = kernel.Process32FirstW(handle, ctypes.byref(entry))
        if not success:
            raise OSError("Cannot read process ancestry")
        while success:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            success = kernel.Process32NextW(handle, ctypes.byref(entry))
        return parents
    finally:
        kernel.CloseHandle(handle)


def _ancestors(parents: Dict[int, int]) -> Set[int]:
    excluded = {os.getpid()}
    pid = os.getppid()
    while pid > 0 and pid not in excluded:
        excluded.add(pid)
        pid = parents.get(pid, 0)
    # Windows Python versions can report a different immediate parent than ps.
    pid = parents.get(os.getpid(), 0)
    while pid > 0 and pid not in excluded:
        excluded.add(pid)
        pid = parents.get(pid, 0)
    return excluded


def _process_output(command: List[str]) -> str:
    completed = subprocess.run(command, capture_output=True,
                               text=True, errors="replace", timeout=5, check=True)
    return completed.stdout


def _windows_processes() -> List[ProcessInfo]:
    output = _process_output(["tasklist", "/FO", "CSV", "/NH"])
    processes: Dict[int, ProcessInfo] = {}
    for row in csv.reader(io.StringIO(output)):
        if len(row) < 2 or row[0].strip().casefold() != "codex.exe":
            continue
        try:
            pid = int(row[1].strip().replace(",", ""))
        except ValueError:
            continue
        processes[pid] = ProcessInfo(pid, row[0].strip(), "")
    try:
        output = _process_output(
            ["wmic", "process", "get", "ProcessId,Name,CommandLine", "/format:csv"]
        )
        rows = csv.reader(io.StringIO(output))
        header = None
        for row in rows:
            if not row or not any(cell.strip() for cell in row):
                continue
            if header is None:
                fields = [cell.strip().lstrip("\ufeff").casefold() for cell in row]
                if {"processid", "name", "commandline"}.issubset(fields):
                    header = fields
                continue
            entry = dict(zip(header, row))
            if entry.get("name", "").strip().casefold() != "codex.exe":
                continue
            try:
                pid = int(entry.get("processid", "").strip())
            except ValueError:
                continue
            processes[pid] = ProcessInfo(pid, entry["name"].strip(),
                                         entry.get("commandline", ""))
    except Exception:
        # WMIC is optional; CIM still supplies command lines on newer Windows.
        try:
            output = _process_output([
                "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name = 'codex.exe'\" | "
                "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress",
            ])
            entries = json.loads(output) if output.strip() else []
            if isinstance(entries, dict):
                entries = [entries]
            for entry in entries:
                if (not isinstance(entry, dict)
                        or str(entry.get("Name", "")).casefold() != "codex.exe"):
                    continue
                try:
                    pid = int(entry.get("ProcessId", ""))
                except (ValueError, TypeError):
                    continue
                processes[pid] = ProcessInfo(pid, "codex.exe", entry.get("CommandLine") or "")
        except Exception:
            pass
    excluded = _ancestors(_windows_parents())
    return [process for pid, process in sorted(processes.items())
            if pid > 0 and pid not in excluded
            and "app-server" not in process.cmdline.casefold()]


def detect_running_codex() -> List[ProcessInfo]:
    try:
        if os.name == "nt":
            return _windows_processes()
        output = _process_output(["ps", "-eo", "pid=,comm=,args="])
        ancestry = _process_output(["ps", "-eo", "pid=,ppid="])
        parents = {}
        for line in ancestry.splitlines():
            parts = line.split()
            if len(parts) == 2:
                try:
                    parents[int(parts[0])] = int(parts[1])
                except ValueError:
                    continue
        excluded = _ancestors(parents)
        processes = []
        for line in output.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) < 2:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            name = os.path.basename(parts[1])
            cmdline = parts[2] if len(parts) == 3 else ""
            if (pid > 0 and pid not in excluded and name.casefold() in ("codex", "codex.exe")
                    and "app-server" not in cmdline.casefold()):
                processes.append(ProcessInfo(pid, name, cmdline))
        return sorted(processes, key=lambda process: process.pid)
    except Exception:
        # Process discovery is best effort, including missing tools or malformed output.
        return []


def current_live_identity() -> Optional[AccountIdentity]:
    try:
        return identity.identity_from_auth(identity.load_auth(paths.live_auth_path()))
    except (OSError, ValueError, TypeError, errors.AuthFileMissing, errors.AuthFileInvalid):
        return None


def capture_current(
    store: AccountStore, *, slot: Optional[int] = None, alias: Optional[str] = None,
) -> Account:
    try:
        auth = identity.load_auth(paths.live_auth_path())
    except errors.AuthFileMissing:
        raise errors.AuthFileMissing("No live authentication file; run codex login first") from None
    with FileLock(store._path(paths.lock_path())):
        account = store.add_from_auth(auth, slot=slot, alias=alias)
        if store.active_slot is None:
            store.set_active(account.slot)
        return account


def sync_live_to_slot(store: AccountStore) -> Optional[int]:
    if not paths.live_auth_path().exists():
        return None
    with FileLock(store._path(paths.lock_path())):
        try:
            auth = identity.load_auth(paths.live_auth_path())
        except errors.AuthFileMissing:
            return None
        derived = identity.identity_from_auth(auth)
        account = (store.find_by_account_id(derived.account_id)
                   if derived.account_id is not None else None)
        if account is None and derived.email is not None:
            account = store.find_by_email(derived.email)
        if account is None:
            return None
        paths.ensure_dir(store._path(paths.slot_home(account.slot)))
        paths.atomic_write_json(store._path(paths.slot_auth_path(account.slot)), auth, mode=0o600)
        account.identity = derived
        store.save()
        return account.slot


def activate(
    store: AccountStore, target: Account, *, force: bool = False, sync_back: bool = True,
) -> None:
    if not force:
        processes = detect_running_codex()
        if processes:
            running = ", ".join(f"{process.pid} {process.name}" for process in processes)
            raise errors.CodexRunning(f"Codex is running: {running}. Close them or pass --force")
    with FileLock(store._path(paths.lock_path())):
        target = store.get(target.slot)
        if sync_back:
            sync_live_to_slot(store)
        auth_path = store._path(paths.slot_auth_path(target.slot))
        if not auth_path.is_file():
            raise errors.AuthFileMissing(
                f"No auth file for slot {target.slot}; run codexswap add"
            )
        auth = identity.load_auth(auth_path)
        paths.ensure_dir(paths.codex_home())
        paths.atomic_write_json(paths.live_auth_path(), auth, mode=0o600)
        store.active_slot = target.slot
        target.last_switched_at = paths.iso_now()
        store.save()


def slot_env(slot: int, base_env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    env["CODEX_HOME"] = str(paths.slot_home(slot))
    return env


def run_as(store: AccountStore, account: Account, argv: Sequence[str]) -> int:
    from .appserver import find_codex_binary

    account = store.get(account.slot)
    binary = find_codex_binary()
    env = slot_env(account.slot)
    env["CODEX_HOME"] = str(store._path(paths.slot_home(account.slot)))
    return subprocess.call([binary] + list(argv), env=env)
