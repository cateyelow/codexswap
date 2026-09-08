from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import RATE_LIMITS_RESULT, make_apikey_auth, make_auth

from codexswap import auto, errors, locking, mappings, paths, resets, strategy, switcher, transfer
from codexswap.models import Account, RateLimitWindow, UsageSnapshot
from codexswap.settings import SPECS, Settings
from codexswap.store import AccountStore

NOW = 1_789_777_228.0
STATE_FILES = ("accounts.json", "settings.json", "state.json", "mappings.json", "usage-cache.json")
# Deliberately independent of the implementation's bounds.
NUMERIC_BOUNDS = (
    ("autoswitch.threshold", 1, 100),
    ("autoswitch.intervalSeconds", 10, 3600),
    ("autoswitch.cooldownSeconds", 0, 86400),
    ("autoswitch.hysteresisPct", 0, 50),
    ("autoswitch.unhealthyTicks", 1, 20),
    ("reset.expiryDays", 0, 30),
    ("reset.minUsagePercent", 0, 100),
    ("reset.maxPerDay", 0, 10),
    ("probe.timeoutSeconds", 5, 300),
    ("probe.staleSeconds", 0, 86400),
)


@pytest.fixture(autouse=True)
def offline_robustness(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Robustness tests must not probe, redeem, or discover a Codex binary")

    from codexswap import appserver

    monkeypatch.setattr(resets, "redeem", forbidden)
    monkeypatch.setattr(appserver, "probe_usage", forbidden)
    monkeypatch.setattr(appserver, "find_codex_binary", forbidden)
    monkeypatch.setattr(switcher, "detect_running_codex", forbidden)


@pytest.fixture
def two_accounts(live_auth):
    store = AccountStore.load()
    store.add_from_auth(live_auth[1], slot=1)
    store.add_from_auth(make_auth(email="second@example.com", account_id="second"), slot=2)
    store.set_active(1)
    return store


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def snapshot(percent=84, *, account_id="acct-0000-1111"):
    return replace(
        UsageSnapshot.from_api(RATE_LIMITS_RESULT, fetched_at=NOW),
        account_id=account_id,
        primary=RateLimitWindow(percent, 10080, int(NOW + 86400)),
    )


@pytest.mark.parametrize("damage", ["non-credential", "empty", "truncated", "directory"])
def test_activate_bad_target_preserves_live_bytes(two_accounts, live_auth, damage):
    target = paths.slot_auth_path(2)
    if damage == "directory":
        target.unlink()
        target.mkdir()
    else:
        target.write_bytes({
            "non-credential": b'{"hello": 1}',
            "empty": b"",
            "truncated": b'{"tokens":{"refresh_token":"rt.1.TRUNC',
        }[damage])
    before = live_auth[0].read_bytes()
    try:
        with pytest.raises((errors.AuthFileInvalid, errors.AuthFileMissing)):
            switcher.activate(two_accounts, two_accounts.get(2), force=True)
    finally:
        # Run the safety assertion even if activate fails to reject the input.
        assert live_auth[0].read_bytes() == before, "Invalid target destroyed the live credential"
    assert two_accounts.active_slot == AccountStore.load().active_slot == 1


def test_activate_replace_failure_preserves_live_and_cleans_temp(
    two_accounts, live_auth, codex_root, monkeypatch,
):
    before = live_auth[0].read_bytes()
    target_before = paths.slot_auth_path(2).read_bytes()
    real_replace = os.replace
    destinations = []

    def crash_first_replace(source, destination):
        destinations.append(Path(destination))
        if len(destinations) == 1:
            raise OSError("injected crash before live replacement")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", crash_first_replace)
    # Disable sync-back so the FIRST replace really is the live credential write.
    with pytest.raises(OSError, match="injected crash"):
        switcher.activate(two_accounts, two_accounts.get(2), force=True, sync_back=False)
    assert destinations == [live_auth[0]]
    assert live_auth[0].read_bytes() == before
    assert paths.slot_auth_path(2).read_bytes() == target_before
    assert list(codex_root.glob(".auth.json.*.tmp")) == []
    assert two_accounts.active_slot == AccountStore.load().active_slot == 1


def test_atomic_write_full_disk_preserves_original_and_cleans_temp(live_auth, monkeypatch):
    destination = live_auth[0]
    before = destination.read_bytes()
    factory = paths.tempfile.NamedTemporaryFile
    temporary_names = []

    @contextmanager
    def full_disk_file(*args, **kwargs):
        with factory(*args, **kwargs) as handle:
            temporary_names.append(Path(handle.name))
            original_write = handle.write

            def partial_write(text):
                original_write(text[:7])
                raise OSError(28, "No space left on device")

            monkeypatch.setattr(handle, "write", partial_write)
            yield handle

    monkeypatch.setattr(paths.tempfile, "NamedTemporaryFile", full_disk_file)
    with pytest.raises(OSError) as caught:
        paths.atomic_write_text(destination, '{"replacement": true}')
    assert caught.value.errno == 28
    assert destination.read_bytes() == before
    assert len(temporary_names) == 1
    assert not temporary_names[0].exists()
    assert list(destination.parent.glob(".auth.json.*.tmp")) == []


@pytest.mark.parametrize("kind", ["oauth", "apikey"])
def test_activate_same_slot_twice_keeps_complete_credential(live_auth, kind):
    auth = (make_auth(email="repeat@example.com", account_id="repeat")
            if kind == "oauth" else make_apikey_auth())
    auth["futureCredentialField"] = {"nested": [None, "keep"]}
    store = AccountStore.load()
    account = store.add_from_auth(auth)
    switcher.activate(store, account, force=True)
    first = live_auth[0].read_bytes()
    switcher.activate(store, account, force=True)
    assert live_auth[0].read_bytes() == first
    assert read_json(live_auth[0]) == read_json(paths.slot_auth_path(account.slot)) == auth
    assert store.active_slot == AccountStore.load().active_slot == account.slot


def test_sync_copies_refreshed_live_content_to_its_actual_slot(two_accounts, live_auth):
    refreshed = copy.deepcopy(live_auth[1])
    refreshed["tokens"]["refresh_token"] = "rt.1.TESTONLY-REFRESHED"
    refreshed["last_refresh"] = "2026-09-09T12:00:00Z"
    live_auth[0].write_text(json.dumps(refreshed), encoding="utf-8")
    before = live_auth[0].read_bytes()
    second_before = paths.slot_auth_path(2).read_bytes()
    # A stale activeSlot must not cause a refreshed token to overwrite another account.
    two_accounts.set_active(2)
    assert switcher.sync_live_to_slot(two_accounts) == 1
    assert read_json(paths.slot_auth_path(1)) == refreshed
    assert live_auth[0].read_bytes() == before
    assert paths.slot_auth_path(2).read_bytes() == second_before


def test_sync_unknown_live_identity_creates_nothing(two_accounts, live_auth, swap_home):
    auth = make_auth(email="unknown@example.com", account_id="unknown")
    live_auth[0].write_text(json.dumps(auth), encoding="utf-8")
    before_live = live_auth[0].read_bytes()
    before = {p.relative_to(swap_home): p.read_bytes()
              for p in swap_home.rglob("*") if p.is_file()}
    assert switcher.sync_live_to_slot(two_accounts) is None
    assert live_auth[0].read_bytes() == before_live
    assert {p.relative_to(swap_home): p.read_bytes()
            for p in swap_home.rglob("*") if p.is_file()} == before
    assert set(two_accounts.accounts) == set(AccountStore.load().accounts) == {1, 2}


def malformed_fields(filename):
    if filename == "accounts.json":
        return {"version": [], "activeSlot": {}, "accounts": False}
    if filename == "settings.json":
        data = {}
        for key in SPECS:
            group, field = key.split(".")
            data.setdefault(group, {})[field] = {"wrong": [None]}
        return data
    if filename == "state.json":
        return {"lastSwitchAt": [], "cooldownUntil": {}, "unhealthy": False, "redemptions": {}}
    if filename == "mappings.json":
        return {"a": [], "b": {}, "c": False, "d": "1", "e": None}
    return {"1": {key: [] for key in snapshot().to_dict()}}


@pytest.mark.parametrize("filename", STATE_FILES)
@pytest.mark.parametrize("damage", ["absent", "empty", "null", "array", "wrong-fields", "truncated"])
def test_state_loaders_degrade_safely(filename, damage, swap_home, capsys):
    store = AccountStore.load()
    if filename == "usage-cache.json":
        store.add_from_auth(make_auth(), slot=1)
    path = swap_home / filename
    payloads = {"empty": "", "null": "null", "array": "[]", "truncated": '{"unfinished":'}
    if damage != "absent":
        payload = json.dumps(malformed_fields(filename)) if damage == "wrong-fields" else payloads[damage]
        path.write_text(payload, encoding="utf-8")
    if filename == "accounts.json":
        restored = AccountStore.load()
        assert restored.accounts == {}
        assert restored.active_slot is None
        quarantined = list(swap_home.glob("accounts.json.corrupt-*"))
        if damage == "absent":
            assert quarantined == []
        else:
            assert not path.exists()
            assert len(quarantined) == 1
            assert quarantined[0].read_bytes() == payload.encode("utf-8")
            assert "warning" in capsys.readouterr().err.lower()
    elif filename == "settings.json":
        restored = Settings.load()
        assert {key: restored.get(key) for key in SPECS} == {
            key: spec.default for key, spec in SPECS.items()
        }
    elif filename == "state.json":
        restored = auto.AutoState.load()
        assert restored == auto.AutoState()
        assert restored.redeemed_last_24h(NOW) == 0
        restored.prune(NOW)
    elif filename == "mappings.json":
        assert mappings.load_mappings() == {}
    else:
        cached = store.cached_usage(1)
        if damage == "wrong-fields":
            # Pin the defensible, unspecified choice to retain a blank snapshot
            # instead of discarding an object whose fields are all malformed.
            assert cached == UsageSnapshot.from_dict({})
            assert store.cached_usage(1, max_age=120, now=NOW) is None
        else:
            assert cached is None


def test_account_optional_fields_with_wrong_types_are_sanitised(swap_home):
    blank = Account.from_dict({"slot": 1})
    entry = {key: [] for key in blank.to_dict()}
    entry["slot"] = 1
    paths.accounts_path().write_text(json.dumps({
        "version": 1, "activeSlot": 1, "accounts": [entry],
    }), encoding="utf-8")
    store = AccountStore.load()
    assert store.get(1) == blank
    assert store.active_slot == 1
    # Pin tolerant optional metadata; corrupt slot/envelope fields quarantine instead.
    assert list(swap_home.glob("accounts.json.corrupt-*")) == []
    store.save()
    assert AccountStore.load().get(1) == blank


def test_auto_state_malformed_nested_entries_do_not_poison_consumers():
    paths.state_path().write_text(json.dumps({
        "lastSwitchAt": False, "cooldownUntil": "NaN",
        "unhealthy": {"not-a-slot": 1, "1": [], "2": {}},
        "redemptions": [None, [], {"at": []}, {"at": {}}, {"at": "Infinity"}],
    }), encoding="utf-8")
    state = auto.AutoState.load()
    assert state == auto.AutoState()
    assert state.redeemed_last_24h(NOW) == 0
    state.prune(NOW)
    state.save()
    assert auto.AutoState.load() == state


@pytest.mark.parametrize("filename", STATE_FILES)
def test_future_state_fields_do_not_break_loaders(filename, swap_home, tmp_path):
    future = {"futureVersionData": {"nested": [None, True, {"k": "값"}]}}
    if filename == "accounts.json":
        data = {"version": 1, "activeSlot": None, "accounts": [], **future}
    elif filename == "settings.json":
        data = {"autoswitch": {"threshold": 90, **future}, **future}
    elif filename == "mappings.json":
        data = {mappings.normalise_path(tmp_path): 1, **future}
    elif filename == "usage-cache.json":
        store = AccountStore.load()
        store.add_from_auth(make_auth(), slot=1)
        data = {"1": {**snapshot().to_dict(), **future}, **future}
    else:
        data = {**auto.AutoState().to_dict(), **future}
    path = swap_home / filename
    path.write_text(json.dumps(data), encoding="utf-8")
    if filename == "settings.json":
        restored = Settings.load()
        restored.set("autoswitch.threshold", "91")
        restored.save()
        data["autoswitch"]["threshold"] = 91
        assert read_json(path) == data
        restored = Settings.load()
        restored.unset("autoswitch.threshold")
        restored.save()
        del data["autoswitch"]["threshold"]
        assert read_json(path) == data
    elif filename == "accounts.json":
        restored = AccountStore.load()
        assert restored.accounts == {}
        restored.save()
        assert AccountStore.load().accounts == {}
    elif filename == "state.json":
        restored = auto.AutoState.load()
        assert restored == auto.AutoState()
        restored.save()
        assert auto.AutoState.load() == restored
    elif filename == "mappings.json":
        restored = mappings.load_mappings()
        assert restored == {mappings.normalise_path(tmp_path): 1}
        mappings.save_mappings(restored)
        assert mappings.load_mappings() == restored
    else:
        assert store.cached_usage(1) == snapshot()
        store.record_usage(1, snapshot(20))
        assert AccountStore.load().cached_usage(1) == snapshot(20)


@pytest.mark.parametrize("bad_entry", [
    {"slot": 1}, {"slot": -1}, {"slot": 0}, {"slot": "2"},
    {"slot": 2.5}, {"slot": True}, {},
], ids=["duplicate", "negative", "zero", "string", "float", "boolean", "missing"])
def test_invalid_registry_slots_quarantine_without_touching_credentials(
    two_accounts, swap_home, bad_entry, capsys,
):
    credentials = {slot: paths.slot_auth_path(slot).read_bytes() for slot in (1, 2)}
    document = {"version": 1, "activeSlot": 1, "accounts": [{"slot": 1}, bad_entry]}
    original = json.dumps(document).encode("utf-8")
    paths.accounts_path().write_bytes(original)
    restored = AccountStore.load()
    assert restored.accounts == {}
    assert restored.active_slot is None
    quarantined = list(swap_home.glob("accounts.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == original
    assert not paths.accounts_path().exists()
    assert "warning" in capsys.readouterr().err.lower()
    assert {slot: paths.slot_auth_path(slot).read_bytes() for slot in (1, 2)} == credentials


def test_concurrent_store_saves_always_produce_json(two_accounts):
    stores = [AccountStore.load(), AccountStore.load()]
    gate = threading.Barrier(3, timeout=2)
    started = time.monotonic()
    failures = []

    def writer(index):
        store = stores[index]
        try:
            for iteration in range(30):
                store.get(1).alias = str(iteration) + ("a" if index == 0 else "b") * (128 + index * 2048)
                gate.wait()
                store.save()
                gate.wait()
                gate.wait()
        except Exception as exc:
            failures.append(exc)
            gate.abort()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(writer, index) for index in (0, 1)]
        try:
            for _ in range(30):
                gate.wait()
                gate.wait()
                # Read after each overlapping pair of differently sized writes.
                data = read_json(paths.accounts_path())
                assert data["version"] == 1
                assert [entry["slot"] for entry in data["accounts"]] == [1, 2]
                gate.wait()
        except threading.BrokenBarrierError:
            # Surface a writer's real exception, not a secondary barrier timeout.
            for failure in failures:
                if not isinstance(failure, threading.BrokenBarrierError):
                    raise failure from None
            raise
        except BaseException:
            gate.abort()
            raise
        for future in futures:
            future.result(timeout=2)
    assert time.monotonic() - started < 5


def test_file_lock_second_thread_waits_then_succeeds(swap_home):
    attempted = threading.Event()
    acquired = threading.Event()

    def contender():
        attempted.set()
        with locking.FileLock(swap_home / ".lock", timeout=1, poll=0.005):
            acquired.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with locking.FileLock(swap_home / ".lock"):
            future = pool.submit(contender)
            assert attempted.wait(1)
            acquired_while_held = acquired.wait(0.15)
        future.result(timeout=2)
    assert not acquired_while_held, "Another thread entered a held critical section"
    assert acquired.is_set()


def test_file_lock_contending_thread_times_out(swap_home):
    def contender():
        started = time.monotonic()
        contender_lock = locking.FileLock(swap_home / ".lock", timeout=0.1, poll=0.005)
        with pytest.raises(errors.LockBusy), contender_lock:
            pass
        assert time.monotonic() - started >= 0.09

    with ThreadPoolExecutor(max_workers=1) as pool, locking.FileLock(swap_home / ".lock"):
        pool.submit(contender).result(timeout=2)
    with locking.FileLock(swap_home / ".lock", timeout=0.1):
        pass


def test_file_lock_reentrant_same_thread_and_path(swap_home):
    lock_path = swap_home / ".lock"
    equivalent = locking.FileLock(swap_home / "." / ".lock", timeout=0.1)
    with locking.FileLock(lock_path, timeout=0.1) as outer, outer, equivalent:
        assert lock_path.exists()
    with locking.FileLock(lock_path, timeout=0.1):
        pass


def test_lock_file_left_by_dead_process_does_not_block(swap_home):
    lock_path = swap_home / ".lock"
    # OS advisory locks are released on process exit. The file's continued
    # existence is not ownership; pin this behaviour without inventing a PID/TTL.
    child = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from codexswap.locking import FileLock\n"
        "with FileLock(Path(sys.argv[2]), timeout=0.5):\n"
        "    os._exit(0)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", child, str(Path(locking.__file__).resolve().parents[1]), str(lock_path)],
        capture_output=True, text=True, timeout=5, check=True,
    )
    assert completed.returncode == 0
    assert lock_path.is_file()
    with locking.FileLock(lock_path, timeout=0.3):
        pass


