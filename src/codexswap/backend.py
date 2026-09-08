"""Explicit opt-in fallback to undocumented ChatGPT backend endpoints.

These endpoints were reverse-engineered from the Codex VS Code extension, are
unsupported by OpenAI, and may break without notice. They are only reached when
the user explicitly opts in via --backend or probe.allowBackendFallback. Callers
must enforce that choice; the supported default lives in appserver.py.
"""

# The public contract explicitly requires typing.Optional/List/Dict annotations.
# ruff: noqa: UP006, UP007, UP035

from __future__ import annotations

import dataclasses
import json
import re
import time
import urllib.error
import urllib.request
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__, errors, identity
from .models import ResetCredit, UsageSnapshot

BASE_URL = "https://chatgpt.com/backend-api"
# Mirrors resets.OUTCOMES, which cannot be imported here: resets imports this.
OUTCOMES = ("reset", "nothingToReset", "noCredit", "alreadyRedeemed")


# Bounded so that redacting a hostile multi-megabyte body cannot stall the caller;
# only the first 300 characters ever reach the message anyway.
_MAX_DETAIL = 4096
# A server-controlled body can echo back part of what we sent, so redacting the whole
# credential is not enough: a fragment of it is still a credential leak. Nine is the
# smallest window that leaves ordinary eight-character English words alone.
_SECRET_WINDOW = 9


def _redact_fragments(detail: str, secret: str) -> str:
    """Blank every span of `detail` that repeats a window of `secret`."""
    if len(secret) < _SECRET_WINDOW or len(detail) < _SECRET_WINDOW:
        return detail
    windows = {secret[i:i + _SECRET_WINDOW] for i in range(len(secret) - _SECRET_WINDOW + 1)}
    hidden = bytearray(len(detail))
    for i in range(len(detail) - _SECRET_WINDOW + 1):
        if detail[i:i + _SECRET_WINDOW] in windows:
            hidden[i:i + _SECRET_WINDOW] = b"\x01" * _SECRET_WINDOW
    if not any(hidden):
        return detail
    out: List[str] = []
    index = 0
    while index < len(detail):
        if hidden[index]:
            out.append("[redacted]")
            while index < len(detail) and hidden[index]:
                index += 1
        else:
            out.append(detail[index])
            index += 1
    return "".join(out)


def _safe_detail(detail: str, access_token: str, account_id: str = "") -> str:
    for secret in (access_token, account_id):
        if secret:
            detail = detail.replace(secret, "[redacted]")
    detail = re.sub(r"(?i)(\bbearer\s+)[^\s\"'<>]+", r"\1[redacted]", detail)
    detail = re.sub(
        r"(?i)([\"']?(?:access[_-]?token|refresh[_-]?token|id[_-]?token|"
        r"openai_api_key|api[_-]?key|authorization)[\"']?\s*[:=]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)",
        r"\1[redacted]",
        detail,
    )
    detail = re.sub(
        r"\b(?:eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]*)?"
        r"|sk-[A-Za-z0-9_-]+|rt\.[A-Za-z0-9._-]+)",
        "[redacted]",
        detail,
    )
    # Last, because every substitution above can leave a partial credential behind.
    for secret in (access_token, account_id):
        detail = _redact_fragments(detail, secret)
    return detail


def _request(
    path: str,
    access_token: str,
    account_id: str,
    *,
    timeout: float,
    payload: Optional[Dict[str, Any]] = None,
) -> Any:
    headers = {
        "Authorization": "Bearer " + access_token,
        "ChatGPT-Account-Id": account_id,
        "Accept": "application/json",
        "User-Agent": "codexswap/" + __version__,
    }
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    try:
        request = urllib.request.Request(
            BASE_URL + path,
            data=data,
            headers=headers,
            method="POST" if payload is not None else "GET",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:_MAX_DETAIL]
            detail = _safe_detail(body, access_token, account_id)[:300]
        except (OSError, ValueError):
            pass
        finally:
            with suppress(Exception):
                exc.close()
        message = f"Undocumented Codex backend HTTP {exc.code}"
        if detail:
            message += ": " + detail
        if exc.code in (401, 403):
            raise errors.AuthExpired(message + "; re-login for this account") from None
        raise errors.BackendError(message) from None
    except urllib.error.URLError as exc:
        raise errors.BackendError(
            "Undocumented Codex backend request failed: "
            + _safe_detail(str(exc.reason), access_token, account_id)
        ) from None
    except (OSError, ValueError) as exc:
        # JSON/header errors and socket timeouts must not expose response or token data.
        if isinstance(exc, ValueError):
            raise errors.BackendError(
                "Invalid request or JSON response from undocumented Codex backend"
            ) from None
        raise errors.BackendError(
            "Undocumented Codex backend request failed: "
            + _safe_detail(str(exc), access_token, account_id)
        ) from None


