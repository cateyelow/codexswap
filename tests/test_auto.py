from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime

import pytest
from conftest import RATE_LIMITS_RESULT, make_auth

from codexswap import auto, errors
from codexswap.models import PerLimitUsage, RateLimitWindow, UsageSnapshot
from codexswap.settings import Settings
from codexswap.store import AccountStore

NOW = 1_788_912_000.0
DAY = 86400


def usage(percent, *, fetched_at=NOW):
    snapshot = UsageSnapshot.from_api(RATE_LIMITS_RESULT, fetched_at=fetched_at)
    return replace(snapshot, primary=RateLimitWindow(percent, 10080, int(NOW + DAY)))


class Scenario:
    def __init__(self):
        self.store = AccountStore.load()
        for number in (1, 2):
            self.store.add_from_auth(
                make_auth(email=f"person{number}@example.com", account_id=f"acct-{number}",
                          id_exp=int(NOW + 3600), access_exp=int(NOW + DAY)), now=NOW,
            )
        self.store.set_active(1)
        self.settings = Settings.load()
        self.settings.set("reset.policy", "never")
        self.state = auto.AutoState()
        self.snapshots = {1: usage(90), 2: usage(20)}
        self.probed = []
        self.redeemed = []
        self.activated = []
        self.outcome = "reset"

    def probe(self, account):
        self.probed.append(account.slot)
        value = self.snapshots[account.slot]
        if isinstance(value, Exception):
            raise value
        return value

    def redeem(self, account, credit_id):
        self.redeemed.append((account.slot, credit_id))
        return self.outcome

    def activate(self, account):
        self.activated.append(account.slot)

    def tick(self, *, now, dry_run=False, journal=None):
        return auto.tick(self.store, self.settings, self.state, now=now,
                         probe=self.probe, redeemer=self.redeem, activator=self.activate,
                         dry_run=dry_run, journal=journal)


@pytest.fixture
def scenario():
    return Scenario()


@pytest.mark.parametrize("action", ["disabled", "no-accounts", "cooldown"])
def test_early_actions_do_not_probe_or_mutate(scenario, action):
    if action == "disabled":
        scenario.settings.set("autoswitch.enabled", "false")
    elif action == "no-accounts":
        for account in scenario.store.ordered():
            scenario.store.set_disabled(account.slot, True)
    else:
        scenario.state.cooldown_until = NOW + 60
    before = scenario.state.to_dict()
    assert scenario.tick(now=NOW).action == action
    assert scenario.probed == scenario.redeemed == scenario.activated == []
    assert scenario.state.to_dict() == before


def test_empty_store_has_no_accounts_action(scenario, tmp_path):
    scenario.store = AccountStore.load(tmp_path / "empty")
    assert scenario.tick(now=NOW).action == "no-accounts"
    assert scenario.probed == []


def test_successful_probe_is_idle_and_resets_unhealthy_counter(scenario):
    scenario.state.unhealthy[1] = 2
    scenario.snapshots[1] = usage(79)
    result = scenario.tick(now=NOW)
    assert result.action == "idle"
    assert result.slot == 1
    assert scenario.state.unhealthy[1] == 0
    assert scenario.probed == [1]
    assert scenario.activated == scenario.redeemed == []


def test_unhealthy_failures_escalate_until_selection(scenario):
    scenario.settings.set("autoswitch.unhealthyTicks", "3")
    scenario.snapshots[1] = errors.AppServerError("probe fixture failed")
    for failures in (1, 2):
        result = scenario.tick(now=NOW + failures)
        assert result.action == "probe-failed"
        assert scenario.state.unhealthy[1] == failures
        assert scenario.activated == []
    result = scenario.tick(now=NOW + 3)
    assert result.action == "switched"
    assert result.slot == 2
    assert scenario.state.unhealthy[1] == 3
    assert scenario.probed == [1, 1, 1, 2]
    assert scenario.activated == [2]
    assert scenario.redeemed == []


