from __future__ import annotations

import json
import uuid
from dataclasses import replace
from unittest.mock import MagicMock, Mock

import pytest
from conftest import RATE_LIMITS_RESULT

from codexswap import resets
from codexswap.models import UsageSnapshot
from codexswap.settings import Settings

# Two days before the shared payload's soonest credit expires.
NOW = 1_789_777_228
DAY = 86400
SOONEST_ID = "RateLimitResetCredit_soonest"


@pytest.fixture
def snapshot():
    return UsageSnapshot.from_api(RATE_LIMITS_RESULT, fetched_at=NOW)


@pytest.fixture
def settings():
    return Settings.load()


@pytest.fixture
def soonest(snapshot):
    return next(credit for credit in snapshot.reset_credits if credit.id == SOONEST_ID)


def decide(snapshot, settings, *, redeemed=0, alternatives=True):
    return resets.decide(
        snapshot, settings, now=NOW, redeemed_last_24h=redeemed,
        alternatives_available=alternatives,
    )


def assert_decision(decision, should_redeem, reason, credit=None):
    assert decision.should_redeem is should_redeem
    assert decision.reason == reason
    assert decision.credit == credit


def test_rule_1_policy_never(snapshot, settings):
    settings.set("reset.policy", "never")

    assert_decision(decide(snapshot, settings), False, "policy-never")


@pytest.mark.parametrize("status", [None, "redeeming", "redeemed"])
def test_rule_2_no_available_credits(snapshot, settings, status):
    credits = () if status is None else tuple(
        replace(credit, status=status) for credit in snapshot.reset_credits
    )

    assert_decision(decide(replace(snapshot, reset_credits=credits), settings), False, "no-credits")


@pytest.mark.parametrize("redeemed", [1, 2])
def test_rule_3_daily_cap(snapshot, settings, redeemed):
    settings.set("reset.maxPerDay", "1")

    assert_decision(decide(snapshot, settings, redeemed=redeemed), False, "daily-cap")


def test_rule_4_no_usage_data(snapshot, settings):
    snapshot = replace(snapshot, primary=None, secondary=None)

    assert_decision(decide(snapshot, settings), False, "no-usage-data")


@pytest.mark.parametrize("policy", ["expiring", "exhausted", "always"])
def test_rule_5_usage_too_low(snapshot, settings, soonest, policy):
    settings.set("reset.policy", policy)
    snapshot = replace(snapshot, primary=replace(snapshot.primary, used_percent=49))

    assert_decision(
        decide(snapshot, settings, alternatives=False), False, "usage-too-low", soonest,
    )


def test_rule_6_expiring_soon(snapshot, settings, soonest):
    assert_decision(decide(snapshot, settings), True, "expiring-soon", soonest)


def test_rule_6_not_expiring(snapshot, settings, soonest):
    settings.set("reset.expiryDays", "1")

    assert_decision(decide(snapshot, settings), False, "not-expiring", soonest)


def test_rule_7_all_accounts_exhausted(snapshot, settings, soonest):
    settings.set("reset.policy", "exhausted")
    settings.set("reset.expiryDays", "0")

    assert_decision(
        decide(snapshot, settings, alternatives=False), True, "all-accounts-exhausted", soonest,
    )


def test_rule_7_alternatives_available(snapshot, settings, soonest):
    settings.set("reset.policy", "exhausted")

    assert_decision(decide(snapshot, settings), False, "alternatives-available", soonest)


def test_rule_8_policy_always(snapshot, settings, soonest):
    settings.set("reset.policy", "always")
    settings.set("reset.expiryDays", "0")

    assert_decision(decide(snapshot, settings), True, "policy-always", soonest)


@pytest.mark.parametrize(
    "policy,has_credits,redeemed,percent,reason",
    [
        ("never", False, 100, None, "policy-never"),
        ("always", False, 100, None, "no-credits"),
        ("always", True, 100, None, "daily-cap"),
        ("always", True, 0, None, "no-usage-data"),
        ("always", True, 0, 49, "usage-too-low"),
    ],
)
def test_earlier_rule_wins_when_multiple_rules_apply(
    snapshot, settings, soonest, policy, has_credits, redeemed, percent, reason,
):
    settings.set("reset.policy", policy)
    snapshot = replace(
        snapshot,
        reset_credits=snapshot.reset_credits if has_credits else (),
        primary=None if percent is None else replace(snapshot.primary, used_percent=percent),
        secondary=None,
    )

    assert_decision(
        decide(snapshot, settings, redeemed=redeemed, alternatives=False), False, reason,
        soonest if reason == "usage-too-low" else None,
    )


def test_zero_max_per_day_means_unlimited(snapshot, settings, soonest):
    settings.set("reset.maxPerDay", "0")

    assert_decision(decide(snapshot, settings, redeemed=10000), True, "expiring-soon", soonest)


@pytest.mark.parametrize("percent", [50, 51])
def test_minimum_usage_is_inclusive(snapshot, settings, soonest, percent):
    snapshot = replace(snapshot, primary=replace(snapshot.primary, used_percent=percent))

    assert_decision(decide(snapshot, settings), True, "expiring-soon", soonest)


def test_binding_usage_considers_both_windows(snapshot, settings, soonest):
    snapshot = replace(
        snapshot, primary=replace(snapshot.primary, used_percent=10),
        secondary=replace(snapshot.primary, used_percent=80, window_minutes=300),
    )

    assert_decision(decide(snapshot, settings), True, "expiring-soon", soonest)