def _normalise_credit(d: Dict[str, Any]) -> Dict[str, Any]:
    normalised = dict(d)
    for snake, camel in (
        ("reset_type", "resetType"),
        ("granted_at", "grantedAt"),
        ("expires_at", "expiresAt"),
    ):
        if snake in normalised:
            normalised.setdefault(camel, normalised.pop(snake))
    return normalised


def list_reset_credits(
    access_token: str, account_id: str, *, timeout: float = 20.0
) -> List[ResetCredit]:
    result = _request("/wham/rate-limit-reset-credits", access_token, account_id, timeout=timeout)
    # CONTRACT: the undocumented list can be bare or wrapped in a credits object.
    credits = result.get("credits", []) if isinstance(result, dict) else result
    if credits is None:
        return []
    if not isinstance(credits, list) or any(not isinstance(item, dict) for item in credits):
        raise errors.BackendError("Unrecognised backend reset-credit list")
    return [ResetCredit.from_api(_normalise_credit(item)) for item in credits]


def consume_reset_credit(
    access_token: str,
    account_id: str,
    *,
    credit_id: str,
    redeem_request_id: Optional[str] = None,
    timeout: float = 20.0,
) -> str:
    if redeem_request_id is None:
        redeem_request_id = str(uuid.uuid4())
    result = _request(
        "/wham/rate-limit-reset-credits/consume",
        access_token,
        account_id,
        timeout=timeout,
        payload={"credit_id": credit_id, "redeem_request_id": redeem_request_id},
    )
    # CONTRACT: accept the same outcome wrappers as the app-server response.
    if isinstance(result, dict):
        result = result.get("outcome", result.get("result"))
        if isinstance(result, dict):
            result = result.get("outcome")
    # Only the four documented outcomes may leave this call; an arbitrary string
    # would be printed and logged verbatim, straight past the redactor.
    if isinstance(result, str) and result in OUTCOMES:
        return result
    raise errors.BackendError("Unrecognised backend reset-credit outcome")


def read_usage(access_token: str, account_id: str, *, timeout: float = 20.0) -> Dict[str, Any]:
    result = _request("/wham/usage", access_token, account_id, timeout=timeout)
    if not isinstance(result, dict):
        raise errors.BackendError("Unrecognised backend usage response")
    return result


def probe_usage(codex_home: Path, *, timeout: float = 20.0) -> UsageSnapshot:
    """The `--backend` fallback for `appserver.probe_usage`.

    Callers must gate this behind the explicit opt-in; nothing here decides policy.
    `/wham/usage` is undocumented and its shape is unverified, so an unrecognised
    payload yields unknown usage rather than invented numbers, and reset credits come
    from the endpoint whose shape section 1.5 does record.
    """
    auth = identity.load_auth(Path(codex_home) / "auth.json")
    tokens = auth.get("tokens")
    tokens = tokens if isinstance(tokens, dict) else {}
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise errors.BackendError(
            "The undocumented backend needs an OAuth access token; this account has none"
        )
    account_id = identity.identity_from_auth(auth).account_id or ""
    fetched_at = time.time()
    snapshot = UsageSnapshot.from_api(
        read_usage(access_token, account_id, timeout=timeout), fetched_at=fetched_at
    )
    if snapshot.reset_credits:
        return snapshot
    credits = list_reset_credits(access_token, account_id, timeout=timeout)
    return dataclasses.replace(snapshot, reset_credits=tuple(credits))
