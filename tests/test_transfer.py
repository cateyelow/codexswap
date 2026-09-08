from __future__ import annotations

import json
import os
import stat
from datetime import datetime

import pytest
from conftest import make_auth

from codexswap import errors, transfer
from codexswap.store import AccountStore

NOW = 1_789_777_228


@pytest.fixture
def populated_store(swap_home):
    store = AccountStore.load(swap_home)
    auths = {}
    for slot, alias in ((1, "main"), (2, "spare")):
        auth = make_auth(
            email=f"source-{slot}@example.com", name=f"Source {slot}",
            account_id=f"source-account-{slot}", plan_type="pro" if slot == 1 else "plus",
            id_exp=NOW + 3600, access_exp=NOW + 86400,
        )
        # Export must preserve fields it does not interpret, as well as every token.
        auth["future_auth_field"] = {"values": [slot, None, True]}
        store.add_from_auth(auth, slot=slot, alias=alias, now=NOW)
        auths[slot] = auth
    store.set_disabled(2, True)
    store.set_active(1)
    return store, auths


@pytest.fixture
def occupied_store(tmp_path):
    root = tmp_path / "occupied-store"
    store = AccountStore.load(root)
    for slot in (1, 2):
        store.add_from_auth(make_auth(
            email=f"existing-{slot}@example.com", account_id=f"existing-account-{slot}",
            id_exp=NOW + 3600, access_exp=NOW + 86400,
        ), slot=slot, alias=f"existing-{slot}", now=NOW)
    store.set_active(2)
    return root, store


def auth_path(root, slot):
    return root / "homes" / str(slot) / "auth.json"


def assert_account_copy(source, destination, expected_auth, destination_auth_path):
    assert destination.identity == source.identity
    assert destination.alias == source.alias
    assert destination.disabled is source.disabled
    assert json.loads(destination_auth_path.read_text(encoding="utf-8")) == expected_auth


def test_two_accounts_round_trip_into_a_second_empty_store(populated_store, swap_home, tmp_path):
    source, auths = populated_store
    export_path = tmp_path / "accounts-export.json"
    destination_root = tmp_path / "second-store"
    destination = AccountStore.load(destination_root)
    assert destination_root != swap_home
    assert destination.accounts == {}

    assert transfer.export_accounts(source, export_path) == 2
    assert transfer.import_accounts(destination, export_path) == [(1, 1), (2, 2)]

    reloaded = AccountStore.load(destination_root)
    assert set(reloaded.accounts) == {1, 2}
    assert reloaded.active_slot == source.active_slot == 1
    for slot in (1, 2):
        assert_account_copy(source.get(slot), reloaded.get(slot), auths[slot],
                            auth_path(destination_root, slot))
        assert json.loads(auth_path(swap_home, slot).read_text(encoding="utf-8")) == auths[slot]


def test_export_envelope_contains_metadata_and_verbatim_auth(populated_store, tmp_path):
    store, auths = populated_store
    path = tmp_path / "export.json"

    assert transfer.export_accounts(store, path) == 2

    envelope = json.loads(path.read_text(encoding="utf-8"))
    assert envelope["format"] == "codexswap-export"
    assert type(envelope["version"]) is int
    assert envelope["version"] == 1
    assert isinstance(envelope["exportedAt"], str)
    assert envelope["exportedAt"]
    assert datetime.fromisoformat(envelope["exportedAt"].replace("Z", "+00:00")).tzinfo is not None
    assert envelope["activeSlot"] == 1
    assert envelope["accounts"] == [
        {"slot": 1, "email": "source-1@example.com", "alias": "main",
         "disabled": False, "auth": auths[1]},
        {"slot": 2, "email": "source-2@example.com", "alias": "spare",
         "disabled": True, "auth": auths[2]},
    ]


