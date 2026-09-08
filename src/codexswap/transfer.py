"""Export and import of accounts between machines."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from . import errors, identity, paths
from .locking import FileLock
from .models import Account, AccountIdentity
from .store import AccountStore


def export_accounts(
    store: AccountStore, path: Union[str, Path], *, account_ref: Optional[str] = None,
) -> int:
    with FileLock(store._path(paths.lock_path())):
        accounts = ([store.resolve(account_ref)] if account_ref is not None else store.ordered())
        entries = []
        for account in accounts:
            try:
                auth = identity.load_auth(store._path(paths.slot_auth_path(account.slot)))
            except errors.AuthFileMissing:
                print(f"warning: skipping slot {account.slot} because its auth file is missing",
                      file=sys.stderr)
                continue
            entries.append({"slot": account.slot, "email": account.identity.email,
                            "alias": account.alias, "disabled": account.disabled, "auth": auth})
        paths.atomic_write_json(
            Path(path).expanduser(),
            {"format": "codexswap-export", "version": 1, "exportedAt": paths.iso_now(),
             "activeSlot": store.active_slot, "accounts": entries},
            mode=0o600,
        )
        return len(entries)


def import_accounts(
    store: AccountStore, path: Union[str, Path], *, force: bool = False,
) -> List[Tuple[int, int]]:
    try:
        envelope = paths.read_json(Path(path).expanduser())
    except (OSError, ValueError, errors.AuthFileInvalid):
        raise errors.UserError("Cannot read the account export file") from None
    if (not isinstance(envelope, dict) or envelope.get("format") != "codexswap-export"
            or type(envelope.get("version")) is not int or envelope["version"] != 1):
        raise errors.UserError("Expected a codexswap-export file with version 1")
    if not isinstance(envelope.get("accounts"), list):
        raise errors.UserError("Export accounts must be a list")
    active = envelope.get("activeSlot")
    if active is not None and (type(active) is not int or active <= 0):
        raise errors.UserError("Export activeSlot must be a positive integer or null")

    validated: List[Tuple[dict, AccountIdentity]] = []
    seen = set()
    for entry in envelope["accounts"]:
        if not isinstance(entry, dict):
            raise errors.UserError("Invalid account entry in export")
        source = entry.get("slot")
        if type(source) is not int or source <= 0 or source in seen:
            raise errors.UserError("Export slots must be unique positive integers")
        if entry.get("alias") is not None and not isinstance(entry["alias"], str):
            raise errors.UserError("Export account alias must be a string or null")
        if type(entry.get("disabled", False)) is not bool:
            raise errors.UserError("Export account disabled flag must be a boolean")
        if not isinstance(entry.get("auth"), dict):
            raise errors.UserError("Export account must contain an auth object")
        # Reject here, in the validation pass, so a bad entry cannot overwrite a
        # stored credential that --force would otherwise replace mid-import.
        identity.validate_auth(entry["auth"], source=f"Export account in slot {source}")
        derived = identity.identity_from_auth(entry["auth"])
        if derived.auth_mode == "apikey":
            email = entry.get("email")
            if email is not None and not isinstance(email, str):
                raise errors.UserError("Export account email must be a string or null")
            derived = replace(derived, email=email, plan_type="api key")
        validated.append((entry, derived))
        seen.add(source)

    remaps: List[Tuple[int, int]] = []
    destinations: Dict[int, int] = {}
    with FileLock(store._path(paths.lock_path())):
        store.reload()
        had_active = store.active_slot is not None
        cache = store._read_usage_cache()
        for entry, derived in validated:
            source = entry["slot"]
            destination = source
            if destination in store.accounts and not force:
                destination = store.next_free_slot()
            # CONTRACT: imports preserve every entry, bypassing add's identity deduplication.
            account = Account(slot=destination, identity=derived, alias=entry.get("alias"),
                              disabled=entry.get("disabled", False), added_at=paths.iso_now())
            paths.ensure_dir(store._path(paths.slot_home(destination)))
            store.seed_config(destination)
            paths.atomic_write_json(store._path(paths.slot_auth_path(destination)),
                                    entry["auth"], mode=0o600)
            store.accounts[destination] = account
            cache.pop(str(destination), None)
            destinations[source] = destination
            remaps.append((source, destination))
        if not had_active and active in destinations:
            store.active_slot = destinations[active]
        store._write_usage_cache(cache)
        store.save()
    return remaps
