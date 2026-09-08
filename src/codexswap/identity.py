"""Read authentication files and derive identity without exposing token material."""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import errors, models
from .models import AccountIdentity


def decode_jwt_payload(token: str) -> dict:
    try:
        segment = token.split(".")[1]
        if re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", segment) is None:
            raise ValueError("Invalid base64url characters")
        segment += "=" * (-len(segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(segment))
    except (AttributeError, IndexError, TypeError, ValueError, RecursionError):
        raise errors.AuthFileInvalid("Cannot decode JWT payload: invalid encoding or JSON") from None
    if not isinstance(payload, dict):
        raise errors.AuthFileInvalid("Invalid JWT payload: expected a JSON object")
    return payload


def load_auth(path: Path) -> dict:
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as handle:
            auth = json.load(handle)
    except FileNotFoundError:
        raise errors.AuthFileMissing(f"Authentication file not found: {path}") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise errors.AuthFileInvalid(f"Invalid JSON in authentication file: {path}") from None
    if not isinstance(auth, dict):
        raise errors.AuthFileInvalid(f"Authentication file must contain a JSON object: {path}")
    return auth


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def validate_auth(auth: Any, *, source: str = "authentication data") -> dict:
    """Reject JSON that parses but could never authenticate Codex.

    `load_auth` only proves the file holds an object. Writing an object with no
    credential in it into the live home logs the user out, so every path that
    copies auth into a slot or into `CODEX_HOME` validates first. Only the fields
    Codex itself needs are required: a refreshable OAuth token pair, or an API key.
    """
    if not isinstance(auth, dict):
        raise errors.AuthFileInvalid(f"{source} must contain a JSON object")
    tokens = auth.get("tokens")
    if isinstance(tokens, dict) and (
        _nonempty(tokens.get("refresh_token")) or _nonempty(tokens.get("access_token"))
    ):
        return auth
    if _nonempty(auth.get("OPENAI_API_KEY")):
        return auth
    raise errors.AuthFileInvalid(
        f"{source} holds no usable credential: expected tokens.refresh_token "
        "or OPENAI_API_KEY"
    )


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def _expiry(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def identity_from_auth(auth: dict) -> AccountIdentity:
    if not isinstance(auth, dict):
        raise errors.AuthFileInvalid("Authentication data must be a JSON object")
    raw_tokens = auth.get("tokens")
    auth_mode = _text(auth.get("auth_mode")) or (
        "apikey" if auth.get("OPENAI_API_KEY") and not raw_tokens else "unknown"
    )
    if auth_mode != "chatgpt":
        return AccountIdentity(None, None, None, None, auth_mode, None, None, None)

    tokens = raw_tokens if isinstance(raw_tokens, dict) else {}
    id_payload = decode_jwt_payload(tokens["id_token"]) if tokens.get("id_token") else {}
    claims = id_payload.get("https://api.openai.com/auth")
    claims = claims if isinstance(claims, dict) else {}
    email = _text(id_payload.get("email"))
    access_token_exp = None
    if tokens.get("access_token"):
        try:
            access_payload = decode_jwt_payload(tokens["access_token"])
        except errors.AuthFileInvalid:
            pass
        else:
            access_token_exp = _expiry(access_payload.get("exp"))
            if not email:
                email = _text(access_payload.get("https://api.openai.com/profile.email"))
                # The namespaced profile claim can also contain an email field.
                profile = access_payload.get("https://api.openai.com/profile")
                if not email and isinstance(profile, dict):
                    email = _text(profile.get("email"))

    return AccountIdentity(
        email=email,
        name=_text(id_payload.get("name")),
        account_id=_text(claims.get("chatgpt_account_id")) or _text(tokens.get("account_id")),
        plan_type=_text(claims.get("chatgpt_plan_type")),
        auth_mode=auth_mode,
        subscription_active_until=_text(claims.get("chatgpt_subscription_active_until")),
        access_token_exp=access_token_exp,
        id_token_exp=_expiry(id_payload.get("exp")),
    )


def access_token_expired(
    identity: AccountIdentity, *, now: float, skew: float = 60.0,
) -> bool:
    return identity.access_token_exp is not None and identity.access_token_exp - skew <= now


def subscription_lapsed(identity: AccountIdentity, *, now: float) -> bool:
    """Whether the billing period stamped into the token has already ended.

    This is informational only. The claim records the billing period that was
    current when the token was issued, and it is not reissued every period, so an
    active subscriber routinely carries a timestamp in the past. It must never be
    treated as evidence that the account stopped working.
    """
    if not identity.subscription_active_until:
        return False
    try:
        until = identity.subscription_active_until
        if until.endswith("Z"):
            until = until[:-1] + "+00:00"
        end = datetime.fromisoformat(until)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return end.timestamp() < now
    except (ValueError, OverflowError, OSError):
        return False


def health_of(identity: AccountIdentity, *, now: float) -> str:
    """Health that can be judged from the stored file alone.

    Only two states are knowable offline: the credential parses (ok) or it does
    not (unknown). HEALTH_EXPIRED means the refresh token was actually rejected,
    which only a live call can establish, so it is never returned from here.
    """
    if identity.auth_mode == "apikey":
        return models.HEALTH_OK
    if identity.auth_mode != "chatgpt":
        return models.HEALTH_UNKNOWN
    # An elapsed access-token expiry stays healthy: Codex refreshes it on demand.
    if identity.access_token_exp is None:
        return models.HEALTH_UNKNOWN
    return models.HEALTH_OK