def test_concurrent_record_usage_preserves_both_slots(two_accounts, monkeypatch):
    first_read = threading.Event()
    second_finished = threading.Event()
    original_read = two_accounts.read_usage_cache
    counter_guard = threading.Lock()
    read_count = 0

    def delayed_read():
        nonlocal read_count
        cache = original_read()
        with counter_guard:
            read_count += 1
            first = read_count == 1
        if first:
            first_read.set()
            # A bounded pause exposes read/modify/write overlap. Correct locking
            # simply waits out this pause; no barrier inside the lock can deadlock it.
            second_finished.wait(0.2)
        return cache

    monkeypatch.setattr(two_accounts, "read_usage_cache", delayed_read)
    usages = {1: snapshot(12), 2: snapshot(34, account_id="second")}

    def record_second():
        try:
            two_accounts.record_usage(2, usages[2])
        finally:
            second_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(two_accounts.record_usage, 1, usages[1])
        assert first_read.wait(1)
        second = pool.submit(record_second)
        first.result(timeout=2)
        second.result(timeout=2)
    cached = read_json(paths.usage_cache_path())
    assert set(cached) == {"1", "2"}, "Concurrent read/modify/write discarded a slot's usage"
    restored = AccountStore.load()
    for slot, usage in usages.items():
        assert restored.cached_usage(slot) == usage
        assert restored.get(slot).last_seen_usage == usage


