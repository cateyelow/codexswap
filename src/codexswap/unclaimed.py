"""Rescued credentials: logins that were about to be overwritten and had no home.

`switcher.activate` writes the target account's credential over the live `auth.json`.
When the file it is about to replace belongs to no registered account -- a `codex
login` the user never ran `codexswap add` on, or something outside this tool rewriting
the live login -- overwriting it destroys the only copy. An OAuth authorisation code
is single-use, so recovering means authorising the account again from scratch.

So the switch parks a copy here first and then proceeds. Refusing the switch outright
would be safe for the credential and useless for the user; losing it would be the
opposite. This is the third option.

Every entry file holds a real credential. It is written `0600`, it is never printed,
and only `--claim` ever reads the `auth` member back out.
"""

from __future__ import annotations

# CONTRACT: Preserve the documented typing annotations for Python 3.9 callers.
# ruff: noqa: UP006, UP007, UP035
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import errors, identity, paths


@dataclass(frozen=True)
class Entry:
    """What can be shown about a stashed credential. Never the credential itself."""

    id: str
    stashed_at: str
    reason: str
    email: Optional[str]
    account_id: Optional[str]
    plan_type: Optional[str]
    auth_mode: Optional[str]
    fingerprint: str
    # False for bytes that did not parse as a credential. Kept anyway, because
    # "this release cannot read it" is not the same as "it is worthless".
    claimable: bool = True

    def label(self) -> str:
        """A name for a listing. The id is printed beside it, so it is not repeated."""
        if not self.claimable:
            return "unreadable login"
        if self.email:
            return self.email
        return "api key" if self.auth_mode == "apikey" else "unidentified login"


def directory(root: Optional[Path] = None) -> Path:
    return (Path(root) if root is not None else paths.codexswap_home()) / "unclaimed"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint(auth: Dict[str, Any]) -> str:
    """A stable name for a credential that reveals nothing about it.

    Used to recognise a credential already stashed, so a live file that keeps being
    rewritten by something outside this tool cannot fill the directory with copies.
    """
    return _digest(json.dumps(auth, sort_keys=True, ensure_ascii=False,
                              separators=(",", ":")))


def _entry_from_document(document: Any, *, entry_id: str) -> Optional[Entry]:
    if not isinstance(document, dict):
        return None
    claimable = isinstance(document.get("auth"), dict)
    if not claimable and not isinstance(document.get("raw"), str):
        return None

    def text(key: str) -> Optional[str]:
        value = document.get(key)
        return value if isinstance(value, str) and value else None

    return Entry(
        id=entry_id,
        stashed_at=text("stashedAt") or "",
        reason=text("reason") or "",
        email=text("email"),
        account_id=text("accountId"),
        plan_type=text("planType"),
        auth_mode=text("authMode"),
        fingerprint=text("fingerprint") or "",
        claimable=claimable,
    )


def entries(root: Optional[Path] = None) -> List[Entry]:
    """Every readable stash entry, oldest first. Unreadable files are skipped."""
    home = directory(root)
    found: List[Entry] = []
    try:
        candidates = sorted(home.glob("*.json"))
    except OSError:
        return found
    for path in candidates:
        document = paths.read_json_tolerant(path, None)
        entry = _entry_from_document(document, entry_id=path.stem)
        if entry is not None:
            found.append(entry)
    return found


