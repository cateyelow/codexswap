from __future__ import annotations

from dataclasses import replace

import pytest
from conftest import RATE_LIMITS_RESULT, make_auth

from codexswap import errors, identity, strategy
from codexswap.models import Account, RateLimitWindow, UsageSnapshot

NOW = 1_789_777_228


def candidate(slot, percent, *, disabled=False):
    account = Account(
        slot=slot,
        identity=identity.identity_from_auth(make_auth(
            email=f"account-{slot}@example.com", account_id=f"account-{slot}",
            id_exp=NOW + 3600, access_exp=NOW + 86400,
        )),
        disabled=disabled,
    )
    snapshot = None
    if percent is not None:
        snapshot = replace(
            UsageSnapshot.from_api(RATE_LIMITS_RESULT, fetched_at=NOW),
            primary=RateLimitWindow(percent, 10080, NOW + 86400), secondary=None,
        )
    return strategy.Candidate(account, snapshot)


def pick(candidates, *, current_slot=1, selection="best"):
    return strategy.pick_target(
        candidates, current_slot=current_slot, strategy=selection, threshold=80, hysteresis=10,
    )


def test_best_picks_lowest_usage_among_eligible_candidates():
    candidates = [candidate(4, 60), candidate(2, 20), candidate(3, 40), candidate(5, 71)]

    assert pick(candidates) is candidates[1].account


def test_best_breaks_usage_ties_by_lowest_slot():
    candidates = [candidate(5, 20), candidate(3, 20), candidate(2, 20)]

    assert pick(candidates) is candidates[2].account


def test_unknown_usage_is_eligible_but_ranks_after_known_sixty_percent():
    unknown, known = candidate(2, None), candidate(3, 60)

    assert unknown.percent is None
    assert strategy.eligible(unknown, current_slot=1, threshold=80, hysteresis=10) is True
    assert pick([unknown, known]) is known.account
    assert pick([unknown]) is unknown.account


def test_unknown_usage_ties_break_by_lowest_slot():
    candidates = [candidate(5, None), candidate(2, None)]

    assert pick(candidates) is candidates[1].account


@pytest.mark.parametrize("selection", ["best", "next-available"])
@pytest.mark.parametrize("percent,expected_eligible", [(69, True), (70, True), (71, False)])
def test_hysteresis_boundary(selection, percent, expected_eligible):
    target = candidate(2, percent)

    assert strategy.eligible(
        target, current_slot=1, threshold=80, hysteresis=10,
    ) is expected_eligible
    assert pick([target], selection=selection) is (target.account if expected_eligible else None)


@pytest.mark.parametrize("selection", ["best", "next-available"])
def test_current_and_disabled_accounts_are_never_selected(selection):
    current = candidate(1, 0)
    disabled = candidate(2, 0, disabled=True)
    enabled = candidate(3, 60)

    assert pick([current, disabled, enabled], selection=selection) is enabled.account


@pytest.mark.parametrize("selection", ["best", "next-available"])
def test_all_ineligible_returns_none(selection):
    candidates = [candidate(1, 0), candidate(2, None, disabled=True), candidate(3, 71)]

    assert pick(candidates, selection=selection) is None
    assert pick([], selection=selection) is None


def test_next_available_starts_after_current_and_skips_ineligible_slots():
    candidates = [
        candidate(5, 60), candidate(1, 0), candidate(4, 0, disabled=True),
        candidate(3, 71), candidate(2, 10),
    ]

    assert pick(candidates, current_slot=2, selection="next-available") is candidates[0].account


def test_next_available_wraps_from_slot_three_to_slot_one():
    candidates = [candidate(3, 0), candidate(2, 10), candidate(1, 60)]

    assert pick(candidates, current_slot=3, selection="next-available") is candidates[2].account


def test_next_available_skips_ineligible_after_wrapping():
    candidates = [candidate(3, 0), candidate(2, 60), candidate(1, 71)]

    assert pick(candidates, current_slot=3, selection="next-available") is candidates[1].account


