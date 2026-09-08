"""Storage paths and safe IO; every credential write uses the atomic helpers."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

from . import errors

_home_override: Optional[Path] = None


def set_home_override(path: Union[str, Path]) -> None:
    global _home_override
    _home_override = Path(path).expanduser()


def clear_home_override() -> None:
    global _home_override
    _home_override = None


def codexswap_home() -> Path:
    if _home_override is not None:
        return _home_override
    return Path(os.environ.get("CODEXSWAP_HOME") or "~/.codexswap").expanduser()


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser()


def live_auth_path() -> Path:
    return codex_home() / "auth.json"


def homes_dir() -> Path:
    return codexswap_home() / "homes"


def slot_home(slot: int) -> Path:
    return homes_dir() / str(slot)


def slot_auth_path(slot: int) -> Path:
    return slot_home(slot) / "auth.json"


def accounts_path() -> Path:
    return codexswap_home() / "accounts.json"


def settings_path() -> Path:
    return codexswap_home() / "settings.json"


def state_path() -> Path:
    return codexswap_home() / "state.json"


def mappings_path() -> Path:
    return codexswap_home() / "mappings.json"


def usage_cache_path() -> Path:
    return codexswap_home() / "usage-cache.json"


def log_path() -> Path:
    return codexswap_home() / "codexswap.log"


def lock_path() -> Path:
    return codexswap_home() / ".lock"


def ensure_dir(path: Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    path = Path(path)
    ensure_dir(path.parent)
    temporary: Optional[Path] = None
    replaced = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix="." + path.name + ".", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        replaced = True
    finally:
        # os.replace consumes the temp file on success; clean up only when it did not.
        if temporary is not None and not replaced:
            with contextlib.suppress(OSError):
                # Preserve the original write error if cleanup also fails.
                temporary.unlink()


def atomic_write_json(
    path: Path, obj: Any, *, mode: int = 0o600, indent: int = 2,
) -> None:
    atomic_write_text(
        path, json.dumps(obj, ensure_ascii=False, indent=indent) + "\n", mode=mode,
    )


def read_json(path: Path, default: Any = None) -> Any:
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, UnicodeDecodeError):
        # CONTRACT: only auth.json receives the credential-specific error type.
        if path.name.casefold() == "auth.json":
            raise errors.AuthFileInvalid(
                f"Invalid JSON in authentication file: {path}"
            ) from None
        raise


def read_json_tolerant(path: Path, default: Any) -> Any:
    try:
        return read_json(path, default)
    except (OSError, ValueError, errors.AuthFileInvalid):
        return default


def quarantine_corrupt(path: Path) -> Path:
    path = Path(path)
    destination = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
    path.rename(destination)
    return destination


def secure_chmod(path: Path, mode: int = 0o600) -> None:
    with contextlib.suppress(OSError):
        os.chmod(path, mode)


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
