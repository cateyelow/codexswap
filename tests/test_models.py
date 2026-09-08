"""Tests for the shared dataclasses and app-server payload parsing."""

from __future__ import annotations

import time

import pytest
from conftest import RATE_LIMITS_RESULT, make_auth

from codexswap import identity, models

NOW = 1789000000.0


def snapshot() -> models.UsageSnapshot:
    return models.UsageSnapshot.from_api(RATE_LIMITS_RESULT, fetched_at=NOW)


def test_from_api_parses_the_verified_payload():
    snap = snapshot()
    assert snap.plan_type == "pro"
    assert snap.account_id == "acct-0000-1111"
    assert snap.primary is not None
    assert snap.primary.used_percent == 84.0
    assert snap.primary.window_minutes == 10080
    assert snap.primary.resets_at == 1789435573
    assert snap.secondary is None
    assert snap.has_credits is False
    assert snap.credits_balance == "0"


def test_binding_percent_is_the_worst_window():
    snap = snapshot()
    assert snap.binding_percent == 84.0

    both = models.UsageSnapshot.from_api(
        {
            "rateLimits": {
                "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1},
                "secondary": {"usedPercent": 77, "windowDurationMins": 10080, "resetsAt": 2},
                "planType": "plus",
            }
        },
        fetched_at=NOW,
    )
    assert both.binding_percent == 77.0


def test_binding_percent_is_none_without_windows():
    empty = models.UsageSnapshot.from_api({}, fetched_at=NOW)
    assert empty.binding_percent is None
    assert empty.available_reset_count == 0
    assert empty.per_limit == ()


def test_used_percent_coerces_to_float():
    snap = models.UsageSnapshot.from_api(
        {"rateLimits": {"primary": {"usedPercent": 5, "windowDurationMins": 300}}},
        fetched_at=NOW,
    )
    assert isinstance(snap.primary.used_percent, float)


def test_per_limit_entries_are_parsed():
    snap = snapshot()
    by_id = {p.limit_id: p for p in snap.per_limit}
    assert set(by_id) == {"codex", "codex_bengalfox"}
    spark = by_id["codex_bengalfox"]
    assert spark.limit_name == "GPT-5.3-Codex-Spark"
    assert spark.primary.window_minutes == 300
    assert spark.secondary.window_minutes == 10080


def test_reset_credits_and_soonest_expiry():
    snap = snapshot()
    assert snap.available_reset_count == 2
    soonest = snap.soonest_expiring_credit()
    assert soonest is not None
    assert soonest.id == "RateLimitResetCredit_soonest"


def test_credits_without_expiry_sort_last():
    payload = {
        "rateLimitResetCredits": {
            "credits": [
                {"id": "no-expiry", "status": "available", "expiresAt": None},
                {"id": "has-expiry", "status": "available", "expiresAt": 1789950028},
            ]
        }
    }
    snap = models.UsageSnapshot.from_api(payload, fetched_at=NOW)
    assert snap.soonest_expiring_credit().id == "has-expiry"


def test_redeemed_credits_are_not_available():
    payload = {
        "rateLimitResetCredits": {
            "credits": [
                {"id": "spent", "status": "redeemed", "expiresAt": 1},
                {"id": "pending", "status": "redeeming", "expiresAt": 2},
            ]
        }
    }
    snap = models.UsageSnapshot.from_api(payload, fetched_at=NOW)
    assert snap.available_reset_count == 0
    assert snap.soonest_expiring_credit() is None


def test_reset_credit_days_until_expiry():
    credit = models.ResetCredit(
        id="c",
        reset_type="codexRateLimits",
        status="available",
        granted_at=None,
        expires_at=int(NOW + 86400 * 3),
        title="Full reset",
        description=None,
    )
    assert round(credit.days_until_expiry(NOW), 3) == 3.0
    assert credit.is_available is True

    forever = models.ResetCredit(
        id="d",
        reset_type=None,
        status="available",
        granted_at=None,
        expires_at=None,
        title=None,
        description=None,
    )
    assert forever.days_until_expiry(NOW) is None


