from __future__ import annotations

import json

import pytest
from conftest import make_auth

from codexswap import appserver, errors, identity, paths, switcher
from codexswap.store import AccountStore

NOW = 1_788_912_000.0


@pytest.fixture(autouse=True)
def no_real_subprocesses(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("A switcher test attempted to launch a real subprocess")

    for name in ("run", "call", "Popen"):
        monkeypatch.setattr(switcher.subprocess, name, forbidden)


def add_target(store):
    return store.add_from_auth(
        make_auth(email="target@example.com", account_id="acct-target",
                  id_exp=int(NOW + 3600), access_exp=int(NOW + 86400)), now=NOW,
    )


def test_current_live_identity_reads_auth(live_auth):
    _, auth = live_auth
    assert switcher.current_live_identity() == identity.identity_from_auth(auth)


@pytest.mark.parametrize("contents", [None, "{invalid", "[]", '{"auth_mode":"chatgpt","tokens":{"id_token":"broken"}}'])
def test_current_live_identity_missing_or_unparseable_returns_none(codex_root, contents):
    if contents is not None:
        (codex_root / "auth.json").write_text(contents, encoding="utf-8")
    assert switcher.current_live_identity() is None


def test_capture_missing_auth_suggests_login():
    with pytest.raises(errors.AuthFileMissing, match="codex login"):
        switcher.capture_current(AccountStore.load())


def test_capture_copies_auth_and_sets_initial_active_account(live_auth):
    live_path, auth = live_auth
    original = live_path.read_bytes()
    store = AccountStore.load()
    account = switcher.capture_current(store, alias="main")
    assert account.alias == "main"
    assert json.loads(paths.slot_auth_path(account.slot).read_text(encoding="utf-8")) == auth
    assert store.active_slot == account.slot
    assert AccountStore.load().active_slot == account.slot
    assert live_path.read_bytes() == original


def test_activate_copies_auth_updates_metadata_and_keeps_slot(live_auth, monkeypatch):
    live_path, _ = live_auth
    store = AccountStore.load()
    switcher.capture_current(store)
    target = add_target(store)
    slot_path = paths.slot_auth_path(target.slot)
    slot_bytes = slot_path.read_bytes()
    switched_at = "2026-09-09T03:00:00Z"
    monkeypatch.setattr(paths, "iso_now", lambda: switched_at)
    monkeypatch.setattr(switcher, "detect_running_codex", lambda: [])

    switcher.activate(store, target)

    assert json.loads(live_path.read_text(encoding="utf-8")) == json.loads(slot_bytes)
    assert slot_path.read_bytes() == slot_bytes
    assert store.active_slot == target.slot
    assert target.last_switched_at == switched_at
    loaded = AccountStore.load()
    assert loaded.active_slot == target.slot
    assert loaded.get(target.slot).last_switched_at == switched_at


def test_activate_syncs_refreshed_live_auth_before_overwrite(live_auth, monkeypatch):
    live_path, auth = live_auth
    store = AccountStore.load()
    original = switcher.capture_current(store)
    target = add_target(store)
    auth["last_refresh"] = "2026-09-09T04:05:06Z"
    live_path.write_text(json.dumps(auth), encoding="utf-8")
    calls = []
    real_sync = switcher.sync_live_to_slot

    def observe_sync(current_store):
        assert json.loads(live_path.read_text(encoding="utf-8"))["tokens"]["account_id"] == original.identity.account_id
        result = real_sync(current_store)
        calls.append(result)
        assert json.loads(paths.slot_auth_path(original.slot).read_text(encoding="utf-8"))["last_refresh"] == auth["last_refresh"]
        return result

    monkeypatch.setattr(switcher, "sync_live_to_slot", observe_sync)
    switcher.activate(store, target, force=True)
    assert calls == [original.slot]
    assert json.loads(paths.slot_auth_path(original.slot).read_text(encoding="utf-8"))["last_refresh"] == auth["last_refresh"]
    assert json.loads(live_path.read_text(encoding="utf-8"))["tokens"]["account_id"] == target.identity.account_id


def test_corrupt_slot_does_not_clobber_live_auth(live_auth):
    live_path, _ = live_auth
    store = AccountStore.load()
    original = switcher.capture_current(store)
    target = add_target(store)
    paths.slot_auth_path(target.slot).write_text("{corrupt", encoding="utf-8")
    before = live_path.read_bytes()
    with pytest.raises(errors.AuthFileInvalid):
        switcher.activate(store, target, force=True)
    assert live_path.read_bytes() == before
    assert store.active_slot == original.slot
    assert target.last_switched_at is None


def test_activate_refuses_running_codex_unless_forced(live_auth, monkeypatch):
    live_path, _ = live_auth
    before = live_path.read_bytes()
    store = AccountStore.load()
    target = add_target(store)
    monkeypatch.setattr(switcher, "detect_running_codex", lambda: [
        switcher.ProcessInfo(pid=4242, name="codex.exe", cmdline="codex --resume"),
    ])
    with pytest.raises(errors.CodexRunning, match="4242"):
        switcher.activate(store, target)
    assert live_path.read_bytes() == before
    switcher.activate(store, target, force=True)
    assert store.active_slot == target.slot
    assert switcher.current_live_identity().account_id == target.identity.account_id


def test_detect_running_codex_reports_unknown_on_oserror(monkeypatch):
    calls = []

    def fail(command, **kwargs):
        calls.append(command)
        raise OSError("process discovery unavailable")

    monkeypatch.setattr(switcher.subprocess, "run", fail)
    # None, not [], so that activate can refuse instead of overwriting auth.json
    # while a Codex session that discovery simply could not see is still holding it.
    assert switcher.detect_running_codex() is None
    assert calls


def test_activate_refuses_when_process_discovery_fails(monkeypatch, swap_home, live_auth):
    store = AccountStore.load()
    account = store.add_from_auth(make_auth())
    monkeypatch.setattr(switcher, "detect_running_codex", lambda: None)
    before = paths.live_auth_path().read_bytes()
    with pytest.raises(errors.CodexRunning) as caught:
        switcher.activate(store, account)
    assert "--force" in str(caught.value)
    assert paths.live_auth_path().read_bytes() == before
    # The same unknown answer must not block an explicitly forced switch.
    switcher.activate(store, account, force=True)
    assert store.active_slot == account.slot


def test_slot_env_overrides_home_and_preserves_other_variables():
    base = {"CODEX_HOME": "old-home", "PATH": "example-path", "CUSTOM": "kept"}
    result = switcher.slot_env(2, base_env=base)
    assert result == {"CODEX_HOME": str(paths.slot_home(2)), "PATH": "example-path", "CUSTOM": "kept"}
    assert base["CODEX_HOME"] == "old-home"


def test_run_as_passes_argv_and_slot_environment_without_changing_live(live_auth, monkeypatch):
    live_path, _ = live_auth
    before = live_path.read_bytes()
    store = AccountStore.load()
    account = add_target(store)
    calls = []

    def fake_call(argv, *, env):
        calls.append((argv, env))
        return 17

    monkeypatch.setenv("CODEXSWAP_TEST_MARKER", "preserved")
    monkeypatch.setattr(appserver, "find_codex_binary", lambda: "fake-codex")
    monkeypatch.setattr(switcher.subprocess, "call", fake_call)
    assert switcher.run_as(store, account, ["--resume", "session-123"]) == 17
    assert len(calls) == 1
    argv, env = calls[0]
    assert argv == ["fake-codex", "--resume", "session-123"]
    assert env["CODEX_HOME"] == str(paths.slot_home(account.slot))
    assert env["CODEXSWAP_TEST_MARKER"] == "preserved"
    assert live_path.read_bytes() == before
    assert store.active_slot is None


def test_unparsable_process_output_reports_unknown_not_empty(monkeypatch):
    class Completed:
        returncode, stdout, stderr = 0, "this is not a process listing\n", ""

    monkeypatch.setattr(switcher.subprocess, "run", lambda *a, **k: Completed())

    # A command that succeeds but says nothing we understand is not evidence that no
    # Codex is running; reporting [] here would let a switch overwrite a live token.
    assert switcher.detect_running_codex() is None


def test_a_listing_with_no_codex_rows_still_reports_empty(monkeypatch):
    listing = "  1 systemd /sbin/init\n 42 bash -l\n"
    ancestry = "1 0\n42 1\n"
    outputs = iter([listing, ancestry])

    class Completed:
        def __init__(self, stdout):
            self.returncode, self.stdout, self.stderr = 0, stdout, ""

    monkeypatch.setattr(switcher.os, "name", "posix")
    monkeypatch.setattr(switcher.subprocess, "run", lambda *a, **k: Completed(next(outputs)))

    assert switcher.detect_running_codex() == []