@pytest.mark.parametrize("component", ["with spaces", "hash#directory", "한글폴더", "trailing"])
@pytest.mark.parametrize("source", ["environment", "override"])
def test_unusual_home_and_mapping_paths_work(tmp_path, monkeypatch, component, source):
    root = tmp_path / component
    spelling = str(root) + (os.sep if component == "trailing" else "")
    if source == "environment":
        monkeypatch.setenv("CODEXSWAP_HOME", spelling)
    else:
        paths.set_home_override(spelling)
    assert paths.codexswap_home() == root
    store = AccountStore.load()
    auth = make_auth()
    account = store.add_from_auth(auth, slot=3)
    assert read_json(paths.slot_auth_path(3)) == auth
    assert paths.slot_home(3).is_dir()
    assert AccountStore.load().get(3) == account
    project = root / "project # 한글"
    project.mkdir()
    mappings.set_mapping(str(project) + os.sep, 3)
    assert mappings.lookup(project / "nonexistent child") == 3
    assert mappings.load_mappings() == {mappings.normalise_path(project): 3}
    assert mappings.remove_mapping(project)
    assert mappings.load_mappings() == {}


@pytest.mark.parametrize("spelling", ["relative", ".", "..", "missing/deep/path", "trailing/"])
def test_normalise_path_edge_cases_are_absolute(tmp_path, monkeypatch, spelling):
    monkeypatch.chdir(tmp_path)
    normalised = mappings.normalise_path(spelling)
    assert Path(normalised).is_absolute()
    assert normalised == mappings.normalise_path(tmp_path / spelling)
    assert normalised == mappings.normalise_path(normalised + os.sep)


