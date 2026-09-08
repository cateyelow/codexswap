"""Directory to account-slot mappings."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from . import errors, paths
from .locking import FileLock


def normalise_path(p: Union[str, Path]) -> str:
    # CONTRACT: preserve the separator of a filesystem root so it stays absolute.
    result = os.path.normpath(str(Path(p).expanduser().resolve()))
    return result.casefold() if os.name == "nt" else result


def _mapping_path(root: Optional[Path]) -> Path:
    return (Path(root).expanduser() / "mappings.json"
            if root is not None else paths.mappings_path())


def load_mappings(root: Optional[Path] = None) -> Dict[str, int]:
    data = paths.read_json_tolerant(_mapping_path(root), {})
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items()
            if isinstance(key, str) and type(value) is int and value > 0}


def save_mappings(m: Dict[str, int], root: Optional[Path] = None) -> None:
    destination = _mapping_path(root)
    lock = destination.parent / ".lock" if root is not None else paths.lock_path()
    with FileLock(lock):
        paths.atomic_write_json(destination, m, mode=0o644)


def set_mapping(path: Union[str, Path], slot: int) -> None:
    if type(slot) is not int or slot <= 0:
        raise errors.AccountNotFound("Slot must be a positive integer")
    with FileLock(paths.lock_path()):
        mappings = load_mappings()
        mappings[normalise_path(path)] = slot
        save_mappings(mappings)


def remove_mapping(path: Union[str, Path]) -> bool:
    with FileLock(paths.lock_path()):
        mappings = load_mappings()
        key = normalise_path(path)
        if key not in mappings:
            return False
        del mappings[key]
        save_mappings(mappings)
        return True


def lookup(path: Union[str, Path]) -> Optional[int]:
    mappings = load_mappings()
    current = normalise_path(path)
    while True:
        if current in mappings:
            return mappings[current]
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def all_mappings() -> List[Tuple[str, int]]:
    return sorted(load_mappings().items())
