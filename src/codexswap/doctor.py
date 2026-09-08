from __future__ import annotations

import errno
import os
import platform
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__, appserver, errors, identity, locking, paths, switcher
from .models import HEALTH_OK, Account
from .settings import SPECS


def _existing_parent(path: Path) -> Path:
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def _lock_check(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {"status": "ok", "detail": "absent (unlocked)"}
    if locking._backend == "noop":
        return {"status": "warn", "detail": "present; no OS locking backend available"}
    key = os.path.normcase(str(path.resolve()))
    if key in locking._locks:
        return {"status": "warn", "detail": "held by this process"}
    try:
        # Inspect the existing inode without creating, truncating, or removing it.
        with path.open("r+b") as handle:
            locking._acquire(handle)
            locking._release(handle)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            return {"status": "warn", "detail": "busy or inaccessible"}
        return {"status": "fail", "detail": "cannot inspect lock file"}
    return {"status": "ok", "detail": "present, unlocked"}


def _settings_checks(root: Path) -> List[Dict[str, str]]:
    path = root / "settings.json"
    try:
        data = paths.read_json(path, {})
        if not isinstance(data, dict):
            raise ValueError
    except (OSError, ValueError):
        return [{"name": "settings", "status": "fail", "detail": "invalid or unreadable settings.json"}]
    invalid = []
    unknown = []
    groups = {key.split(".")[0] for key in SPECS}
    for group, section in data.items():
        if group not in groups:
            unknown.append(group)
            continue
        if not isinstance(section, dict):
            invalid.append(group)
            continue
        for name, value in section.items():
            key = group + "." + name
            spec = SPECS.get(key)
            if spec is None:
                unknown.append(key)
                continue
            expected = {"int": int, "bool": bool, "str": str}[spec.type]
            valid = type(value) is expected
            if valid and spec.choices is not None:
                valid = value in spec.choices
            if valid and spec.minimum is not None:
                valid = spec.minimum <= value <= spec.maximum
            if not valid:
                invalid.append(key)
    checks = [{"name": "settings", "status": "fail" if invalid else "ok",
               "detail": ("invalid values: " + ", ".join(sorted(invalid)) if invalid
                          else "valid" if path.exists() else "absent (defaults apply)")}]
    checks.append({"name": "settings.unknown", "status": "warn" if unknown else "ok",
                   "detail": "unrecognised keys: " + ", ".join(sorted(unknown)) if unknown else "none"})
    return checks


def collect_checks() -> Dict[str, Any]:
    """Diagnose local state without modifying it or reading account usage."""
    checks: List[Dict[str, str]] = []
    secrets = {value for key, value in os.environ.items() if value and any(
        part in key.upper() for part in ("TOKEN", "API_KEY", "PASSWORD")
    )}

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    def remember(auth: Dict[str, Any]) -> None:
        values = [auth.get("OPENAI_API_KEY")]
        tokens = auth.get("tokens")
        if isinstance(tokens, dict):
            values.extend(tokens.values())
        secrets.update(value for value in values if isinstance(value, str) and value)

    add("codexswap", "ok", __version__)
    add("python", "ok", platform.python_version())
    add("platform", "ok", platform.platform())
    root = paths.codexswap_home().resolve()
    live = paths.codex_home().resolve()
    for name, directory in (("CODEXSWAP_HOME", root), ("CODEX_HOME", live)):
        try:
            exists = directory.exists()
            writable = os.access(_existing_parent(directory), os.W_OK | os.X_OK)
            valid = not exists or directory.is_dir()
            status = "fail" if not writable or not valid else "ok" if exists else "warn"
            add(name, status, f"{directory}; exists={exists}; writable={writable}"
                + ("; not a directory" if not valid else ""))
        except OSError:
            add(name, "fail", f"{directory}; cannot inspect directory")

    # CONTRACT: doctor reads the registry directly so corrupt files are not quarantined.
    accounts: List[Account] = []
    try:
        data = paths.read_json(root / "accounts.json", {"version": 1, "accounts": []})
        if (not isinstance(data, dict) or type(data.get("version")) is not int
                or data["version"] != 1 or not isinstance(data.get("accounts"), list)):
            raise ValueError
        seen = set()
        for entry in data["accounts"]:
            if not isinstance(entry, dict):
                raise ValueError
            slot = entry.get("slot")
            if type(slot) is not int or slot <= 0 or slot in seen:
                raise ValueError
            accounts.append(Account.from_dict(entry))
            seen.add(slot)
        active = data.get("activeSlot")
        if active is not None and (type(active) is not int or active not in seen):
            raise ValueError
        add("accounts", "ok" if accounts else "warn", f"{len(accounts)} configured")
    except (OSError, ValueError, TypeError):
        add("accounts", "fail", "invalid or unreadable accounts.json")

    auths: Dict[int, Dict[str, Any]] = {}
    for account in sorted(accounts, key=lambda item: item.slot):
        name = f"slot.{account.slot}.auth"
        path = root / "homes" / str(account.slot) / "auth.json"
        try:
            auth = identity.load_auth(path)
            remember(auth)
            derived = identity.identity_from_auth(auth)
            health = identity.health_of(derived, now=time.time())
            if derived.auth_mode == "apikey":
                key = auth.get("OPENAI_API_KEY")
                if not isinstance(key, str) or not key.strip():
                    health = "unknown"
            auths[account.slot] = auth
            add(name, "ok" if health == HEALTH_OK else "fail",
                f"present, parses; auth_mode={derived.auth_mode}; health={health}")
            if os.name != "nt":
                mode = stat.S_IMODE(path.stat().st_mode)
                add(f"slot.{account.slot}.mode", "warn" if mode & 0o077 else "ok",
                    f"{mode:#05o}" + ("; expected private permissions (0600)" if mode & 0o077 else ""))
        except errors.AuthFileMissing:
            add(name, "fail", "auth.json missing; health=unknown")
        except (OSError, ValueError, errors.AuthFileInvalid):
            add(name, "fail", "auth.json unreadable or invalid; health=unknown")

    try:
        auth = identity.load_auth(live / "auth.json")
        remember(auth)
        derived = identity.identity_from_auth(auth)
        matches = []
        for slot, saved in auths.items():
            other = identity.identity_from_auth(saved)
            if derived.auth_mode == "apikey":
                same = (other.auth_mode == "apikey" and bool(auth.get("OPENAI_API_KEY"))
                        and auth.get("OPENAI_API_KEY") == saved.get("OPENAI_API_KEY"))
            else:
                same = (bool(derived.account_id) and derived.account_id == other.account_id
                        or bool(derived.email) and bool(other.email)
                        and derived.email.casefold() == other.email.casefold())
            if same:
                matches.append(str(slot))
        add("live.auth", "ok" if matches else "warn",
            "present, parses; matches " + ("slot " + ", ".join(matches) if matches else "no slot"))
    except errors.AuthFileMissing:
        add("live.auth", "warn", "auth.json absent")
    except (OSError, ValueError, errors.AuthFileInvalid):
        add("live.auth", "fail", "auth.json unreadable or invalid")

    checks.extend(_settings_checks(root))
    checks.append({"name": "lock", **_lock_check(root / ".lock")})
    processes = switcher.detect_running_codex()
    # Command lines can contain credentials; show only numeric process IDs.
    if processes is None:
        add("processes", "warn", "cannot determine whether Codex is running")
    elif processes:
        add("processes", "warn",
            "running Codex PIDs: " + ", ".join(str(item.pid) for item in processes))
    else:
        add("processes", "ok", "no running Codex sessions detected")
    try:
        free = shutil.disk_usage(_existing_parent(root)).free
        # CONTRACT: below 100 MiB is a warning; only an unreadable volume fails.
        add("disk", "warn" if free < 100 * 1024 * 1024 else "ok",
            f"{free} bytes free ({free / (1024 ** 3):.2f} GiB)")
    except OSError:
        add("disk", "fail", "cannot determine free disk space")

    binary: Optional[str] = None
    try:
        binary = appserver.find_codex_binary()
    except (OSError, errors.CodexSwapError):
        add("codex.binary", "fail", "Codex executable not found; install Codex or set CODEX_BIN")
    if binary is None:
        add("app-server", "fail", "cannot initialize without a Codex executable")
    else:
        try:
            with tempfile.TemporaryDirectory(prefix="codexswap-doctor-") as temporary:
                env = dict(os.environ, CODEX_HOME=temporary)
                try:
                    result = subprocess.run([binary, "--version"], env=env, capture_output=True,
                                            text=True, encoding="utf-8", errors="replace", timeout=5)
                    version = result.stdout.strip()
                    add("codex.binary", "ok" if result.returncode == 0 and version else "fail",
                        f"{binary}; {version}" if result.returncode == 0 and version
                        else f"{binary}; --version failed (exit {result.returncode})")
                except (OSError, subprocess.SubprocessError):
                    add("codex.binary", "fail", f"{binary}; --version could not complete within 5s")
                try:
                    # __enter__ performs initialize + initialized only. No usage or consume call.
                    with appserver.AppServerClient(Path(temporary), timeout=5, codex_bin=binary):
                        pass
                    add("app-server", "ok", "initialize answered in a throwaway CODEX_HOME")
                except Exception as exc:
                    add("app-server", "fail", f"initialize failed ({type(exc).__name__}; timeout 5s)")
        except OSError:
            add("app-server", "fail", "cannot create or clean up throwaway CODEX_HOME")

    # Redact before returning either representation, including externally supplied diagnostics.
    for check in checks:
        for field in ("name", "detail"):
            value = check[field]
            for secret in sorted(secrets, key=len, reverse=True):
                value = value.replace(secret, "[redacted]")
            value = appserver._CREDENTIAL.sub("[redacted]", value)
            check[field] = " ".join(value.split())
    return {"checks": checks}