def test_success_between_failures_restarts_unhealthy_count(scenario):
    scenario.snapshots[1] = errors.AppServerError("fixture failure")
    assert scenario.tick(now=NOW).action == "probe-failed"
    scenario.snapshots[1] = usage(10)
    assert scenario.tick(now=NOW + 1).action == "idle"
    assert scenario.state.unhealthy[1] == 0
    scenario.snapshots[1] = errors.AppServerError("another failure")
    assert scenario.tick(now=NOW + 2).action == "probe-failed"
    assert scenario.state.unhealthy[1] == 1


def test_switch_records_action_time_and_cooldown(scenario):
    result = scenario.tick(now=NOW)
    assert result.action == "switched"
    assert result.slot == 2
    assert scenario.activated == [2]
    assert scenario.redeemed == []
    assert scenario.state.last_switch_at == NOW
    assert scenario.state.cooldown_until == NOW + scenario.settings.get("autoswitch.cooldownSeconds")


def test_no_target_when_alternatives_fail_hysteresis(scenario):
    scenario.snapshots[2] = usage(71)
    assert scenario.tick(now=NOW).action == "no-target"
    assert scenario.activated == scenario.redeemed == []
    assert scenario.state.cooldown_until is None


def test_successful_redemption_records_one_entry_and_cooldown(scenario):
    scenario.settings.set("reset.policy", "always")
    result = scenario.tick(now=NOW)
    assert result.action == "redeemed"
    assert result.slot == 1
    assert scenario.redeemed == [(1, "RateLimitResetCredit_soonest")]
    assert scenario.activated == []
    assert len(scenario.state.redemptions) == 1
    entry = scenario.state.redemptions[0]
    assert entry["at"] == NOW
    assert entry["slot"] == 1
    assert entry["creditId"] == "RateLimitResetCredit_soonest"
    assert scenario.state.cooldown_until == NOW + scenario.settings.get("autoswitch.cooldownSeconds")
    assert scenario.state.last_switch_at is None


@pytest.mark.parametrize("action,policy", [("switched", "never"), ("redeemed", "always")])
def test_dry_run_decides_without_mutating_action_history(scenario, action, policy):
    scenario.settings.set("reset.policy", policy)
    scenario.state.last_switch_at = NOW - 1000
    scenario.state.cooldown_until = NOW - 700
    scenario.state.redemptions = [{"at": NOW - 2 * DAY, "slot": 2, "creditId": "old"}]
    history = [dict(entry) for entry in scenario.state.redemptions]
    result = scenario.tick(now=NOW, dry_run=True)
    assert result.action == action
    assert result.detail.startswith("[dry-run] ")
    assert scenario.activated == scenario.redeemed == []
    assert scenario.state.last_switch_at == NOW - 1000
    assert scenario.state.cooldown_until == NOW - 700
    assert scenario.state.redemptions == history
    assert scenario.probed == [1, 2]


def test_no_credit_outcome_costs_no_allowance_and_falls_through_to_switch(scenario):
    scenario.settings.set("reset.policy", "always")
    scenario.outcome = "noCredit"
    result = scenario.tick(now=NOW)
    assert result.action == "switched"
    assert scenario.redeemed == [(1, "RateLimitResetCredit_soonest")]
    # The attempt is written down, because a crash before the answer must still be
    # visible. noCredit is the one outcome that proves nothing was spent, so it is
    # the one outcome that does not consume the daily allowance.
    assert [entry["outcome"] for entry in scenario.state.redemptions] == ["noCredit"]
    assert scenario.state.redeemed_last_24h(NOW) == 0
    assert scenario.activated == [2]
    assert scenario.state.last_switch_at == NOW


