"""The account registry and its on-disk representation."""

from __future__ import annotations

import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Union

from . import errors, identity, paths
from .locking import FileLock
from .models import Account, UsageSnapshot


class AccountStore:
    accounts: Dict[int, Account]
    active_slot: Optional[int]

    def __init__(self, root: Optional[Path] = None) -> None:
        self.accounts = {}
        self.active_slot = None
        self._root = Path(root).expanduser().resolve() if root is not None else None

    def _path(self, default: Path) -> Path:
        if self._root is None:
            return default
        return self._root / default.relative_to(paths.codexswap_home())

    @classmethod
    def load(cls, root: Optional[Path] = None) -> AccountStore:
        store = cls(root)
        registry = store._path(paths.accounts_path())
        try:
            data = paths.read_json(registry)
            if data is None and not registry.exists():
                return store
            if not isinstance(data, dict) or type(data.get("version")) is not int:
                raise ValueError("Invalid registry")
            if data["version"] != 1 or not isinstance(data.get("accounts"), list):
                raise ValueError("Invalid registry")
            for entry in data["accounts"]:
                if not isinstance(entry, dict):
                    raise ValueError("Invalid account")
                slot = entry.get("slot")
                if type(slot) is not int or slot <= 0 or slot in store.accounts:
                    raise ValueError("Invalid slot")
                store.accounts[slot] = Account.from_dict(entry)
            active = data.get("activeSlot")
            if active is not None and (
                type(active) is not int or active not in store.accounts
            ):
                raise ValueError("Invalid active slot")
            store.active_slot = active
        except (ValueError, TypeError, KeyError, AttributeError):
            paths.quarantine_corrupt(registry)
            print("warning: corrupt accounts.json was quarantined; loaded an empty store",
                  file=sys.stderr)
            store.accounts = {}
            store.active_slot = None
        return store

    def save(self) -> None:
        with FileLock(self._path(paths.lock_path())):
            paths.atomic_write_json(
                self._path(paths.accounts_path()),
                {"version": 1, "activeSlot": self.active_slot,
                 "accounts": [account.to_dict() for account in self.ordered()]},
                mode=0o600,
            )

    def next_free_slot(self) -> int:
        slot = 1
        while slot in self.accounts:
            slot += 1
        return slot

    def _validate_slot(self, slot: int) -> None:
        if type(slot) is not int or slot <= 0:
            raise errors.AccountNotFound("Slot must be a positive integer")

    def _not_found(self) -> errors.AccountNotFound:
        known = ", ".join(str(slot) for slot in sorted(self.accounts)) or "none"
        return errors.AccountNotFound("Account not found. Known slots: " + known)

    def resolve(self, ref: Union[str, int]) -> Account:
        if not self.accounts:
            raise errors.NoAccountsConfigured("No accounts configured; run codexswap add")
        value = str(ref).strip()
        try:
            number = int(value)
        except ValueError:
            number = None
        if number in self.accounts:
            return self.accounts[number]
        folded = value.casefold()
        for account in self.ordered():
            if account.alias is not None and account.alias.casefold() == folded:
                return account
        account = self.find_by_email(value)
        if account is not None:
            return account
        matches = [account for account in self.ordered()
                   if folded and account.identity.email is not None
                   and account.identity.email.casefold().startswith(folded)]
        if len(matches) == 1:
            return matches[0]
        if matches:
            candidates = ", ".join(f"{a.slot}: {a.identity.email}" for a in matches)
            raise errors.UserError("Ambiguous account prefix. Candidates: " + candidates)
        raise self._not_found()

    def get(self, slot: int) -> Account:
        self._validate_slot(slot)
        if slot not in self.accounts:
            raise self._not_found()
        return self.accounts[slot]

    def add_from_auth(
        self, auth: dict, *, slot: Optional[int] = None,
        alias: Optional[str] = None, now: Optional[float] = None,
    ) -> Account:
        if slot is not None:
            self._validate_slot(slot)
        derived = identity.identity_from_auth(auth)
        with FileLock(self._path(paths.lock_path())):
            existing = (self.find_by_account_id(derived.account_id)
                        if derived.account_id is not None else None)
            if existing is None and derived.email is not None:
                existing = self.find_by_email(derived.email)
            if existing is not None:
                account = existing
            else:
                slot = self.next_free_slot() if slot is None else slot
                if slot in self.accounts:
                    raise errors.SlotInUse(f"Slot {slot} is already in use")
                # CONTRACT: addedAt uses iso_now(); now does not alter its format.
                account = Account(slot=slot, identity=derived, alias=alias,
                                  added_at=paths.iso_now())
            paths.ensure_dir(self._path(paths.slot_home(account.slot)))
            paths.atomic_write_json(self._path(paths.slot_auth_path(account.slot)),
                                    auth, mode=0o600)
            account.identity = derived
            self.accounts[account.slot] = account
            self.save()
            return account

    def _checked_home(self, slot: int) -> Path:
        home = self._path(paths.slot_home(slot))
        parent = self._path(paths.homes_dir()).resolve()
        if home.is_symlink() or home.resolve().parent != parent:
            raise errors.UserError("Slot home must be inside the homes directory")
        return home

    def remove(self, slot: int) -> Account:
        with FileLock(self._path(paths.lock_path())):
            account = self.get(slot)
            home = self._checked_home(slot)
            shutil.rmtree(home, ignore_errors=True)
            del self.accounts[slot]
            if self.active_slot == slot:
                self.active_slot = None
            cache = self._read_usage_cache()
            cache.pop(str(slot), None)
            self._write_usage_cache(cache)
            self.save()
            return account

    def set_alias(self, slot: int, alias: Optional[str]) -> Account:
        with FileLock(self._path(paths.lock_path())):
            account = self.get(slot)
            account.alias = alias
            self.save()
            return account

    def set_disabled(self, slot: int, disabled: bool) -> Account:
        with FileLock(self._path(paths.lock_path())):
            account = self.get(slot)
            account.disabled = disabled
            self.save()
            return account

    def swap_slots(self, a: int, b: int) -> None:
        with FileLock(self._path(paths.lock_path())):
            first, second = self.get(a), self.get(b)
            if a == b:
                self.save()
                return
            home_a, home_b = self._checked_home(a), self._checked_home(b)
            paths.ensure_dir(home_a.parent)
            temporary = home_a.parent / (".swap-" + uuid.uuid4().hex)
            if temporary.resolve().parent != home_a.parent.resolve():
                raise errors.UserError("Invalid temporary slot directory")
            moved_a = moved_b = False
            try:
                if home_a.exists():
                    home_a.rename(temporary)
                    moved_a = True
                if home_b.exists():
                    home_b.rename(home_a)
                    moved_b = True
                if moved_a:
                    temporary.rename(home_b)
            except OSError:
                # Keep credentials in the temporary directory if rollback also fails.
                if moved_b:
                    home_a.rename(home_b)
                if moved_a:
                    temporary.rename(home_a)
                raise
            first.slot, second.slot = b, a
            self.accounts[a], self.accounts[b] = second, first
            if self.active_slot == a:
                self.active_slot = b
            elif self.active_slot == b:
                self.active_slot = a
            # CONTRACT: cached usage follows the account when its slot changes.
            cache = self._read_usage_cache()
            usage_a, usage_b = cache.pop(str(a), None), cache.pop(str(b), None)
            if usage_a is not None:
                cache[str(b)] = usage_a
            if usage_b is not None:
                cache[str(a)] = usage_b
            self._write_usage_cache(cache)
            self.save()

    def move_slot(self, slot: int, new_slot: int) -> None:
        self._validate_slot(new_slot)
        with FileLock(self._path(paths.lock_path())):
            account = self.get(slot)
            if new_slot in self.accounts:
                self.swap_slots(slot, new_slot)
                return
            source, destination = self._checked_home(slot), self._checked_home(new_slot)
            if destination.exists():
                raise errors.SlotInUse(f"Slot {new_slot} already has a home directory")
            if source.exists():
                source.rename(destination)
            del self.accounts[slot]
            account.slot = new_slot
            self.accounts[new_slot] = account
            if self.active_slot == slot:
                self.active_slot = new_slot
            cache = self._read_usage_cache()
            usage = cache.pop(str(slot), None)
            cache.pop(str(new_slot), None)
            if usage is not None:
                cache[str(new_slot)] = usage
            self._write_usage_cache(cache)
            self.save()

    def enabled_accounts(self) -> List[Account]:
        return [account for account in self.ordered() if not account.disabled]

    def ordered(self) -> List[Account]:
        return [self.accounts[slot] for slot in sorted(self.accounts)]

    def _read_usage_cache(self) -> Dict[str, dict]:
        cache = paths.read_json_tolerant(self._path(paths.usage_cache_path()), {})
        return cache if isinstance(cache, dict) else {}

    def _write_usage_cache(self, cache: Dict[str, dict]) -> None:
        paths.atomic_write_json(self._path(paths.usage_cache_path()), cache, mode=0o600)

    def record_usage(self, slot: int, snapshot: UsageSnapshot) -> None:
        with FileLock(self._path(paths.lock_path())):
            account = self.get(slot)
            cache = self._read_usage_cache()
            cache[str(slot)] = snapshot.to_dict()
            self._write_usage_cache(cache)
            account.last_seen_usage = snapshot
            account.last_seen_at = snapshot.fetched_at
            self.save()

    def cached_usage(
        self, slot: int, *, max_age: Optional[float] = None,
        now: Optional[float] = None,
    ) -> Optional[UsageSnapshot]:
        self.get(slot)
        data = self._read_usage_cache().get(str(slot))
        if not isinstance(data, dict):
            return None
        try:
            snapshot = UsageSnapshot.from_dict(data)
        except (ValueError, TypeError, KeyError, AttributeError):
            return None
        if (max_age is not None
                and (time.time() if now is None else now) - snapshot.fetched_at > max_age):
            return None
        return snapshot

    def set_active(self, slot: Optional[int]) -> None:
        with FileLock(self._path(paths.lock_path())):
            if slot is not None:
                self.get(slot)
            self.active_slot = slot
            self.save()

    def find_by_account_id(self, account_id: str) -> Optional[Account]:
        if account_id is None:
            return None
        return next((a for a in self.ordered() if a.identity.account_id == account_id), None)

    def find_by_email(self, email: str) -> Optional[Account]:
        if email is None:
            return None
        return next((a for a in self.ordered() if a.identity.email is not None
                     and a.identity.email.casefold() == email.casefold()), None)