def test_rate_limit_window_seconds_until_reset():
    window = models.RateLimitWindow(
        used_percent=10.0, window_minutes=300, resets_at=int(NOW + 600)
    )
    assert round(window.seconds_until_reset(NOW)) == 600
    unknown = models.RateLimitWindow(used_percent=10.0, window_minutes=300, resets_at=None)
    assert unknown.seconds_until_reset(NOW) is None


def test_usage_snapshot_roundtrip():
    snap = snapshot()
    restored = models.UsageSnapshot.from_dict(snap.to_dict())
    assert restored.binding_percent == snap.binding_percent
    assert restored.available_reset_count == snap.available_reset_count
    assert restored.plan_type == snap.plan_type
    assert restored.primary.resets_at == snap.primary.resets_at
    assert len(restored.per_limit) == len(snap.per_limit)


def test_from_dict_tolerates_missing_fields():
    assert models.UsageSnapshot.from_dict({}) is not None
    assert models.AccountIdentity.from_dict({}) is not None
    assert models.Account.from_dict({"slot": 1}) .slot == 1


def test_account_matches_slot_alias_and_email():
    ident = identity.identity_from_auth(make_auth(email="Person@Example.com"))
    account = models.Account(slot=2, identity=ident, alias="Main", added_at="")
    assert account.matches("2")
    assert account.matches("main")
    assert account.matches("MAIN")
    assert account.matches("person@example.com")
    assert not account.matches("3")
    assert not account.matches("other@example.com")


def test_account_roundtrip_preserves_usage():
    ident = identity.identity_from_auth(make_auth())
    account = models.Account(
        slot=1,
        identity=ident,
        alias="a",
        disabled=True,
        added_at="2026-09-09T03:00:00Z",
        last_seen_at=NOW,
        last_seen_usage=snapshot(),
    )
    restored = models.Account.from_dict(account.to_dict())
    assert restored.slot == 1
    assert restored.alias == "a"
    assert restored.disabled is True
    assert restored.identity.email == "a@example.com"
    assert restored.last_seen_usage.binding_percent == 84.0


def test_account_display_includes_alias_when_present():
    ident = identity.identity_from_auth(make_auth(email="x@y.z"))
    with_alias = models.Account(slot=1, identity=ident, alias="work", added_at="")
    without = models.Account(slot=1, identity=ident, added_at="")
    assert "work" in with_alias.display()
    assert "x@y.z" in with_alias.display()
    assert without.display() == "x@y.z"


def test_identity_label_falls_back_to_account_id():
    ident = models.AccountIdentity(
        email=None,
        name=None,
        account_id="abcdef123456",
        plan_type=None,
        auth_mode="chatgpt",
        subscription_active_until=None,
        access_token_exp=None,
        id_token_exp=None,
    )
    assert "abcdef" in ident.label()
    blank = models.AccountIdentity(
        email=None,
        name=None,
        account_id=None,
        plan_type=None,
        auth_mode="unknown",
        subscription_active_until=None,
        access_token_exp=None,
        id_token_exp=None,
    )
    assert blank.label() == "unknown"


def test_fetched_at_is_preserved():
    snap = models.UsageSnapshot.from_api(RATE_LIMITS_RESULT, fetched_at=123.5)
    assert snap.fetched_at == 123.5
    assert time.time() > 0  # sanity, keeps the import meaningful


def _with_limits(*limits) -> models.UsageSnapshot:
    """A snapshot whose aggregate is 10% and whose per-model entries are given."""
    return models.UsageSnapshot(
        fetched_at=NOW, account_id="acct", plan_type="pro",
        primary=models.RateLimitWindow(10.0, 10080, None), secondary=None,
        has_credits=False, credits_balance=None, reset_credits=(),
        per_limit=tuple(
            models.PerLimitUsage(
                limit_id=limit_id, limit_name=limit_name,
                primary=models.RateLimitWindow(primary, 300, None) if primary is not None else None,
                secondary=(models.RateLimitWindow(secondary, 10080, None)
                           if secondary is not None else None),
                plan_type="pro",
            )
            for limit_id, limit_name, primary, secondary in limits
        ),
    )