@pytest.mark.parametrize("account_ref", ["2", "spare", "source-2@example.com"])
def test_account_ref_exports_one_account_and_returns_count(populated_store, tmp_path, account_ref):
    store, auths = populated_store
    path = tmp_path / "one-account.json"

    assert transfer.export_accounts(store, path, account_ref=account_ref) == 1

    entries = json.loads(path.read_text(encoding="utf-8"))["accounts"]
    assert entries == [{
        "slot": 2, "email": "source-2@example.com", "alias": "spare",
        "disabled": True, "auth": auths[2],
    }]


def test_import_remaps_conflicts_and_preserves_existing_accounts(
    populated_store, occupied_store, tmp_path,
):
    source, auths = populated_store
    root, destination = occupied_store
    before_accounts = {slot: account.to_dict() for slot, account in destination.accounts.items()}
    before_auths = {slot: auth_path(root, slot).read_bytes() for slot in (1, 2)}
    path = tmp_path / "export.json"
    transfer.export_accounts(source, path)

    remaps = transfer.import_accounts(destination, path)

    assert remaps == [(1, 3), (2, 4)]
    reloaded = AccountStore.load(root)
    assert set(reloaded.accounts) == {1, 2, 3, 4}
    assert reloaded.active_slot == 2
    for slot in (1, 2):
        assert reloaded.get(slot).to_dict() == before_accounts[slot]
        assert auth_path(root, slot).read_bytes() == before_auths[slot]
    for source_slot, destination_slot in remaps:
        assert_account_copy(source.get(source_slot), reloaded.get(destination_slot),
                            auths[source_slot], auth_path(root, destination_slot))


def test_force_import_overwrites_occupied_slots(populated_store, occupied_store, tmp_path):
    source, auths = populated_store
    root, destination = occupied_store
    path = tmp_path / "export.json"
    transfer.export_accounts(source, path)

    assert transfer.import_accounts(destination, path, force=True) == [(1, 1), (2, 2)]

    reloaded = AccountStore.load(root)
    assert set(reloaded.accounts) == {1, 2}
    for slot in (1, 2):
        assert_account_copy(source.get(slot), reloaded.get(slot), auths[slot], auth_path(root, slot))


@pytest.mark.parametrize("version", [0, 2, -1, "1", True, None])
def test_import_rejects_unsupported_version(tmp_path, swap_home, version):
    path = tmp_path / "bad-version.json"
    path.write_text(json.dumps({
        "format": "codexswap-export", "version": version, "accounts": [],
    }), encoding="utf-8")
    store = AccountStore.load(swap_home)

    with pytest.raises(errors.UserError, match="version 1"):
        transfer.import_accounts(store, path)

    assert store.accounts == {}
    assert not (swap_home / "accounts.json").exists()


def test_import_rejects_wrong_format(tmp_path, swap_home):
    path = tmp_path / "wrong-format.json"
    path.write_text(json.dumps({
        "format": "unrelated-export", "version": 1, "accounts": [],
    }), encoding="utf-8")
    store = AccountStore.load(swap_home)

    with pytest.raises(errors.UserError, match="codexswap-export"):
        transfer.import_accounts(store, path)

    assert store.accounts == {}
    assert not (swap_home / "accounts.json").exists()


def test_export_warns_and_skips_missing_auth(populated_store, swap_home, tmp_path, capsys):
    store, auths = populated_store
    auth_path(swap_home, 1).unlink()
    path = tmp_path / "partial-export.json"

    assert transfer.export_accounts(store, path) == 1

    entries = json.loads(path.read_text(encoding="utf-8"))["accounts"]
    assert len(entries) == 1
    assert entries[0]["slot"] == 2
    assert entries[0]["auth"] == auths[2]
    captured = capsys.readouterr()
    assert "warning" in captured.err.lower()
    assert "slot 1" in captured.err
    assert "missing" in captured.err.lower()


@pytest.mark.skipif(os.name == "nt", reason="POSIX credential permissions do not apply on Windows")
def test_export_file_is_private_on_posix(populated_store, tmp_path):
    store, _ = populated_store
    path = tmp_path / "private-export.json"

    transfer.export_accounts(store, path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