def test_next_available_without_current_starts_at_lowest_slot():
    candidates = [candidate(3, 0), candidate(2, 10), candidate(1, 60)]

    assert pick(candidates, current_slot=None, selection="next-available") is candidates[2].account


def test_next_available_uses_slot_order_even_when_next_usage_is_unknown():
    candidates = [candidate(3, 10), candidate(2, None)]

    assert pick(candidates, selection="next-available") is candidates[1].account


def test_unknown_strategy_raises_user_error():
    with pytest.raises(errors.UserError, match="random|best|next-available"):
        pick([candidate(2, 20)], selection="random")


@pytest.mark.parametrize("selection", ["best", "next-available"])
def test_eligibility_agrees_with_selection_for_mixed_candidates(selection):
    candidates = [
        candidate(1, 0), candidate(2, None, disabled=True), candidate(3, 71),
        candidate(4, 70), candidate(5, None),
    ]
    flags = [strategy.eligible(item, current_slot=1, threshold=80, hysteresis=10)
             for item in candidates]

    assert flags == [False, False, False, True, True]
    assert pick(candidates, selection=selection) is candidates[3].account
    assert pick(candidates[:3], selection=selection) is None


def test_rotate_next_ignores_usage_and_skips_disabled_accounts():
    candidates = [candidate(4, 0), candidate(3, 100), candidate(1, 0),
                  candidate(2, 0, disabled=True)]
    for item in candidates:
        item.account.last_seen_usage = item.snapshot

    assert strategy.rotate_next([item.account for item in candidates], 1) is candidates[1].account


def test_rotate_next_wraps_to_lowest_enabled_slot():
    accounts = [candidate(3, 0).account, candidate(2, 100).account,
                candidate(1, 0, disabled=True).account]

    assert strategy.rotate_next(accounts, 3) is accounts[1]


def test_rotate_next_without_current_starts_at_lowest_enabled_slot():
    accounts = [candidate(3, 0).account, candidate(1, 100).account, candidate(2, 10).account]

    assert strategy.rotate_next(accounts, None) is accounts[1]


def test_rotate_next_returns_none_for_empty_or_current_only():
    assert strategy.rotate_next([], None) is None
    assert strategy.rotate_next([candidate(1, 0).account], 1) is None
    assert strategy.rotate_next([candidate(2, 0, disabled=True).account], 1) is None


def model_candidate(slot, percent, model_percent, *, models=()):
    """A candidate whose aggregate and named-model usage disagree."""
    from codexswap.models import PerLimitUsage

    base = candidate(slot, percent)
    snapshot = replace(base.snapshot, per_limit=(PerLimitUsage(
        limit_id="codex_bengalfox", limit_name="GPT-5.3-Codex-Spark",
        primary=RateLimitWindow(model_percent, 300, NOW + 86400),
        secondary=None, plan_type="pro",
    ),))
    return strategy.Candidate(base.account, snapshot, models)


def test_candidate_percent_defaults_to_the_aggregate():
    assert model_candidate(2, 30, 99).percent == 30


def test_candidate_percent_follows_the_named_model():
    assert model_candidate(2, 30, 99, models=("codex_bengalfox",)).percent == 99


def test_candidate_percent_is_unknown_without_a_snapshot():
    assert strategy.Candidate(candidate(2, 30).account, None, ("codex_bengalfox",)).percent is None


def test_an_exhausted_model_makes_a_candidate_ineligible():
    free = model_candidate(2, 30, 5, models=("codex_bengalfox",))
    busy = model_candidate(3, 5, 99, models=("codex_bengalfox",))

    assert pick([busy, free]) is free.account


def test_the_same_pair_picks_the_other_way_without_the_model():
    free = model_candidate(2, 30, 5)
    busy = model_candidate(3, 5, 99)

    assert pick([busy, free]) is busy.account