@pytest.mark.parametrize("outcome,counts", [
    ("reset", 1), ("alreadyRedeemed", 1), ("nothingToReset", 1), ("noCredit", 0),
])
def test_attempt_outcomes_that_cannot_prove_the_credit_survived_use_the_cap(
    scenario, outcome, counts,
):
    scenario.settings.set("reset.policy", "always")
    scenario.outcome = outcome
    scenario.tick(now=NOW)
    assert scenario.state.redeemed_last_24h(NOW) == counts


def test_crashed_attempt_left_pending_still_uses_the_cap(scenario):
    # A process that dies between the consume request and its answer leaves this.
    scenario.state.redemptions.append({
        "at": NOW - 60, "slot": 1, "creditId": "credit-x", "outcome": "pending",
    })
    assert scenario.state.redeemed_last_24h(NOW) == 1
    scenario.settings.set("reset.policy", "always")
    result = scenario.tick(now=NOW)
    assert result.action == "switched"
    assert scenario.redeemed == []


def test_no_active_slot_bootstraps_to_best_target(scenario):
    scenario.store.set_active(None)
    result = scenario.tick(now=NOW)
    assert result.action == "switched"
    assert result.slot == 2
    assert scenario.activated == [2]
    assert scenario.redeemed == []


@pytest.mark.parametrize("age,expected_probes", [(119, [1]), (121, [1, 2])])
def test_alternative_cache_respects_stale_seconds(scenario, age, expected_probes):
    scenario.settings.set("probe.staleSeconds", "120")
    scenario.store.record_usage(2, usage(20, fetched_at=NOW - age))
    scenario.snapshots[2] = usage(95)
    result = scenario.tick(now=NOW)
    assert scenario.probed == expected_probes
    assert result.action == ("switched" if age == 119 else "no-target")
    assert scenario.activated == ([2] if age == 119 else [])


def test_auto_state_json_round_trip_preserves_integer_unhealthy_keys():
    state = auto.AutoState(
        last_switch_at=NOW - 10, cooldown_until=NOW + 290, unhealthy={1: 2, 4: 0},
        redemptions=[{"at": NOW - 20, "slot": 1, "creditId": "fixture-credit"}],
    )
    restored = auto.AutoState.from_dict(json.loads(json.dumps(state.to_dict())))
    assert restored == state
    assert restored.unhealthy == {1: 2, 4: 0}
    assert all(type(slot) is int for slot in restored.unhealthy)


def test_redeemed_last_24h_only_counts_recent_entries():
    state = auto.AutoState(redemptions=[
        {"at": timestamp, "slot": 1, "creditId": str(index)}
        for index, timestamp in enumerate([NOW, NOW - 10, NOW - DAY, NOW - DAY - 1, NOW + 1])
    ])
    assert state.redeemed_last_24h(now=NOW) == 3


def test_prune_drops_40_day_old_entries():
    old = {"at": NOW - 40 * DAY, "slot": 1, "creditId": "old"}
    recent = {"at": NOW - DAY, "slot": 2, "creditId": "recent"}
    state = auto.AutoState(redemptions=[old, recent])
    state.prune(now=NOW)
    assert state.redemptions == [recent]


def test_prune_clamps_long_history_and_retains_newest():
    # Spread over the retention window, so the count cap is the thing being tested.
    entries = [{"at": NOW - index * 1000, "slot": 1, "creditId": str(index)}
               for index in range(2000)]
    state = auto.AutoState(redemptions=entries.copy())
    state.prune(now=NOW)
    assert 0 < len(state.redemptions) < len(entries)
    assert state.redemptions[-1]["creditId"] == "0"
    assert entries[0] in state.redemptions
    assert {entry["creditId"] for entry in state.redemptions} == {
        str(index) for index in range(len(state.redemptions))
    }


