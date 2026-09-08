"""Tests for the shared dataclasses and app-server payload parsing."""

from __future__ import annotations

import time

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
