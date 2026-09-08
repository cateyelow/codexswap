from __future__ import annotations

import json

import pytest
from conftest import RATE_LIMITS_RESULT, make_auth

from codexswap import errors, paths
from codexswap.models import UsageSnapshot
from codexswap.store import AccountStore

NOW = 1_788_912_000.0


def add_account(store, number, **kwargs):
    auth = make_auth(email=f"person{number}@example.com", account_id=f"acct-{number}",
                     id_exp=int(NOW + 3600), access_exp=int(NOW + 86400))
    return store.add_from_auth(auth, now=NOW, **kwargs)


def test_readding_account_refreshes_identity_and_preserves_metadata():
    store = AccountStore.load()
    original = add_account(store, 1, alias="main")
    store.set_disabled(original.slot, True)
    added_at = original.added_at
    refreshed_auth = make_auth(
        account_id="acct-1", email="updated@example.com", name="Updated Person",
        plan_type="plus", id_exp=int(NOW + 7200), access_exp=int(NOW + 172800),
    )

    updated = store.add_from_auth(refreshed_auth, alias="replacement", now=NOW + 60)

    assert list(store.accounts) == [1]
    assert updated.slot == original.slot
    assert (updated.alias, updated.disabled, updated.added_at) == ("main", True, added_at)
    assert added_at
    assert updated.identity.account_id == "acct-1"
    assert updated.identity.email == "updated@example.com"
    assert updated.identity.name == "Updated Person"
    assert updated.identity.plan_type == "plus"
    assert updated.identity.access_token_exp == NOW + 172800
    assert json.loads(paths.slot_auth_path(1).read_text(encoding="utf-8")) == refreshed_auth


def test_allocation_reuses_smallest_positive_gap():
    store = AccountStore.load()
    assert store.next_free_slot() == 1
    assert [add_account(store, number).slot for number in range(1, 4)] == [1, 2, 3]
    assert store.next_free_slot() == 4
    store.remove(2)
    assert store.next_free_slot() == 2
    assert add_account(store, 4).slot == 2
    assert store.next_free_slot() == 4


def test_explicit_slot_collision_preserves_existing_account():
    store = AccountStore.load()
    first = add_account(store, 1, slot=4)
    before = paths.slot_auth_path(4).read_bytes()
    with pytest.raises(errors.SlotInUse):
        add_account(store, 2, slot=4)
    assert store.ordered() == [first]
    assert paths.slot_auth_path(4).read_bytes() == before


@pytest.mark.parametrize("ref", ["1", "MaIn", "PERSON1@EXAMPLE.COM", "PERSON1@"])
def test_resolve_slot_alias_email_and_unique_prefix(ref):
    store = AccountStore.load()
    account = add_account(store, 1, alias="main")
    add_account(store, 2)
    assert store.resolve(ref) == account


def test_resolve_ambiguous_prefix_names_all_candidates():
    store = AccountStore.load()
    first, second = add_account(store, 1), add_account(store, 2)
    with pytest.raises(errors.UserError) as raised:
        store.resolve("person")
    for account in (first, second):
        assert account.identity.email in str(raised.value)
        assert str(account.slot) in str(raised.value)


def test_resolve_unknown_reference():
    store = AccountStore.load()
    add_account(store, 1)
    with pytest.raises(errors.AccountNotFound):
        store.resolve("nobody@example.net")


def test_missing_registry_loads_empty_and_resolve_explains_setup():
    store = AccountStore.load()
    assert store.accounts == {}
    assert store.active_slot is None
    with pytest.raises(errors.NoAccountsConfigured):
        store.resolve("1")


def test_alias_and_disable_mutators_persist():
    store = AccountStore.load()
    account = add_account(store, 1)
    store.set_alias(account.slot, "work")
    store.set_disabled(account.slot, True)
    loaded = AccountStore.load()
    assert loaded.get(1).alias == "work"
    assert loaded.get(1).disabled is True
    assert loaded.enabled_accounts() == []
    loaded.set_alias(1, None)
    loaded.set_disabled(1, False)
    reloaded = AccountStore.load()
    assert reloaded.get(1).alias is None
    assert reloaded.get(1).disabled is False
    assert [account.slot for account in reloaded.enabled_accounts()] == [1]


def test_remove_deletes_registry_home_and_active_slot():
    store = AccountStore.load()
    add_account(store, 1)
    other = add_account(store, 2)
    store.set_active(1)
    home = paths.slot_home(1)
    (home / "cache").mkdir()
    (home / "cache" / "marker").write_text("slot data", encoding="utf-8")
    store.remove(1)
    assert not home.exists()
    loaded = AccountStore.load()
    assert loaded.ordered() == [other]
    assert loaded.active_slot is None


@pytest.mark.parametrize("operation", ["swap", "move-free", "move-occupied"])
@pytest.mark.parametrize("active_slot", [1, 2])
def test_slot_operations_move_accounts_homes_and_active_identity(operation, active_slot):
    store = AccountStore.load()
    for number in (1, 2):
        add_account(store, number)
        (paths.slot_home(number) / "marker.txt").write_text(f"account-{number}", encoding="utf-8")
    store.set_active(active_slot)
    active_id = store.get(active_slot).identity.account_id
    if operation == "swap":
        store.swap_slots(1, 2)
    else:
        store.move_slot(1, 3 if operation == "move-free" else 2)
    expected = {2: 2, 3: 1} if operation == "move-free" else {1: 2, 2: 1}
    loaded = AccountStore.load()
    assert set(loaded.accounts) == set(expected)
    for slot, number in expected.items():
        account = loaded.get(slot)
        assert account.slot == slot
        assert account.identity.account_id == f"acct-{number}"
        assert (paths.slot_home(slot) / "marker.txt").read_text(encoding="utf-8") == f"account-{number}"
        auth = json.loads(paths.slot_auth_path(slot).read_text(encoding="utf-8"))
        assert auth["tokens"]["account_id"] == f"acct-{number}"
    assert loaded.get(loaded.active_slot).identity.account_id == active_id
    if operation == "move-free":
        assert not paths.slot_home(1).exists()


