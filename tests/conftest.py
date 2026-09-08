"""Shared pytest fixtures and builders for the codexswap test suite.

Every test runs against temporary CODEXSWAP_HOME and CODEX_HOME directories.
Nothing here may read or write the developer's real ``~/.codex``.
"""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:  # pragma: no cover - import side effect
    sys.path.insert(0, str(_SRC))

from codexswap import paths  # noqa: E402

AUTH_CLAIM = "https://api.openai.com/auth"
PROFILE_CLAIM = "https://api.openai.com/profile"


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_jwt(payload: dict) -> str:
    """Build an unsigned JWT-shaped token. Only the payload segment matters."""
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode("utf-8"))
    body = b64url(json.dumps(payload).encode("utf-8"))
    return f"{header}.{body}.{b64url(b'not-a-real-signature')}"


def make_auth(
    *,
    email: str = "a@example.com",
    name: str = "A Person",
    account_id: str = "acct-0000-1111",
    plan_type: str = "pro",
    id_exp: int | None = None,
    access_exp: int | None = None,
    subscription_until: str = "2027-05-31T02:50:31+00:00",
    auth_mode: str = "chatgpt",
) -> dict:
    """Build an auth.json payload shaped exactly like the real Codex one."""
    now = int(time.time())
    id_token = make_jwt(
        {
            "email": email,
            "email_verified": True,
            "name": name,
            "iat": now,
            "exp": id_exp if id_exp is not None else now + 3600,
            AUTH_CLAIM: {
                "chatgpt_account_id": account_id,
                "chatgpt_plan_type": plan_type,
                "chatgpt_subscription_active_start": "2026-05-31T02:50:31+00:00",
                "chatgpt_subscription_active_until": subscription_until,
            },
        }
    )
    access_token = make_jwt(
        {
            "iat": now,
            "exp": access_exp if access_exp is not None else now + 86400,
            PROFILE_CLAIM: {"email": email, "email_verified": True, "name": name},
            AUTH_CLAIM: {
                "chatgpt_account_id": account_id,
                "chatgpt_plan_type": plan_type,
            },
        }
    )
    return {
        "auth_mode": auth_mode,
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": id_token,
            "access_token": access_token,
            "refresh_token": "rt.1.TESTONLY",
            "account_id": account_id,
        },
        "last_refresh": "2026-09-08T10:57:36.966926600Z",
    }


def make_apikey_auth(key: str = "sk-test-0000") -> dict:
    return {"auth_mode": "apikey", "OPENAI_API_KEY": key, "tokens": None}


# The exact payload verified against codex-cli 0.153.4 (CONTRACT section 1.4),
# with a second reset credit added so ordering logic is exercised.
RATE_LIMITS_RESULT = {
    "rateLimits": {
        "limitId": "codex",
        "limitName": None,
        "primary": {"usedPercent": 84, "windowDurationMins": 10080, "resetsAt": 1789435573},
        "secondary": None,
        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
        "individualLimit": None,
        "spendControlReached": False,
        "planType": "pro",
        "rateLimitReachedType": None,
    },
    "rateLimitsByLimitId": {
        "codex": {
            "limitId": "codex",
            "limitName": None,
            "primary": {"usedPercent": 84, "windowDurationMins": 10080, "resetsAt": 1789435573},
            "secondary": None,
            "credits": None,
            "planType": "pro",
        },
        "codex_bengalfox": {
            "limitId": "codex_bengalfox",
            "limitName": "GPT-5.3-Codex-Spark",
            "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": 1788912388},
            "secondary": {"usedPercent": 0, "windowDurationMins": 10080, "resetsAt": 1789499188},
            "credits": None,
            "planType": "pro",
        },
    },
    "rateLimitResetCredits": {
        "availableCount": 2,
        "credits": [
            {
                "id": "RateLimitResetCredit_later",
                "resetType": "codexRateLimits",
                "status": "available",
                "grantedAt": 1788500128,
                "expiresAt": 1791092128,
                "title": "Full reset",
                "description": "Thanks for using Codex!",
            },
            {
                "id": "RateLimitResetCredit_soonest",
                "resetType": "codexRateLimits",
                "status": "available",
                "grantedAt": 1787358028,
                "expiresAt": 1789950028,
                "title": "Full reset",
                "description": "Thanks for using Codex!",
            },
        ],
    },
    "accountId": "acct-0000-1111",
    "rateLimitUpsell": None,
}


@pytest.fixture(autouse=True)
def isolated_homes(tmp_path, monkeypatch):
    """Point every home at tmp_path so the real Codex install is never touched."""
    swap_root = tmp_path / "codexswap-home"
    codex_root = tmp_path / "codex-home"
    swap_root.mkdir(parents=True, exist_ok=True)
    codex_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CODEXSWAP_HOME", str(swap_root))
    monkeypatch.setenv("CODEX_HOME", str(codex_root))
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CODEX_BIN", raising=False)
    paths.clear_home_override()
    yield {"swap": swap_root, "codex": codex_root}
    paths.clear_home_override()


@pytest.fixture
def swap_home(isolated_homes):
    return isolated_homes["swap"]


@pytest.fixture
def codex_root(isolated_homes):
    return isolated_homes["codex"]


@pytest.fixture
def live_auth(codex_root):
    """Write a live auth.json and return (path, auth_dict)."""
    auth = make_auth()
    path = codex_root / "auth.json"
    path.write_text(json.dumps(auth), encoding="utf-8")
    return path, auth
