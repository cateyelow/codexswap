"""Reading, capturing and activating Codex credentials."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
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


def detect_running_codex() -> Optional[List[ProcessInfo]]:
    """Codex processes that would fight over the live credential.

    Returns `None`, not an empty list, when discovery itself failed: "no Codex is
    running" and "the question could not be answered" must not look the same to a
    caller that is about to overwrite `auth.json`.
    """
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
        # A missing tool, a timeout or malformed output means the answer is unknown.
        # Reporting "nothing is running" here would let a switch overwrite the
        # credential a live Codex still holds, so callers must decide for themselves.
        return None


def describe_running(processes: Sequence[ProcessInfo], *, limit: int = 4) -> str:
    """One readable line, however many Codex processes are open."""
    shown = ", ".join(f"{process.pid} {process.name}" for process in processes[:limit])
    remaining = len(processes) - limit
    if remaining > 0:
        shown += f", and {remaining} more"
    return f"Codex is running: {shown}"


def _last_refresh(auth: Optional[dict]) -> Optional[float]:
    """Parse Codex's `last_refresh` stamp; None when absent or unreadable."""
    raw = auth.get("last_refresh") if isinstance(auth, dict) else None
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # Codex writes nanoseconds; fromisoformat accepts at most microseconds until 3.11.
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


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
        # Capture reads the LIVE credential, so the captured account is by definition
        # the one Codex is using. Leaving activeSlot pointing at the previous account
        # made the documented quick start (login A, add, login B, add) end with auto
        # and reset targeting A while B was live.
        store.set_active(account.slot)
        return account


def sync_live_to_slot(store: AccountStore) -> Optional[int]:
    if not paths.live_auth_path().exists():
        return None
    with FileLock(store._path(paths.lock_path())):
        # Adopt anything another process registered while we were deciding.
        store.reload()
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
        slot_auth = store._path(paths.slot_auth_path(account.slot))
        stored: Optional[dict] = None
        if slot_auth.is_file():
            with suppress(errors.AuthFileMissing, errors.AuthFileInvalid):
                stored = identity.load_auth(slot_auth)
        live_at, stored_at = _last_refresh(auth), _last_refresh(stored)
        if stored_at is not None and live_at is not None and stored_at > live_at:
            # A probe refreshed this slot after the live copy was written. Refresh
            # tokens rotate, so the older live copy may already be void: keep the
            # newer one rather than writing the account backwards.
            return account.slot
        paths.ensure_dir(store._path(paths.slot_home(account.slot)))
        paths.atomic_write_json(slot_auth, auth, mode=0o600)
        account.identity = derived
        store.save()
        return account.slot


def activate(
    store: AccountStore, target: Account, *, force: bool = False, sync_back: bool = True,
) -> None:
    if not force:
        processes = detect_running_codex()
        if processes is None:
            raise errors.CodexRunning(
                "Cannot determine whether Codex is running. Close Codex and retry, "
                "or pass --force"
            )
        if processes:
            raise errors.CodexRunning(f"{describe_running(processes)}. Close them or pass --force")
    with FileLock(store._path(paths.lock_path())):
        store.reload()
        target = store.get(target.slot)
        auth_path = store._path(paths.slot_auth_path(target.slot))
        if not auth_path.is_file():
            raise errors.AuthFileMissing(
                f"No auth file for slot {target.slot}; run codexswap add"
            )
        # Validate before anything is written. An auth file that parses but holds no
        # credential must not cost the user their live login, and must not trigger a
        # sync-back that the failed switch then leaves half applied.
        auth = identity.validate_auth(
            identity.load_auth(auth_path), source=f"Slot {target.slot} auth file"
        )
        if sync_back and sync_live_to_slot(store) == target.slot:
            # Sync-back just rewrote this slot; activate the bytes it left behind.
            auth = identity.validate_auth(
                identity.load_auth(auth_path), source=f"Slot {target.slot} auth file"
            )
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


def pick_target(candidates, *, current_slot, strategy, threshold, hysteresis):
    from . import strategy as selection

    candidates = list(candidates)
    ordinary = [candidate for candidate in candidates
                if candidate.account.identity.auth_mode != "apikey"
                and selection.eligible(candidate, current_slot=current_slot,
                                       threshold=threshold, hysteresis=hysteresis)]
    return selection.pick_target(ordinary or candidates, current_slot=current_slot,
                                 strategy=strategy, threshold=threshold, hysteresis=hysteresis)