@pytest.mark.skipif(os.name != "nt", reason="Windows paths are case insensitive")
def test_normalise_windows_letter_case_is_equivalent(tmp_path):
    mixed = str(tmp_path / "MixedCase" / "한글")
    assert mappings.normalise_path(mixed.upper()) == mappings.normalise_path(mixed.lower())
    assert Path(mappings.normalise_path(mixed.upper())).is_absolute()


def test_slot_paths_near_platform_limit(tmp_path):
    # Construct only: this probes the path helpers without requiring Windows long
    # path support or creating a 4 KB directory tree that pytest must later remove.
    limit = 260 if os.name == "nt" else os.pathconf(str(tmp_path), "PC_PATH_MAX")
    target_length = limit - 16
    root = tmp_path
    suffix_length = len(str(Path("homes") / "123" / "auth.json")) + 1
    while len(str(root)) + suffix_length < target_length:
        remaining = target_length - len(str(root)) - suffix_length - 1
        if remaining < 1:
            break
        root /= "x" * min(100, remaining)
    paths.set_home_override(root)
    assert paths.slot_home(123) == root / "homes" / "123"
    assert paths.slot_auth_path(123) == root / "homes" / "123" / "auth.json"
    assert target_length - 1 <= len(str(paths.slot_auth_path(123))) <= target_length