def test_prune_never_drops_a_spend_the_daily_cap_still_counts():
    spend = {"at": NOW - 100, "slot": 1, "creditId": "spent", "outcome": "reset"}
    noise = [{"at": NOW - 90 + index, "slot": 1, "creditId": str(index),
              "outcome": "noCredit"} for index in range(400)]
    state = auto.AutoState(redemptions=[spend] + noise)

    state.prune(now=NOW)

    assert state.redeemed_last_24h(NOW) == 1
    assert spend in state.redemptions
    # The bound still holds, because the attempts that do not count are droppable.
    assert len(state.redemptions) <= 201


def test_format_tick_is_a_stable_single_line():
    result = auto.TickResult("switched", "1 -> 2, 20%", 2)
    timestamp = datetime.fromtimestamp(NOW).astimezone().isoformat(timespec="seconds")
    rendered = auto.format_tick(result, now=NOW)
    assert rendered == f"[{timestamp}] switched: 1 -> 2, 20%"
    assert rendered.splitlines() == [rendered]
    assert auto.format_tick(result, now=NOW) == rendered


def test_shared_daily_cap_survives_a_second_process(scenario, swap_home):
    """Two daemons on one home must not each spend a credit against a cap of one."""
    scenario.settings.set("reset.policy", "always")
    scenario.settings.set("reset.maxPerDay", "1")

    # The other process redeemed a moment ago and wrote state.json. This process has
    # been running since before that, so its in-memory history is still empty.
    other = auto.AutoState(redemptions=[{
        "at": NOW - 30, "slot": 2, "creditId": "credit-other",
        "attempt": "other-process-attempt", "outcome": "reset",
    }])
    other.save()
    assert scenario.state.redemptions == []

    result = scenario.tick(
        now=NOW, journal=auto.RedemptionJournal(scenario.state),
    )
    assert scenario.redeemed == [], "Spent a credit the shared cap had already used"
    assert result.action == "switched"
    # The other process's entry is adopted rather than overwritten.
    assert [entry["creditId"] for entry in scenario.state.redemptions] == ["credit-other"]
    assert auto.AutoState.load().redemptions[0]["attempt"] == "other-process-attempt"


def test_journal_records_the_attempt_before_the_request_leaves(scenario, swap_home):
    scenario.settings.set("reset.policy", "always")
    recorded = []

    def crash(account, credit_id):
        # Exactly what a process killed mid-consume leaves behind on disk.
        recorded.append(auto.AutoState.load().redemptions)
        raise errors.AppServerTimeout("killed mid-consume")

    scenario.redeem = crash
    with pytest.raises(errors.AppServerTimeout):
        scenario.tick(now=NOW, journal=auto.RedemptionJournal(scenario.state))
    assert [entry["outcome"] for entry in recorded[0]] == ["pending"]
    assert auto.AutoState.load().redeemed_last_24h(NOW) == 1


def model_usage(percent, model_percent, *, limit_id="codex_bengalfox"):
    """A snapshot whose aggregate and named-model usage differ."""
    snapshot = usage(percent)
    return replace(snapshot, per_limit=(
        PerLimitUsage(
            limit_id=limit_id, limit_name="GPT-5.3-Codex-Spark",
            primary=RateLimitWindow(model_percent, 300, int(NOW + DAY)),
            secondary=None, plan_type="pro",
        ),
    ))


def test_exhausted_model_switches_an_account_that_looks_idle_in_total(scenario):
    scenario.settings.set("autoswitch.model", "codex_bengalfox")
    scenario.snapshots[1] = model_usage(20, 95)
    scenario.snapshots[2] = model_usage(30, 5)

    result = scenario.tick(now=NOW)

    assert result.action == "switched"
    assert scenario.activated == [2]


def test_the_same_account_stays_idle_without_the_model_setting(scenario):
    scenario.snapshots[1] = model_usage(20, 95)
    scenario.snapshots[2] = model_usage(30, 5)

    assert scenario.tick(now=NOW).action == "idle"
    assert scenario.activated == []


