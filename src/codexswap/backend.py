"""Explicit opt-in fallback to undocumented ChatGPT backend endpoints.

These endpoints were reverse-engineered from the Codex VS Code extension, are
unsupported by OpenAI, and may break without notice. They are only reached when
the user explicitly opts in via --backend or probe.allowBackendFallback. Callers
must enforce that choice; the supported default lives in appserver.py.
"""

# The public contract explicitly requires typing.Optional/List/Dict annotations.
# ruff: noqa: UP006, UP007, UP035

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import uuid
from contextlib import suppress
from typing import Any, Dict, List, Optional

from . import __version__, errors
from .models import ResetCredit

BASE_URL = "https://chatgpt.com/backend-api"


def _safe_detail(detail: str, access_token: str) -> str:
    if access_token:
        detail = detail.replace(access_token, "[redacted]")
    detail = re.sub(r"(?i)(\bbearer\s+)[^\s\"'<>]+", r"\1[redacted]", detail)
    detail = re.sub(
        r"(?i)([\"']?(?:access[_-]?token|refresh[_-]?token|id[_-]?token|"
        r"openai_api_key|api[_-]?key|authorization)[\"']?\s*[:=]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)",
        r"\1[redacted]",
        detail,
    )
    return re.sub(
        r"\b(?:eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]*)?"
        r"|sk-[A-Za-z0-9_-]+|rt\.[A-Za-z0-9._-]+)",
        "[redacted]",
        detail,
    )


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
            detail = _safe_detail(exc.read().decode("utf-8", errors="replace"), access_token)[:300]
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
            + _safe_detail(str(exc.reason), access_token)
        ) from None
    except (OSError, ValueError) as exc:
        # JSON/header errors and socket timeouts must not expose response or token data.
        if isinstance(exc, ValueError):
            raise errors.BackendError(
                "Invalid request or JSON response from undocumented Codex backend"
            ) from None
        raise errors.BackendError(
            "Undocumented Codex backend request failed: " + _safe_detail(str(exc), access_token)
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
    if isinstance(result, str):
        return result
    raise errors.BackendError("Unrecognised backend reset-credit outcome")


def read_usage(access_token: str, account_id: str, *, timeout: float = 20.0) -> Dict[str, Any]:
    result = _request("/wham/usage", access_token, account_id, timeout=timeout)
    if not isinstance(result, dict):
        raise errors.BackendError("Unrecognised backend usage response")
    return result