def test_percent_for_without_models_is_the_aggregate():
    snap = snapshot()

    assert snap.percent_for() == snap.binding_percent == 84.0
    assert snap.percent_for(()) == 84.0


def test_percent_for_takes_the_worse_of_aggregate_and_selected_model():
    snap = _with_limits(("codex_bengalfox", "GPT-5.3-Codex-Spark", 97.0, 40.0))

    assert snap.percent_for() == 10.0
    assert snap.percent_for(["codex_bengalfox"]) == 97.0


def test_percent_for_keeps_the_aggregate_when_the_model_is_idle():
    snap = _with_limits(("codex_bengalfox", "GPT-5.3-Codex-Spark", 0.0, 0.0))

    assert snap.percent_for(["codex_bengalfox"]) == 10.0


def test_percent_for_matches_limit_name_case_insensitively():
    snap = _with_limits(("codex_bengalfox", "GPT-5.3-Codex-Spark", 91.0, None))

    assert snap.percent_for(["gpt-5.3-codex-spark"]) == 91.0
    assert snap.percent_for(["CODEX_BENGALFOX"]) == 91.0


def test_percent_for_ignores_models_the_account_does_not_report():
    snap = _with_limits(("codex_bengalfox", "GPT-5.3-Codex-Spark", 97.0, None))

    assert snap.percent_for(["codex_nonesuch"]) == 10.0


def test_percent_for_all_selects_every_reported_model():
    snap = _with_limits(
        ("codex", None, 5.0, None),
        ("codex_bengalfox", "GPT-5.3-Codex-Spark", 93.0, None),
    )

    assert snap.percent_for(["all"]) == 93.0


def test_percent_for_takes_the_worst_of_several_selected_models():
    snap = _with_limits(
        ("codex_a", None, 30.0, None),
        ("codex_b", None, 88.0, None),
        ("codex_c", None, 99.0, None),
    )

    assert snap.percent_for(["codex_a", "codex_b"]) == 88.0


def test_percent_for_considers_both_windows_of_a_selected_model():
    snap = _with_limits(("codex_bengalfox", None, 20.0, 95.0))

    assert snap.percent_for(["codex_bengalfox"]) == 95.0


def test_percent_for_reports_unknown_when_nothing_has_a_percent():
    snap = models.UsageSnapshot(
        fetched_at=NOW, account_id=None, plan_type=None, primary=None, secondary=None,
        has_credits=False, credits_balance=None, reset_credits=(),
        per_limit=(models.PerLimitUsage("codex_bengalfox", None, None, None, None),),
    )

    assert snap.percent_for(["codex_bengalfox"]) is None


def test_percent_for_ignores_blank_names():
    snap = _with_limits(("codex_bengalfox", "GPT-5.3-Codex-Spark", 97.0, None))

    assert snap.percent_for(["", "   "]) == 10.0


def _identity(account_id):
    return models.AccountIdentity(
        email="a@example.com", name="A", account_id=account_id, plan_type="pro",
        auth_mode="chatgpt", subscription_active_until=None,
        access_token_exp=None, id_token_exp=None,
    )


def _snapshot_for(account_id):
    return models.UsageSnapshot(
        fetched_at=NOW, account_id=account_id, plan_type="pro", primary=None, secondary=None,
        has_credits=False, credits_balance=None, reset_credits=(), per_limit=(),
    )


@pytest.mark.parametrize("probed,registered,expected", [
    ("acct-1", "acct-1", True),
    ("acct-1", "acct-2", False),
    ("acct-1", None, True),
    (None, "acct-1", True),
    (None, None, True),
    ("acct-1", "", True),
    ("", "acct-1", True),
    ("  acct-1  ", "acct-1", True),
    ("acct-1", "  acct-1  ", True),
    ("ACCT-1", "acct-1", False),
])
def test_describes_only_rejects_two_known_and_different_ids(probed, registered, expected):
    assert _snapshot_for(probed).describes(_identity(registered)) is expected


def test_the_verified_payload_describes_the_account_it_came_from():
    assert snapshot().describes(_identity("acct-0000-1111")) is True
    assert snapshot().describes(_identity("acct-somebody-else")) is False