@pytest.mark.parametrize("key,minimum,maximum", NUMERIC_BOUNDS)
@pytest.mark.parametrize("boundary", ["min", "below", "max", "above"])
def test_numeric_setting_boundaries(key, minimum, maximum, boundary):
    settings = Settings.load()
    value = {"min": minimum, "below": minimum - 1, "max": maximum, "above": maximum + 1}[boundary]
    if boundary in ("min", "max"):
        assert settings.set(key, str(value)) == value
        assert type(settings.get(key)) is int
        settings.save()
        assert Settings.load().get(key) == value
    else:
        before = settings.items()
        with pytest.raises(errors.UserError):
            settings.set(key, str(value))
        assert settings.items() == before


def test_numeric_boundaries_cover_every_numeric_setting():
    assert {key for key, _, _ in NUMERIC_BOUNDS} == {
        key for key, spec in SPECS.items() if spec.type == "int"
    }


@pytest.mark.parametrize("policy", ["always", "expiring", "exhausted"])
def test_zero_reset_daily_cap_is_unlimited(policy):
    settings = Settings.load()
    settings.set("reset.maxPerDay", "0")
    settings.set("reset.policy", policy)
    usage = snapshot(100)
    credit = replace(usage.reset_credits[0], expires_at=int(NOW + 60))
    usage = replace(usage, reset_credits=(credit,))
    decision = resets.decide(
        usage, settings, now=NOW, redeemed_last_24h=1000, alternatives_available=False,
    )
    assert decision.should_redeem
    assert decision.credit == credit


