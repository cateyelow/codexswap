"""Reading, capturing and activating Codex credentials."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import subprocess
from contextlib import suppress
from dataclasses import dataclass, replace
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
    rows_understood = 0
    for row in csv.reader(io.StringIO(output)):
        if len(row) < 2:
            continue
        try:
            pid = int(row[1].strip().replace(",", ""))
        except ValueError:
            continue
        rows_understood += 1
        if row[0].strip().casefold() != "codex.exe":
            continue
        processes[pid] = ProcessInfo(pid, row[0].strip(), "")
    if not rows_understood:
        # tasklist always lists at least itself. Zero readable rows means the output
        # was not a task listing, and "no Codex is running" would be a guess.
        raise OSError("tasklist produced no readable rows")
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
        rows_understood = 0
        for line in output.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) < 2:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            rows_understood += 1
            name = os.path.basename(parts[1])
            cmdline = parts[2] if len(parts) == 3 else ""
            if (pid > 0 and pid not in excluded and name.casefold() in ("codex", "codex.exe")
                    and "app-server" not in cmdline.casefold()):
                processes.append(ProcessInfo(pid, name, cmdline))
        if not rows_understood:
            # ps always lists at least itself. Zero readable rows means the command
            # answered with something that is not a process listing.
            raise OSError("ps produced no readable rows")
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
    # CONTRACT: no credential write without a usable credential. Capturing an auth
    # file that cannot authenticate would register a slot that can never be switched
    # to, and re-capturing over a working slot would destroy the working copy.
    identity.validate_auth(auth, source="the live authentication file")
    with FileLock(store.path_for(paths.lock_path())):
        account = store.add_from_auth(auth, slot=slot, alias=alias)
        # Capture reads the LIVE credential, so the captured account is by definition
        # the one Codex is using. Leaving activeSlot pointing at the previous account
        # made the documented quick start (login A, add, login B, add) end with auto
        # and reset targeting A while B was live.
        store.set_active(account.slot)
        return account


def _stored_auth(store: AccountStore, slot: int) -> Optional[dict]:
    with suppress(errors.AuthFileMissing, errors.AuthFileInvalid, OSError, ValueError):
        return identity.load_auth(store.path_for(paths.slot_auth_path(slot)))
    return None


def slot_owning(store: AccountStore, auth: dict) -> Optional[Account]:
    """The slot that already holds this credential, or None. Callers hold the lock.

    Every match is confirmed against the slot's own auth file, never against the
    registry's recorded identity. The registry is a cache that drifts; the file is
    the thing that would still hold the credential after the live copy is gone, and
    the only question here is whether a copy survives.

    The order matters because the answer decides whether a login is about to be
    destroyed:

    * identical bytes: unambiguous, and the only handle a credential that carries
      neither an account id nor an email ever has;
    * account id: authoritative when both sides have one. An email is deliberately
      not consulted then, so one person's login cannot pass as another's just
      because a slot was labelled with the same address;
    * API key: the key itself, whatever the registry thinks the slot's mode is;
    * email: only when neither side has an account id, so an API key's assigned
      label can never stand in for an OAuth login.
    """
    if not isinstance(auth, dict) or not auth:
        return None
    derived = identity.identity_from_auth(auth)
    key = auth.get("OPENAI_API_KEY")
    for account in store.ordered():
        stored = _stored_auth(store, account.slot)
        if not isinstance(stored, dict) or not stored:
            continue
        if stored == auth:
            return account
        mine = identity.identity_from_auth(stored)
        if derived.account_id and mine.account_id == derived.account_id:
            return account
        if isinstance(key, str) and key and stored.get("OPENAI_API_KEY") == key:
            return account
        if (not derived.account_id and not mine.account_id and derived.email
                and mine.email and mine.email.casefold() == derived.email.casefold()):
            return account
    return None


def live_credential_is_registered(store: AccountStore) -> bool:
    """Whether the live auth file is already saved in some slot.

    `activate` overwrites the live file, so a credential no slot holds is destroyed
    by the switch. A `codex login` that was never followed by `codexswap add` is
    exactly that case, and it is the one where the user has no other copy.

    A file that will not parse answers False: this cannot tell a half-written file
    from a format it does not understand, and only one of those is safe to discard.
    A file that parses but carries no credential answers True, because there is
    nothing in it to lose.
    """
    try:
        auth = identity.load_auth(paths.live_auth_path())
    except errors.AuthFileMissing:
        return True
    except (OSError, ValueError, errors.AuthFileInvalid):
        return False
    try:
        identity.validate_auth(auth)
    except errors.CodexSwapError:
        return True
    return slot_owning(store, auth) is not None


def sync_live_to_slot(store: AccountStore) -> Optional[int]:
    if not paths.live_auth_path().exists():
        return None
    with FileLock(store.path_for(paths.lock_path())):
        # Adopt anything another process registered while we were deciding.
        store.reload()
        try:
            auth = identity.load_auth(paths.live_auth_path())
        except (errors.AuthFileMissing, errors.AuthFileInvalid):
            # A file that will not parse has nothing to sync back. It is not
            # discarded: activate rescues the bytes before it overwrites them.
            return None
        try:
            # Sync-back exists to preserve a refreshed credential. An unusable live
            # file has nothing to preserve, and writing it would destroy the slot's
            # own working copy, which may be the only one left.
            identity.validate_auth(auth, source="the live authentication file")
        except errors.AuthFileInvalid:
            return None
        derived = identity.identity_from_auth(auth)
        account = slot_owning(store, auth)
        if account is None:
            return None
        if derived.auth_mode == "apikey":
            # An API key carries no email and no plan of its own. The label given at
            # registration is the only identity it has, and writing None over it
            # would make the account unfindable by the name the user gave it.
            derived = replace(derived, email=account.identity.email, plan_type="api key")
        slot_auth = store.path_for(paths.slot_auth_path(account.slot))
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
        paths.ensure_dir(store.path_for(paths.slot_home(account.slot)))
        paths.atomic_write_json(slot_auth, auth, mode=0o600)
        account.identity = derived
        store.save()
        return account.slot


_UNREGISTERED = "live login belonged to no registered account"
_UNREADABLE = "live login could not be parsed and was about to be overwritten"


def rescue_unregistered_live(store: AccountStore) -> Optional[str]:
    """Preserve the live credential when no slot holds it. Callers hold the lock.

    Returns the stash id, or None when there was nothing worth preserving. Raising
    means the credential could not be preserved, and the caller must then leave it
    alone: the whole point is that overwriting is irreversible.

    Called as late as possible, immediately before the overwrite, because the file
    belongs to Codex and Codex may be writing it.
    """
    from . import unclaimed

    path = paths.live_auth_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return None
    except (OSError, UnicodeError):
        raise errors.UserError(
            "The live Codex login cannot be read, so switching would overwrite "
            "something that could not be preserved first"
        ) from None
    try:
        auth = json.loads(raw)
    except ValueError:
        # A file caught mid-write and one written by a Codex release this version
        # does not understand look identical from here. Keep the bytes.
        return unclaimed.stash_raw(raw, reason=_UNREADABLE)
    try:
        identity.validate_auth(auth)
    except errors.CodexSwapError:
        return None
    if slot_owning(store, auth) is not None:
        return None
    return unclaimed.stash(auth, reason=_UNREGISTERED)


def activate(
    store: AccountStore, target: Account, *, force: bool = False, sync_back: bool = True,
) -> Optional[str]:
    """Make `target` the live account. Returns a rescue id when one was needed.

    A login the registry does not know is copied into the unclaimed stash before it
    is overwritten, rather than refused. Refusing would be safe for the credential
    and useless for the user; overwriting would be the opposite.
    """
    if not force:
        processes = detect_running_codex()
        if processes is None:
            raise errors.CodexRunning(
                "Cannot determine whether Codex is running. Close Codex and retry, "
                "or pass --force"
            )
        if processes:
            raise errors.CodexRunning(f"{describe_running(processes)}. Close them or pass --force")
    with FileLock(store.path_for(paths.lock_path())):
        store.reload()
        target = store.get(target.slot)
        auth_path = store.path_for(paths.slot_auth_path(target.slot))
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
        # Last thing before the overwrite, so the window in which Codex could finish
        # writing a login this check did not see is as small as it can be made.
        rescued = rescue_unregistered_live(store)
        paths.ensure_dir(paths.codex_home())
        paths.atomic_write_json(paths.live_auth_path(), auth, mode=0o600)
        store.active_slot = target.slot
        target.last_switched_at = paths.iso_now()
        store.save()
        return rescued


def slot_env(slot: int, base_env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    env["CODEX_HOME"] = str(paths.slot_home(slot))
    return env


def run_as(store: AccountStore, account: Account, argv: Sequence[str]) -> int:
    from .appserver import find_codex_binary

    account = store.get(account.slot)
    binary = find_codex_binary()
    env = slot_env(account.slot)
    env["CODEX_HOME"] = str(store.path_for(paths.slot_home(account.slot)))
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
