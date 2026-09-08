"""Shared account, usage, and reset-credit values with JSON conversion helpers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

HEALTH_OK = "ok"
HEALTH_EXPIRED = "expired"
HEALTH_UNKNOWN = "unknown"


def _mapping(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional_str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def _optional_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _optional_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _boolean(value: Any) -> bool:
    if isinstance(value, str):
        return value.casefold() in ("true", "1", "yes", "on")
    return isinstance(value, (bool, int, float)) and bool(value)


def _objects(value: Any) -> Tuple[Dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


@dataclass(frozen=True)
class AccountIdentity:
    email: Optional[str]
    name: Optional[str]
    account_id: Optional[str]
    plan_type: Optional[str]
    auth_mode: str
    subscription_active_until: Optional[str]
    access_token_exp: Optional[int]
    id_token_exp: Optional[int]

    def label(self) -> str:
        return self.email or (self.account_id[:8] if self.account_id else "unknown")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "email": self.email,
            "name": self.name,
            "accountId": self.account_id,
            "planType": self.plan_type,
            "authMode": self.auth_mode,
            "subscriptionActiveUntil": self.subscription_active_until,
            "accessTokenExp": self.access_token_exp,
            "idTokenExp": self.id_token_exp,
        }

    @classmethod
    def from_dict(cls, d: Any) -> AccountIdentity:
        d = _mapping(d)
        return cls(
            email=_optional_str(d.get("email")),
            name=_optional_str(d.get("name")),
            account_id=_optional_str(d.get("accountId")),
            plan_type=_optional_str(d.get("planType")),
            auth_mode=_optional_str(d.get("authMode")) or "unknown",
            subscription_active_until=_optional_str(d.get("subscriptionActiveUntil")),
            access_token_exp=_optional_int(d.get("accessTokenExp")),
            id_token_exp=_optional_int(d.get("idTokenExp")),
        )


@dataclass(frozen=True)
class RateLimitWindow:
    used_percent: float
    window_minutes: int
    resets_at: Optional[int]

    def seconds_until_reset(self, now: float) -> Optional[float]:
        if self.resets_at is None:
            return None
        return max(0.0, self.resets_at - now)

    def to_dict(self) -> Dict[str, Any]:
        # CONTRACT: local JSON uses windowMinutes (section 9); the API differs.
        return {
            "usedPercent": self.used_percent,
            "windowMinutes": self.window_minutes,
            "resetsAt": self.resets_at,
        }

    @classmethod
    def from_dict(cls, d: Any) -> RateLimitWindow:
        d = _mapping(d)
        return cls(
            used_percent=_optional_float(d.get("usedPercent")) or 0.0,
            window_minutes=_optional_int(
                d.get("windowMinutes", d.get("windowDurationMins"))
            ) or 0,
            resets_at=_optional_int(d.get("resetsAt")),
        )

    @classmethod
    def from_api(cls, d: Any) -> RateLimitWindow:
        d = _mapping(d)
        return cls(
            used_percent=_optional_float(d.get("usedPercent")) or 0.0,
            window_minutes=_optional_int(d.get("windowDurationMins")) or 0,
            resets_at=_optional_int(d.get("resetsAt")),
        )


@dataclass(frozen=True)
class ResetCredit:
    id: str
    reset_type: Optional[str]
    status: str
    granted_at: Optional[int]
    expires_at: Optional[int]
    title: Optional[str]
    description: Optional[str]

    @property
    def is_available(self) -> bool:
        return self.status == "available"

    def days_until_expiry(self, now: float) -> Optional[float]:
        if self.expires_at is None:
            return None
        return (self.expires_at - now) / 86400.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "resetType": self.reset_type,
            "status": self.status,
            "grantedAt": self.granted_at,
            "expiresAt": self.expires_at,
            "title": self.title,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, d: Any) -> ResetCredit:
        d = _mapping(d)
        return cls(
            id=_optional_str(d.get("id")) or "",
            reset_type=_optional_str(d.get("resetType")),
            # CONTRACT: a missing status must never imply an available credit.
            status=_optional_str(d.get("status")) or "unknown",
            granted_at=_optional_int(d.get("grantedAt")),
            expires_at=_optional_int(d.get("expiresAt")),
            title=_optional_str(d.get("title")),
            description=_optional_str(d.get("description")),
        )

    @classmethod
    def from_api(cls, d: Any) -> ResetCredit:
        return cls.from_dict(d)


@dataclass(frozen=True)
class PerLimitUsage:
    limit_id: str
    limit_name: Optional[str]
    primary: Optional[RateLimitWindow]
    secondary: Optional[RateLimitWindow]
    plan_type: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "limitId": self.limit_id,
            "limitName": self.limit_name,
            "primary": self.primary.to_dict() if self.primary is not None else None,
            "secondary": self.secondary.to_dict() if self.secondary is not None else None,
            "planType": self.plan_type,
        }

    @classmethod
    def from_dict(cls, d: Any) -> PerLimitUsage:
        d = _mapping(d)
        return cls(
            limit_id=_optional_str(d.get("limitId")) or "",
            limit_name=_optional_str(d.get("limitName")),
            primary=(RateLimitWindow.from_dict(d["primary"])
                     if isinstance(d.get("primary"), dict) else None),
            secondary=(RateLimitWindow.from_dict(d["secondary"])
                       if isinstance(d.get("secondary"), dict) else None),
            plan_type=_optional_str(d.get("planType")),
        )

    @classmethod
    def from_api(cls, limit_id: str, d: Any) -> PerLimitUsage:
        d = _mapping(d)
        return cls(
            limit_id=_optional_str(d.get("limitId")) or _optional_str(limit_id) or "",
            limit_name=_optional_str(d.get("limitName")),
            primary=(RateLimitWindow.from_api(d["primary"])
                     if isinstance(d.get("primary"), dict) else None),
            secondary=(RateLimitWindow.from_api(d["secondary"])
                       if isinstance(d.get("secondary"), dict) else None),
            plan_type=_optional_str(d.get("planType")),
        )


@dataclass(frozen=True)
class UsageSnapshot:
    fetched_at: float
    account_id: Optional[str]
    plan_type: Optional[str]
    primary: Optional[RateLimitWindow]
    secondary: Optional[RateLimitWindow]
    has_credits: bool
    credits_balance: Optional[str]
    reset_credits: Tuple[ResetCredit, ...]
    per_limit: Tuple[PerLimitUsage, ...]

    @property
    def binding_percent(self) -> Optional[float]:
        percentages = tuple(
            window.used_percent for window in (self.primary, self.secondary)
            if window is not None and window.used_percent is not None
        )
        return max(percentages) if percentages else None

    @property
    def available_reset_credits(self) -> Tuple[ResetCredit, ...]:
        return tuple(credit for credit in self.reset_credits if credit.is_available)

    @property
    def available_reset_count(self) -> int:
        return len(self.available_reset_credits)

    def soonest_expiring_credit(self) -> Optional[ResetCredit]:
        return min(
            self.available_reset_credits,
            key=lambda credit: (credit.expires_at is None, credit.expires_at or 0),
            default=None,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fetchedAt": self.fetched_at,
            "accountId": self.account_id,
            "planType": self.plan_type,
            "primary": self.primary.to_dict() if self.primary is not None else None,
            "secondary": self.secondary.to_dict() if self.secondary is not None else None,
            "hasCredits": self.has_credits,
            "creditsBalance": self.credits_balance,
            "resetCredits": [credit.to_dict() for credit in self.reset_credits],
            "perLimit": [limit.to_dict() for limit in self.per_limit],
        }

    @classmethod
    def from_dict(cls, d: Any) -> UsageSnapshot:
        d = _mapping(d)
        return cls(
            fetched_at=_optional_float(d.get("fetchedAt")) or 0.0,
            account_id=_optional_str(d.get("accountId")),
            plan_type=_optional_str(d.get("planType")),
            primary=(RateLimitWindow.from_dict(d["primary"])
                     if isinstance(d.get("primary"), dict) else None),
            secondary=(RateLimitWindow.from_dict(d["secondary"])
                       if isinstance(d.get("secondary"), dict) else None),
            has_credits=_boolean(d.get("hasCredits")),
            credits_balance=_optional_str(d.get("creditsBalance")),
            reset_credits=tuple(ResetCredit.from_dict(item)
                                for item in _objects(d.get("resetCredits"))),
            per_limit=tuple(PerLimitUsage.from_dict(item)
                            for item in _objects(d.get("perLimit"))),
        )

    @classmethod
    def from_api(cls, result: dict, *, fetched_at: float) -> UsageSnapshot:
        result = _mapping(result)
        limits = _mapping(result.get("rateLimits"))
        credits = _mapping(limits.get("credits"))
        resets = _mapping(result.get("rateLimitResetCredits"))
        per_limit = _mapping(result.get("rateLimitsByLimitId"))
        return cls(
            fetched_at=_optional_float(fetched_at) or 0.0,
            account_id=_optional_str(result.get("accountId")),
            plan_type=_optional_str(limits.get("planType")),
            primary=(RateLimitWindow.from_api(limits["primary"])
                     if isinstance(limits.get("primary"), dict) else None),
            secondary=(RateLimitWindow.from_api(limits["secondary"])
                       if isinstance(limits.get("secondary"), dict) else None),
            has_credits=_boolean(credits.get("hasCredits")),
            credits_balance=_optional_str(credits.get("balance")),
            reset_credits=tuple(ResetCredit.from_api(item)
                                for item in _objects(resets.get("credits"))),
            per_limit=tuple(PerLimitUsage.from_api(limit_id, item)
                            for limit_id, item in per_limit.items()),
        )


@dataclass
class Account:
    slot: int
    identity: AccountIdentity
    alias: Optional[str] = None
    disabled: bool = False
    added_at: str = ""
    last_switched_at: Optional[str] = None
    last_seen_at: Optional[float] = None
    last_seen_usage: Optional[UsageSnapshot] = None

    def display(self) -> str:
        label = self.identity.label()
        return f"{self.alias} ({label})" if self.alias else label

    def matches(self, ref: str) -> bool:
        if not isinstance(ref, str):
            return False
        folded = ref.casefold()
        return (
            ref == str(self.slot)
            or (self.alias is not None and folded == self.alias.casefold())
            or (self.identity.email is not None and folded == self.identity.email.casefold())
        )

    def to_dict(self) -> Dict[str, Any]:
        # CONTRACT: identity fields are flattened into the account (section 2.1).
        result = {"slot": self.slot, **self.identity.to_dict()}
        result.update({
            "alias": self.alias,
            "disabled": self.disabled,
            "addedAt": self.added_at,
            "lastSwitchedAt": self.last_switched_at,
            "lastSeenAt": self.last_seen_at,
            "lastSeenUsage": (self.last_seen_usage.to_dict()
                              if self.last_seen_usage is not None else None),
        })
        return result

    @classmethod
    def from_dict(cls, d: Any) -> Account:
        d = _mapping(d)
        return cls(
            slot=_optional_int(d.get("slot")) or 0,
            identity=AccountIdentity.from_dict(d),
            alias=_optional_str(d.get("alias")),
            disabled=_boolean(d.get("disabled")),
            added_at=_optional_str(d.get("addedAt")) or "",
            last_switched_at=_optional_str(d.get("lastSwitchedAt")),
            last_seen_at=_optional_float(d.get("lastSeenAt")),
            last_seen_usage=(UsageSnapshot.from_dict(d["lastSeenUsage"])
                             if isinstance(d.get("lastSeenUsage"), dict) else None),
        )
