"""Policy and execution for banked rate-limit reset credits."""

from __future__ import annotations

# CONTRACT: Optional annotations must remain compatible with Python 3.9.
# ruff: noqa: UP007
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .models import ResetCredit, UsageSnapshot
from .settings import Settings

OUTCOME_RESET = "reset"
OUTCOME_NOTHING = "nothingToReset"
OUTCOME_NO_CREDIT = "noCredit"
OUTCOME_ALREADY = "alreadyRedeemed"


@dataclass(frozen=True)
class ResetDecision:
    should_redeem: bool
    credit: Optional[ResetCredit]
    reason: str


def decide(
    snapshot: UsageSnapshot,
    settings: Settings,
    *,
    now: float,
    redeemed_last_24h: int,
    alternatives_available: bool,
) -> ResetDecision:
    policy = settings.reset_policy
    if policy not in ("never", "expiring", "exhausted", "always"):
        policy = "expiring"
    if policy == "never":
        return ResetDecision(False, None, "policy-never")
    if snapshot.available_reset_count == 0:
        return ResetDecision(False, None, "no-credits")
    cap = settings.reset_max_per_day
    if cap > 0 and redeemed_last_24h >= cap:
        return ResetDecision(False, None, "daily-cap")
    percent = snapshot.binding_percent
    if percent is None:
        return ResetDecision(False, None, "no-usage-data")
    credit = snapshot.soonest_expiring_credit()
    if percent < settings.reset_min_usage_percent:
        return ResetDecision(False, credit, "usage-too-low")
    if policy == "expiring":
        days_until_expiry = credit.days_until_expiry(now) if credit is not None else None
        if days_until_expiry is not None and days_until_expiry <= settings.reset_expiry_days:
            return ResetDecision(True, credit, "expiring-soon")
        return ResetDecision(False, credit, "not-expiring")
    if policy == "exhausted":
        if not alternatives_available:
            return ResetDecision(True, credit, "all-accounts-exhausted")
        return ResetDecision(False, credit, "alternatives-available")
    return ResetDecision(True, credit, "policy-always")


def redeem(
    codex_home: Path, *, credit_id: Optional[str] = None,
    idempotency_key: Optional[str] = None, timeout: float = 45.0,
    client_factory=None,
) -> str:
    if client_factory is None:
        from .appserver import AppServerClient

        client_factory = AppServerClient
    if idempotency_key is None:
        idempotency_key = str(uuid.uuid4())
    with client_factory(codex_home, timeout=timeout) as client:
        return client.consume_reset_credit(idempotency_key, credit_id=credit_id)


def outcome_is_success(outcome: str) -> bool:
    return outcome == OUTCOME_RESET


def describe(decision: ResetDecision) -> str:
    # CONTRACT: Decisions carry no usage percent or evaluation time, so descriptions use reasons.
    descriptions = {
        "policy-never": "skipped: automatic credit redemption is disabled",
        "no-credits": "skipped: no reset credits are available",
        "daily-cap": "skipped: the redemption limit for the last 24 hours has been reached",
        "no-usage-data": "skipped: no usage data is available",
        "usage-too-low": "skipped: usage is below the configured minimum",
        "expiring-soon": "redeeming: credit expires soon",
        "not-expiring": "skipped: credit does not expire within the configured period",
        "all-accounts-exhausted": "redeeming: no alternative account has enough headroom",
        "alternatives-available": "skipped: another account has enough headroom",
        "policy-always": "redeeming: usage meets the minimum and the policy is always",
    }
    action = "redeeming" if decision.should_redeem else "skipped"
    return descriptions.get(decision.reason, f"{action}: {decision.reason}")