@pytest.mark.parametrize(
    "policy,reason",
    [("expiring", "expiring-soon"), ("exhausted", "all-accounts-exhausted"),
     ("always", "policy-always")],
)
@pytest.mark.parametrize("reverse_order", [False, True])
def test_chosen_credit_is_soonest_available_with_expiry_first(
    snapshot, settings, soonest, policy, reason, reverse_order,
):
    settings.set("reset.policy", policy)
    no_expiry = replace(soonest, id="RateLimitResetCredit_no_expiry", expires_at=None)
    redeemed = replace(soonest, id="RateLimitResetCredit_used", status="redeemed", expires_at=NOW)
    redeeming = replace(redeemed, id="RateLimitResetCredit_busy", status="redeeming")
    credits = (no_expiry, redeemed, redeeming, *snapshot.reset_credits)
    snapshot = replace(snapshot, reset_credits=credits[::-1] if reverse_order else credits)

    assert_decision(decide(snapshot, settings, alternatives=False), True, reason, soonest)


@pytest.mark.parametrize(
    "policy,should_redeem,reason",
    [("expiring", False, "not-expiring"), ("always", True, "policy-always")],
)
def test_credit_without_expiry_never_triggers_expiring_soon(
    snapshot, settings, soonest, policy, should_redeem, reason,
):
    settings.set("reset.policy", policy)
    settings.set("reset.expiryDays", "30")
    credit = replace(soonest, expires_at=None)
    snapshot = replace(snapshot, reset_credits=(credit,))

    assert_decision(decide(snapshot, settings), should_redeem, reason, credit)


@pytest.mark.parametrize("expiry_days,should_redeem,reason", [
    (2.9, True, "expiring-soon"), (3.0, True, "expiring-soon"), (3.1, False, "not-expiring"),
])
def test_expiry_window_boundary(snapshot, settings, soonest, expiry_days, should_redeem, reason):
    settings.set("reset.expiryDays", "3")
    credit = replace(soonest, expires_at=NOW + int(expiry_days * DAY))

    assert_decision(
        decide(replace(snapshot, reset_credits=(credit,)), settings), should_redeem, reason, credit,
    )


@pytest.mark.parametrize("expiry_days", [2, 4])
def test_unknown_policy_loaded_from_disk_never_redeems(
    snapshot, soonest, swap_home, expiry_days,
):
    # Redemption is irreversible. A policy value this build does not understand is
    # not evidence that the user wanted the default rule applied to their credits.
    (swap_home / "settings.json").write_text(
        json.dumps({"reset": {"policy": "future-policy"}}), encoding="utf-8",
    )
    settings = Settings.load()
    assert settings.reset_policy == "expiring"
    assert settings.invalid == {"reset.policy": "future-policy"}
    credit = replace(soonest, expires_at=NOW + expiry_days * DAY)

    assert_decision(
        decide(replace(snapshot, reset_credits=(credit,)), settings, alternatives=False),
        False, "policy-invalid", None,
    )


@pytest.mark.parametrize("outcome", ["reset", "noCredit", "nothingToReset", "alreadyRedeemed"])
@pytest.mark.parametrize("explicit_key", [None, "same-logical-attempt-key"])
@pytest.mark.parametrize("credit_id", [None, SOONEST_ID])
def test_redeem_uses_injected_context_and_forwards_arguments_and_outcome(
    codex_root, outcome, explicit_key, credit_id,
):
    context = MagicMock()
    client = Mock()
    context.__enter__.return_value = client
    context.__exit__.return_value = False
    factory = Mock(return_value=context)

    def consume(idempotency_key, *, credit_id=None):
        context.__enter__.assert_called_once_with()
        context.__exit__.assert_not_called()
        return outcome

    client.consume_reset_credit.side_effect = consume
    kwargs = {"client_factory": factory, "credit_id": credit_id}
    if explicit_key is not None:
        kwargs.update(idempotency_key=explicit_key, timeout=12.5)

    result = resets.redeem(codex_root, **kwargs)

    assert result == outcome
    factory.assert_called_once_with(codex_root, timeout=45.0 if explicit_key is None else 12.5)
    context.__enter__.assert_called_once_with()
    context.__exit__.assert_called_once_with(None, None, None)
    forwarded_key = client.consume_reset_credit.call_args.args[0]
    if explicit_key is None:
        assert isinstance(forwarded_key, str)
        parsed_key = uuid.UUID(forwarded_key)
        assert parsed_key.version == 4
        assert str(parsed_key) == forwarded_key
    else:
        assert forwarded_key == explicit_key
    client.consume_reset_credit.assert_called_once_with(forwarded_key, credit_id=credit_id)


@pytest.mark.parametrize("outcome,expected", [
    ("reset", True), ("noCredit", False), ("nothingToReset", False),
    ("alreadyRedeemed", False), ("unknown", False), ("RESET", False), ("", False),
])
def test_outcome_is_success_only_for_reset(outcome, expected):
    assert resets.outcome_is_success(outcome) is expected


@pytest.mark.parametrize("reason,should_redeem", [
    ("policy-never", False), ("no-credits", False), ("daily-cap", False),
    ("no-usage-data", False), ("usage-too-low", False), ("expiring-soon", True),
    ("not-expiring", False), ("all-accounts-exhausted", True),
    ("alternatives-available", False), ("policy-always", True),
])
def test_describe_is_nonempty_and_never_exposes_credit_id(soonest, reason, should_redeem):
    decision = resets.ResetDecision(should_redeem, soonest, reason)

    description = resets.describe(decision)

    assert isinstance(description, str)
    assert description.strip()
    assert soonest.id not in description
    assert "RateLimitResetCredit_" not in description
