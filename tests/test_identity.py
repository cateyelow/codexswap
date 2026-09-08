"""Tests for JWT decoding and identity derivation."""

from __future__ import annotations

import json
import time

import pytest
from conftest import AUTH_CLAIM, b64url, make_apikey_auth, make_auth, make_jwt

from codexswap import errors, identity, models


def test_decode_jwt_payload_handles_missing_padding():
    # A payload whose base64 length is not a multiple of 4 must still decode.
    payload = {"a": "x" * 5}
    token = make_jwt(payload)
    assert identity.decode_jwt_payload(token) == payload


def test_decode_jwt_payload_rejects_garbage():
    with pytest.raises(errors.AuthFileInvalid):
        identity.decode_jwt_payload("not-a-jwt")


def test_decode_jwt_payload_rejects_non_json_body():
    token = ".".join(["aGVhZGVy", b64url(b"this is not json"), "c2ln"])
    with pytest.raises(errors.AuthFileInvalid):
        identity.decode_jwt_payload(token)


def test_decode_jwt_payload_error_never_leaks_the_token():
    secret = b64url(json.dumps({"sub": "SUPERSECRETVALUE"}).encode())
    token = "h." + secret + "x.s"
    try:
        identity.decode_jwt_payload(token)
    except errors.AuthFileInvalid as exc:
        assert "SUPERSECRET" not in str(exc)
        assert secret not in str(exc)


def test_identity_from_chatgpt_auth():
    auth = make_auth(email="me@example.com", account_id="acct-42", plan_type="plus")
    ident = identity.identity_from_auth(auth)
    assert ident.email == "me@example.com"
    assert ident.name == "A Person"
    assert ident.account_id == "acct-42"
    assert ident.plan_type == "plus"
    assert ident.auth_mode == "chatgpt"
    assert ident.subscription_active_until == "2027-05-31T02:50:31+00:00"
    assert isinstance(ident.access_token_exp, int)
    assert isinstance(ident.id_token_exp, int)


def test_identity_falls_back_to_tokens_account_id():
    auth = make_auth(account_id="acct-claim")
    payload = identity.decode_jwt_payload(auth["tokens"]["id_token"])
    payload.pop(AUTH_CLAIM)
    auth["tokens"]["id_token"] = make_jwt(payload)
    auth["tokens"]["account_id"] = "acct-fallback"
    ident = identity.identity_from_auth(auth)
    assert ident.account_id == "acct-fallback"


def test_identity_falls_back_to_access_token_email():
    auth = make_auth(email="profile@example.com")
    payload = identity.decode_jwt_payload(auth["tokens"]["id_token"])
    payload.pop("email")
    auth["tokens"]["id_token"] = make_jwt(payload)
    ident = identity.identity_from_auth(auth)
    assert ident.email == "profile@example.com"


def test_malformed_access_token_does_not_break_identity():
    auth = make_auth()
    auth["tokens"]["access_token"] = "totally-broken"
    ident = identity.identity_from_auth(auth)
    assert ident.email == "a@example.com"
    assert ident.access_token_exp is None


def test_apikey_identity():
    ident = identity.identity_from_auth(make_apikey_auth())
    assert ident.auth_mode == "apikey"
    assert ident.email is None


def test_load_auth_missing_file(tmp_path):
    with pytest.raises(errors.AuthFileMissing):
        identity.load_auth(tmp_path / "auth.json")


def test_load_auth_invalid_json(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text("{oops", encoding="utf-8")
    with pytest.raises(errors.AuthFileInvalid):
        identity.load_auth(path)


def test_load_auth_rejects_non_object(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(errors.AuthFileInvalid):
        identity.load_auth(path)


def test_load_auth_roundtrip(tmp_path):
    auth = make_auth()
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(auth), encoding="utf-8")
    assert identity.load_auth(path)["tokens"]["account_id"] == auth["tokens"]["account_id"]


def test_access_token_expired_respects_skew():
    now = time.time()
    fresh = identity.identity_from_auth(make_auth(access_exp=int(now) + 3600))
    stale = identity.identity_from_auth(make_auth(access_exp=int(now) - 10))
    assert identity.access_token_expired(fresh, now=now) is False
    assert identity.access_token_expired(stale, now=now) is True


def test_access_token_expired_unknown_expiry_is_false():
    auth = make_auth()
    auth["tokens"]["access_token"] = "broken"
    ident = identity.identity_from_auth(auth)
    assert identity.access_token_expired(ident, now=time.time()) is False


def test_health_of_expired_access_token_is_still_ok():
    now = time.time()
    ident = identity.identity_from_auth(make_auth(access_exp=int(now) - 10))
    # An expired access token is normal; Codex refreshes it from the refresh token.
    assert identity.health_of(ident, now=now) == models.HEALTH_OK


def test_health_of_missing_tokens_is_unknown():
    ident = identity.identity_from_auth({"auth_mode": "chatgpt", "tokens": None})
    assert identity.health_of(ident, now=time.time()) == models.HEALTH_UNKNOWN


def test_lapsed_billing_period_does_not_make_the_account_unhealthy():
    # Regression: the subscription claim records the billing period current when the
    # token was issued. An active Pro subscriber routinely carries a past timestamp,
    # so treating it as expiry produced a false "re-login needed" on a working account.
    now = time.time()
    ident = identity.identity_from_auth(make_auth(subscription_until="2020-01-01T00:00:00+00:00"))
    assert identity.subscription_lapsed(ident, now=now) is True
    assert identity.health_of(ident, now=now) == models.HEALTH_OK


def test_subscription_lapsed_handles_future_and_unparseable_values():
    now = time.time()
    future = identity.identity_from_auth(make_auth(subscription_until="2099-01-01T00:00:00+00:00"))
    assert identity.subscription_lapsed(future, now=now) is False

    auth = make_auth()
    payload = identity.decode_jwt_payload(auth["tokens"]["id_token"])
    payload[AUTH_CLAIM]["chatgpt_subscription_active_until"] = "not-a-date"
    auth["tokens"]["id_token"] = make_jwt(payload)
    broken = identity.identity_from_auth(auth)
    assert identity.subscription_lapsed(broken, now=now) is False


def test_health_never_reports_expired_from_a_stored_file():
    # HEALTH_EXPIRED means a live call rejected the credential, which cannot be
    # judged offline; health_of must therefore only ever return ok or unknown.
    now = time.time()
    variants = [
        make_auth(),
        make_auth(access_exp=int(now) - 10),
        make_auth(subscription_until="2020-01-01T00:00:00+00:00"),
        make_apikey_auth(),
    ]
    for auth in variants:
        ident = identity.identity_from_auth(auth)
        assert identity.health_of(ident, now=now) in {models.HEALTH_OK, models.HEALTH_UNKNOWN}