def test_a_candidate_whose_model_is_exhausted_is_not_a_target(scenario):
    scenario.settings.set("autoswitch.model", "codex_bengalfox")
    scenario.snapshots[1] = model_usage(90, 90)
    scenario.snapshots[2] = model_usage(5, 99)

    result = scenario.tick(now=NOW)

    assert result.action == "no-target"
    assert scenario.activated == []


def test_model_names_may_be_written_as_a_list(scenario):
    scenario.settings.set("autoswitch.model", "codex_nonesuch, GPT-5.3-Codex-Spark")
    scenario.snapshots[1] = model_usage(20, 95)
    scenario.snapshots[2] = model_usage(30, 5)

    assert scenario.tick(now=NOW).action == "switched"


def test_an_unreported_model_leaves_the_totals_in_charge(scenario):
    scenario.settings.set("autoswitch.model", "codex_nonesuch")
    scenario.snapshots[1] = model_usage(20, 95)

    assert scenario.tick(now=NOW).action == "idle"


def test_model_selection_does_not_change_the_reset_decision(scenario):
    # A credit clears the account-wide windows, so an exhausted model must not by
    # itself authorise spending one while the totals are still low.
    scenario.settings.set("reset.policy", "always")
    scenario.settings.set("reset.minUsagePercent", "50")
    scenario.settings.set("autoswitch.model", "codex_bengalfox")
    scenario.snapshots[1] = model_usage(20, 95)
    scenario.snapshots[2] = model_usage(30, 5)

    result = scenario.tick(now=NOW)

    assert scenario.redeemed == []
    assert result.action == "switched"


def _auto_accounts(scenario, snapshot_for, monkeypatch):
    """Wire the daemon's real probe adapter to a fake app-server."""
    from codexswap import appserver

    probed = []

    def probe_usage(home, **kwargs):
        probed.append(str(home))
        return snapshot_for(str(home))

    monkeypatch.setattr(appserver, "probe_usage", probe_usage)
    return auto._AutoAccounts(scenario.store, scenario.settings, NOW), probed


def test_daemon_probe_rejects_a_slot_that_answers_for_another_account(scenario, monkeypatch):
    accounts, probed = _auto_accounts(
        scenario, lambda home: replace(usage(10), account_id="acct-somebody-else"), monkeypatch,
    )

    with pytest.raises(errors.AppServerError) as caught:
        accounts.probe(scenario.store.get(1))

    assert "different account" in str(caught.value)
    assert probed  # it really did probe; the reading is what was refused
    # A refusal is remembered, so the same tick does not ask again.
    with pytest.raises(errors.AppServerError):
        accounts.probe(scenario.store.get(1))
    assert len(probed) == 1


def test_daemon_probe_accepts_the_matching_account(scenario, monkeypatch):
    accounts, probed = _auto_accounts(
        scenario, lambda home: replace(usage(10), account_id="acct-1"), monkeypatch,
    )

    assert accounts.probe(scenario.store.get(1)).percent_for() == 10
    assert len(probed) == 1


def test_daemon_probe_accepts_a_snapshot_with_no_account_id(scenario, monkeypatch):
    accounts, _ = _auto_accounts(
        scenario, lambda home: replace(usage(10), account_id=None), monkeypatch,
    )

    assert accounts.probe(scenario.store.get(1)).percent_for() == 10


def test_a_mismatched_active_account_never_redeems(scenario, monkeypatch):
    scenario.settings.set("reset.policy", "always")
    scenario.settings.set("reset.minUsagePercent", "0")
    accounts, _ = _auto_accounts(
        scenario, lambda home: replace(usage(95), account_id="acct-somebody-else"), monkeypatch,
    )

    result = auto.tick(accounts, scenario.settings, scenario.state, now=NOW,
                       probe=accounts.probe, redeemer=scenario.redeem,
                       activator=scenario.activate)

    assert scenario.redeemed == []
    # The first refusal is a probe failure, so the credit decision is never reached.
    assert result.action == "probe-failed"
    assert scenario.state.unhealthy[1] == 1


