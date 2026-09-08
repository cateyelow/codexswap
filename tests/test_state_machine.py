from __future__ import annotations

import copy
import json
import random
import time

import pytest
from conftest import make_auth

from codexswap import paths, switcher
from codexswap.models import UsageSnapshot
from codexswap.store import AccountStore

SEED = 0xC0DE5A9
STEPS = 330
NOW = 1_789_777_228.0
OPERATIONS = (
    "add", "remove", "switch", "disable", "enable", "alias", "swap_slots",
    "move_slot", "record_usage", "save", "reload",
)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def assert_invariants(store, expected, expected_active, removed_slots):
    document = read_json(paths.accounts_path())
    slots = [entry["slot"] for entry in document["accounts"]]
    assert all(type(slot) is int and slot > 0 for slot in slots)
    assert len(slots) == len(set(slots))
    assert set(slots) == set(store.accounts) == set(expected)
    assert store.active_slot is None or store.active_slot in store.accounts
    assert document["activeSlot"] == store.active_slot == expected_active
    assert paths.homes_dir().is_dir()
    for home in paths.homes_dir().iterdir():
        if home.is_dir():
            assert home.name.isdecimal(), f"Unexpected leftover directory: {home.name}"
            assert int(home.name) in set(expected) | removed_slots
    for slot, model in expected.items():
        assert paths.slot_home(slot).is_dir()
        # An independent credential model catches swaps/loss hidden by valid JSON.
        assert read_json(paths.slot_auth_path(slot)) == model["auth"]
        account = store.get(slot)
        assert account.slot == slot
        assert account.identity.account_id == model["auth"]["tokens"]["account_id"]
        assert account.alias == model["alias"]
        assert account.disabled is model["disabled"]
        assert account.last_seen_usage == model["usage"]
        assert store.cached_usage(slot) == model["usage"]
    for auth_path in paths.homes_dir().rglob("auth.json"):
        assert isinstance(read_json(auth_path), dict)
    if paths.live_auth_path().exists():
        assert isinstance(read_json(paths.live_auth_path()), dict)
    restored = AccountStore.load()
    assert restored.active_slot == store.active_slot
    assert [a.to_dict() for a in restored.ordered()] == [a.to_dict() for a in store.ordered()]


def test_deterministic_account_state_machine(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("The state machine must never launch or probe Codex")

    monkeypatch.setattr(switcher, "detect_running_codex", forbidden)
    # Freeze builder timestamps and registry timestamps for an exact replay.
    monkeypatch.setattr(time, "time", lambda: NOW)
    monkeypatch.setattr(paths, "iso_now", lambda: "2026-09-09T00:00:00Z")
    rng = random.Random(SEED)
    store = AccountStore.load()
    expected = {}
    expected_active = None
    removed_slots = set()
    history = []
    serial = 0
    started = time.monotonic()

    def add(slot):
        nonlocal serial
        serial += 1
        alias = f"account-{serial}"
        history.append({"op": "add", "slot": slot, "identity": serial, "alias": alias})
        auth = make_auth(email=f"machine-{serial}@example.com", account_id=f"machine-{serial}")
        auth["tokens"]["refresh_token"] = f"rt.1.TESTONLY-{serial}"
        auth["futureCredentialField"] = {"serial": serial}
        store.add_from_auth(auth, slot=slot, alias=alias)
        expected[slot] = {"auth": copy.deepcopy(auth), "alias": alias, "disabled": False, "usage": None}
        removed_slots.discard(slot)

    try:
        for slot in (1, 2, 3):
            add(slot)
            assert_invariants(store, expected, expected_active, removed_slots)
        # Each shuffled block covers all operations while their order and operands
        # remain random. Replays are independent of hash iteration and wall time.
        schedule = []
        for _ in range(STEPS // len(OPERATIONS)):
            block = list(OPERATIONS)
            rng.shuffle(block)
            schedule.extend(block)
        for step, operation in enumerate(schedule):
            if operation == "add" or (not expected and operation not in ("save", "reload")):
                add(next(slot for slot in range(1, 100) if slot not in expected))
            else:
                slot = rng.choice(sorted(expected)) if expected else None
                event = {"op": operation, "slot": slot}
                history.append(event)
                if operation == "remove":
                    store.remove(slot)
                    del expected[slot]
                    removed_slots.add(slot)
                    if expected_active == slot:
                        expected_active = None
                elif operation == "switch":
                    switcher.activate(store, store.get(slot), force=True)
                    expected_active = slot
                    assert read_json(paths.live_auth_path()) == expected[slot]["auth"]
                elif operation in ("disable", "enable"):
                    disabled = operation == "disable"
                    store.set_disabled(slot, disabled)
                    expected[slot]["disabled"] = disabled
                elif operation == "alias":
                    alias = rng.choice([None, f"alias-{step}", f"별명 {step} #"])
                    event["alias"] = alias
                    store.set_alias(slot, alias)
                    expected[slot]["alias"] = alias
                elif operation in ("swap_slots", "move_slot"):
                    if operation == "swap_slots":
                        destination = rng.choice(sorted(expected))
                        event["destination"] = destination
                        store.swap_slots(slot, destination)
                    else:
                        destination = rng.randint(1, 12)
                        event["destination"] = destination
                        store.move_slot(slot, destination)
                    if destination in expected:
                        expected[slot], expected[destination] = expected[destination], expected[slot]
                        if expected_active == destination:
                            expected_active = slot
                        elif expected_active == slot:
                            expected_active = destination
                    else:
                        expected[destination] = expected.pop(slot)
                        if expected_active == slot:
                            expected_active = destination
                    removed_slots.discard(destination)
                elif operation == "record_usage":
                    percent = rng.randint(0, 100)
                    event["percent"] = percent
                    usage = UsageSnapshot.from_dict({
                        "fetchedAt": NOW + step, "accountId": expected[slot]["auth"]["tokens"]["account_id"],
                        "primary": {"usedPercent": percent, "windowMinutes": 300, "resetsAt": NOW + 3600},
                    })
                    store.record_usage(slot, usage)
                    expected[slot]["usage"] = usage
                elif operation == "save":
                    store.save()
                elif operation == "reload":
                    store = AccountStore.load()
                else:
                    raise AssertionError(f"Unhandled operation: {operation}")
            # No unconditional save here: it would conceal missing persistence.
            assert_invariants(store, expected, expected_active, removed_slots)
        assert {event["op"] for event in history} == set(OPERATIONS)
        # A blow-up guard, not a performance target. Each mutation fsyncs, which costs
        # about 10 ms on a quiet Windows box and several times that on a loaded one or
        # a shared CI runner, so the floor here is roughly 330 * 30 ms. The budget is
        # set well above that: it still catches an accidental O(n^2), which is what
        # this assertion is for, without failing because the machine was busy.
        elapsed = time.monotonic() - started
        assert elapsed < 60, f"{elapsed:.1f}s for {len(schedule) + 3} operations"
    except Exception:
        print(f"Replay seed: {SEED} ({SEED:#x})")
        print("Exact operation sequence (synthetic identity numbers, no tokens):")
        print(json.dumps(history, ensure_ascii=True, indent=2))
        raise