def test_usage_cache_freshness_and_disk_reload():
    store = AccountStore.load()
    add_account(store, 1)
    assert store.cached_usage(1, now=NOW) is None
    snapshot = UsageSnapshot.from_api(RATE_LIMITS_RESULT, fetched_at=NOW)
    store.record_usage(1, snapshot)
    assert store.cached_usage(1, max_age=120, now=NOW + 119) == snapshot
    assert store.cached_usage(1, max_age=120, now=NOW + 121) is None
    loaded = AccountStore.load()
    assert loaded.cached_usage(1, max_age=120, now=NOW + 119) == snapshot
    assert loaded.get(1).last_seen_at == NOW
    assert loaded.get(1).last_seen_usage == snapshot


def test_registry_round_trip(swap_home):
    store = AccountStore.load()
    add_account(store, 1, alias="main")
    add_account(store, 2, alias="backup")
    store.set_disabled(2, True)
    store.set_active(1)
    document = json.loads((swap_home / "accounts.json").read_text(encoding="utf-8"))
    assert document["version"] == 1
    assert document["activeSlot"] == 1
    assert document["accounts"] == [account.to_dict() for account in store.ordered()]
    loaded = AccountStore.load()
    assert loaded.accounts == store.accounts
    assert loaded.active_slot == store.active_slot
    assert [(a.alias, a.disabled) for a in loaded.ordered()] == [("main", False), ("backup", True)]


@pytest.mark.parametrize("corrupt", ["{broken json", "[]", '{"version":1,"accounts":[{"slot":0}]}'])
def test_corrupt_registry_is_quarantined_with_warning(swap_home, capsys, corrupt):
    (swap_home / "accounts.json").write_text(corrupt, encoding="utf-8")
    store = AccountStore.load()
    assert store.accounts == {}
    assert store.active_slot is None
    quarantined = list(swap_home.glob("accounts.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == corrupt
    assert not (swap_home / "accounts.json").exists()
    captured = capsys.readouterr()
    assert "warning" in captured.err.lower()
    assert "accounts.json" in captured.err
    assert captured.out == ""


@pytest.mark.parametrize("method,args", [
    ("remove", (99,)),
    ("set_alias", (99, "missing")),
    ("set_alias", (99, None)),
    ("set_disabled", (99, True)),
    ("set_disabled", (99, False)),
    ("swap_slots", (99, 1)),
    ("swap_slots", (1, 99)),
    ("move_slot", (99, 3)),
    ("set_active", (99,)),
    ("record_usage", (99, UsageSnapshot.from_api(RATE_LIMITS_RESULT, fetched_at=NOW))),
])
def test_existing_slot_mutators_reject_unknown_slot(method, args):
    # add_from_auth and move_slot destinations intentionally allow new slots.
    store = AccountStore.load()
    add_account(store, 1)
    before = paths.accounts_path().read_bytes()
    with pytest.raises(errors.AccountNotFound):
        getattr(store, method)(*args)
    assert paths.accounts_path().read_bytes() == before


def test_alias_must_identify_exactly_one_account():
    store = AccountStore.load()
    add_account(store, 1, alias="work")
    second = add_account(store, 2)
    with pytest.raises(errors.UserError, match="already used by slot 1"):
        store.set_alias(second.slot, "WORK")
    assert store.get(2).alias is None
    # Re-setting an account's own alias is not a clash with itself.
    store.set_alias(1, "work")
    assert store.resolve("work").slot == 1


def test_duplicate_alias_from_an_older_registry_is_reported_not_guessed():
    store = AccountStore.load()
    add_account(store, 1, alias="work")
    add_account(store, 2)
    # Written by a build that did not enforce uniqueness; taking the first match
    # would switch, run or redeem against an account the user did not name.
    registry = json.loads(paths.accounts_path().read_text(encoding="utf-8"))
    for entry in registry["accounts"]:
        entry["alias"] = "work"
    paths.accounts_path().write_text(json.dumps(registry), encoding="utf-8")
    reloaded = AccountStore.load()
    with pytest.raises(errors.UserError, match="Ambiguous alias"):
        reloaded.resolve("work")
    assert reloaded.resolve("1").slot == 1


def test_directory_mappings_follow_the_account_not_the_slot(tmp_path):
    from codexswap import mappings

    store = AccountStore.load()
    add_account(store, 1)
    add_account(store, 2)
    first, second = tmp_path / "one", tmp_path / "two"
    mappings.set_mapping(first, 1)
    mappings.set_mapping(second, 2)

    store.swap_slots(1, 2)
    assert mappings.lookup(first) == 2
    assert mappings.lookup(second) == 1

    store.move_slot(2, 7)
    assert mappings.lookup(first) == 7

    store.remove(7)
    # Not left pointing at 7: the next account added there would inherit the promise.
    assert mappings.lookup(first) is None
    assert mappings.lookup(second) == 1