def _reserve(journal, *, at, slot, credit, cap, now):
    entry = {"at": at, "slot": slot, "creditId": credit,
             "attempt": f"attempt-{credit}", "outcome": "pending"}
    return entry, journal.reserve(entry, cap=cap, now=now)


def test_a_stale_save_cannot_erase_another_processes_spend(tmp_path):
    root = tmp_path / "shared"
    root.mkdir()
    a = auto.AutoState.load(root)
    b = auto.AutoState.load(root)          # loaded before A reserved anything
    journal_a = auto.RedemptionJournal(a, root=root, persist=True)

    entry, reserved = _reserve(journal_a, at=NOW, slot=1, credit="c1", cap=1, now=NOW)
    assert reserved
    entry["outcome"] = "reset"
    journal_a.settle(entry)
    assert auto.AutoState.load(root).redeemed_last_24h(NOW) == 1

    # B finishes its own tick and writes the state it loaded before A spent.
    b.save(root)

    assert auto.AutoState.load(root).redeemed_last_24h(NOW) == 1
    journal_b = auto.RedemptionJournal(auto.AutoState.load(root), root=root, persist=True)
    _, second = _reserve(journal_b, at=NOW + 1, slot=2, credit="c2", cap=1, now=NOW + 1)
    assert second is False


def test_a_settled_outcome_survives_a_concurrent_save(tmp_path):
    root = tmp_path / "outcome"
    root.mkdir()
    a = auto.AutoState.load(root)
    journal_a = auto.RedemptionJournal(a, root=root, persist=True)
    entry, _ = _reserve(journal_a, at=NOW, slot=1, credit="c1", cap=2, now=NOW)

    # B adopts the pending entry and reserves its own before A learns the outcome.
    b = auto.AutoState.load(root)
    journal_b = auto.RedemptionJournal(b, root=root, persist=True)
    _, second = _reserve(journal_b, at=NOW + 1, slot=2, credit="c2", cap=2, now=NOW + 1)
    assert second

    entry["outcome"] = "reset"
    journal_a.settle(entry)

    stored = auto.AutoState.load(root)
    outcomes = {item["creditId"]: item["outcome"] for item in stored.redemptions}
    assert outcomes == {"c1": "reset", "c2": "pending"}


def test_an_unreadable_history_refuses_to_reserve(tmp_path):
    root = tmp_path / "unreadable"
    root.mkdir()
    (root / "state.json").write_text("{ truncated", encoding="utf-8")
    state = auto.AutoState.load(root)
    assert state.unreadable is True

    journal = auto.RedemptionJournal(state, root=root, persist=True)
    _, reserved = _reserve(journal, at=NOW, slot=1, credit="c1", cap=1, now=NOW)

    assert reserved is False


def test_a_missing_history_is_not_treated_as_unreadable(tmp_path):
    root = tmp_path / "absent"
    root.mkdir()

    assert auto.AutoState.load(root).unreadable is False


def test_a_failed_alternative_probe_is_headroom_not_exhaustion(scenario):
    scenario.settings.set("reset.policy", "exhausted")
    scenario.settings.set("reset.minUsagePercent", "0")
    scenario.snapshots[1] = usage(99)
    scenario.snapshots[2] = errors.AppServerError("probe timed out")

    result = scenario.tick(now=NOW)

    # The other account may well have quota; nobody knows. A credit is not spent on
    # a guess, so the tick reports that it has nowhere to switch instead.
    assert scenario.redeemed == []
    assert result.action == "no-target"


def test_an_exhausted_alternative_still_permits_the_exhausted_policy(scenario):
    scenario.settings.set("reset.policy", "exhausted")
    scenario.settings.set("reset.minUsagePercent", "0")
    scenario.snapshots[1] = usage(99)
    scenario.snapshots[2] = usage(99)

    result = scenario.tick(now=NOW)

    assert result.action == "redeemed"
    assert [slot for slot, _ in scenario.redeemed] == [1]