def stash(auth: Dict[str, Any], *, reason: str, root: Optional[Path] = None) -> str:
    """Park a copy of `auth` and return its entry id.

    Raises rather than returning on failure: the caller is about to overwrite the
    original, and a stash that silently did nothing would make that destructive.
    """
    if not isinstance(auth, dict) or not auth:
        raise errors.UserError("Refusing to stash an empty credential")
    home = directory(root)
    mark = fingerprint(auth)
    for existing in entries(root):
        # The same credential arriving twice is one rescue, not two.
        if existing.fingerprint == mark:
            return existing.id
    try:
        derived = identity.identity_from_auth(auth)
    except Exception:
        derived = None
    # The id leads with the compacted timestamp, so a plain sort of the directory is
    # chronological. The random tail keeps two rescues in the same second apart.
    stashed_at = paths.iso_now()
    entry_id = "{}-{}".format(stashed_at.replace(":", "").replace("-", ""),
                              secrets.token_hex(3))
    document = {
        "id": entry_id,
        "stashedAt": stashed_at,
        "reason": reason,
        "email": getattr(derived, "email", None),
        "accountId": getattr(derived, "account_id", None),
        "planType": getattr(derived, "plan_type", None),
        "authMode": getattr(derived, "auth_mode", None),
        "fingerprint": mark,
        "auth": auth,
    }
    _write(home, entry_id, document)
    return entry_id


def stash_raw(text: str, *, reason: str, root: Optional[Path] = None) -> str:
    """Park bytes that did not parse as a credential.

    A live `auth.json` caught mid-write and one written by a Codex release this
    version does not understand look identical from here. Discarding the second to
    avoid keeping the first is the wrong trade: the bytes are small and the login
    may be irreplaceable. `--claim` refuses these; a person has to look.
    """
    if not isinstance(text, str) or not text.strip():
        raise errors.UserError("Refusing to stash an empty credential")
    mark = _digest(text)
    for existing in entries(root):
        if existing.fingerprint == mark:
            return existing.id
    stashed_at = paths.iso_now()
    entry_id = "{}-{}".format(stashed_at.replace(":", "").replace("-", ""),
                              secrets.token_hex(3))
    _write(directory(root), entry_id, {
        "id": entry_id, "stashedAt": stashed_at, "reason": reason,
        "email": None, "accountId": None, "planType": None, "authMode": None,
        "fingerprint": mark, "raw": text,
    })
    return entry_id


def _write(home: Path, entry_id: str, document: Dict[str, Any]) -> None:
    paths.ensure_dir(home)
    paths.secure_chmod(home, 0o700)
    paths.atomic_write_json(home / (entry_id + ".json"), document, mode=0o600)


def _path_for(entry_id: str, root: Optional[Path] = None) -> Path:
    # An id reaches this from the command line, so it must not be able to name a file
    # outside the stash directory.
    if not entry_id or entry_id != os.path.basename(entry_id) or entry_id in (".", ".."):
        raise errors.UserError(f"not a stash entry id: {entry_id!r}")
    return directory(root) / (entry_id + ".json")


def resolve(entry_id: str, root: Optional[Path] = None) -> Entry:
    """The entry this id names, or a UserError naming what is actually there."""
    path = _path_for(entry_id, root)
    entry = _entry_from_document(paths.read_json_tolerant(path, None), entry_id=entry_id)
    if entry is None:
        known = ", ".join(item.id for item in entries(root)) or "none"
        raise errors.UserError(f"no stashed credential {entry_id}; stashed: {known}")
    return entry


def credential(entry_id: str, root: Optional[Path] = None) -> Dict[str, Any]:
    """The stashed credential itself. The only reader is `unclaimed --claim`."""
    if not resolve(entry_id, root).claimable:
        raise errors.UserError(
            f"{entry_id} did not parse as a credential and cannot be registered; "
            f"inspect {_path_for(entry_id, root)}"
        )
    document = paths.read_json_tolerant(_path_for(entry_id, root), None)
    auth = document.get("auth") if isinstance(document, dict) else None
    if not isinstance(auth, dict):
        raise errors.UserError(f"stashed credential {entry_id} is unreadable")
    return auth


def discard(entry_id: str, root: Optional[Path] = None) -> None:
    resolve(entry_id, root)
    try:
        _path_for(entry_id, root).unlink()
    except OSError as exc:
        raise errors.UserError(f"cannot remove stashed credential {entry_id}: {exc}") from None