@pytest.mark.parametrize("selection", ["best", "next-available"])
@pytest.mark.parametrize("hysteresis", [20, 21])
def test_hysteresis_at_or_above_threshold_restricts_targets(two_accounts, selection, hysteresis):
    settings = Settings.load()
    settings.set("autoswitch.threshold", "20")
    settings.set("autoswitch.hysteresisPct", str(hysteresis))
    candidates = [strategy.Candidate(two_accounts.get(1), snapshot(0)),
                  strategy.Candidate(two_accounts.get(2), snapshot(1))]
    target = strategy.pick_target(
        candidates, current_slot=None, strategy=selection,
        threshold=settings.threshold, hysteresis=settings.hysteresis_pct,
    )
    assert target == (two_accounts.get(1) if hysteresis == 20 else None)
    # Unknown usage remains eligible by CONTRACT section 8, even at these bounds.
    candidates[1] = strategy.Candidate(two_accounts.get(2), None)
    assert strategy.pick_target(
        candidates, current_slot=1, strategy=selection,
        threshold=settings.threshold, hysteresis=settings.hysteresis_pct,
    ) == two_accounts.get(2)


def test_force_import_rejects_noncredential_before_overwrite(two_accounts, tmp_path):
    export = tmp_path / "invalid-export.json"
    export.write_text(json.dumps({
        "format": "codexswap-export", "version": 1, "activeSlot": 1,
        "accounts": [{"slot": 1, "auth": {"hello": 1}}],
    }), encoding="utf-8")
    before_auth = paths.slot_auth_path(1).read_bytes()
    before_registry = paths.accounts_path().read_bytes()
    try:
        with pytest.raises((errors.AuthFileInvalid, errors.UserError)):
            transfer.import_accounts(two_accounts, export, force=True)
    finally:
        assert paths.slot_auth_path(1).read_bytes() == before_auth, "Import destroyed a stored credential"
        assert paths.accounts_path().read_bytes() == before_registry
