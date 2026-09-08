from __future__ import annotations

import sys
import textwrap
import threading
import time

import pytest
from conftest import RATE_LIMITS_RESULT

from codexswap import appserver, errors

NOW = 1_788_912_000.0


def fake_server(tmp_path, **options):
    """An offline peer that records the wire protocol and echoes consume params."""
    script = tmp_path / "fake_appserver.py"
    source = (
        "from __future__ import annotations\n"
        "import json\nimport os\nimport sys\n"
        f"OPTIONS = {options!r}\n"
        f"RATE_LIMITS = {RATE_LIMITS_RESULT!r}\n"
    )
    source += textwrap.dedent("""
        if "stderr" in OPTIONS:
            print(OPTIONS["stderr"], file=sys.stderr, flush=True)
            sys.exit(1)

        requests = []
        last_consume = None
        for line in sys.stdin:
            request = json.loads(line)
            requests.append(request)
            method = request["method"]
            if "id" not in request:
                continue
            if OPTIONS.get("silent") == method:
                continue
            if OPTIONS.get("noise"):
                print("not JSON: startup banner", flush=True)
                print(json.dumps({"method": "account/updated", "params": {}}), flush=True)
                print(json.dumps({"id": request["id"] + 1000, "result": "wrong id"}), flush=True)
            response = {"id": request["id"]}
            if method == "initialize":
                response["result"] = {"serverInfo": {"name": "fake"}}
            elif method == "account/rateLimits/read":
                if OPTIONS.get("rpc_error"):
                    response["error"] = {"code": -32001, "message": "fixture RPC failure"}
                else:
                    response["result"] = RATE_LIMITS
            elif method == "account/rateLimitResetCredit/consume":
                last_consume = request["params"]
                response["result"] = OPTIONS.get("consume_result", {"outcome": "reset"})
            elif method == "test/lastConsume":
                response["result"] = last_consume
            elif method == "test/requests":
                response["result"] = {
                    "requests": requests,
                    "home": os.environ["CODEX_HOME"],
                    "argv": sys.argv[1:],
                }
            else:
                response["error"] = {"code": -32601, "message": "unknown test method"}
            print(json.dumps(response), flush=True)
    """)
    script.write_text(source, encoding="utf-8")
    return [sys.executable, str(script)]


@pytest.mark.parametrize("noise", [False, True], ids=["clean", "unrelated-lines"])
def test_rate_limits_handshake_and_response_framing(tmp_path, codex_root, monkeypatch, noise):
    monkeypatch.setattr(appserver.time, "time", lambda: NOW)
    with appserver.AppServerClient(
        codex_root, timeout=2, codex_bin=fake_server(tmp_path, noise=noise),
    ) as client:
        snapshot = client.read_rate_limits()
        wire = client.request("test/requests")
    assert snapshot.binding_percent == 84.0
    assert snapshot.available_reset_count == 2
    assert snapshot.fetched_at == NOW
    assert snapshot.primary.window_minutes == 10080
    assert {credit.id for credit in snapshot.reset_credits} == {
        "RateLimitResetCredit_soonest", "RateLimitResetCredit_later",
    }
    assert wire["requests"][:3] == [
        {"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "codexswap", "version": "0.1.0"},
        }},
        {"method": "initialized", "params": None},
        {"id": 2, "method": "account/rateLimits/read"},
    ]
    assert wire["home"] == str(codex_root)
    assert wire["argv"] == ["app-server"]


@pytest.mark.parametrize("silent", ["initialize", "account/rateLimits/read"])
def test_unanswered_request_times_out_without_hanging(tmp_path, codex_root, silent):
    client = appserver.AppServerClient(
        codex_root, timeout=1.0, codex_bin=fake_server(tmp_path, silent=silent),
    )
    watchdog_fired = threading.Event()

    def stop_hung_child():
        watchdog_fired.set()
        proc = client._process
        if proc is not None and proc.poll() is None:
            proc.kill()

    # The fake blocks only on stdin, so normal close ends it immediately. This
    # watchdog also turns a broken client timeout into a bounded test failure.
    watchdog = threading.Timer(3.5, stop_hung_child)
    watchdog.daemon = True
    started = time.monotonic()
    watchdog.start()
    try:
        with pytest.raises(errors.AppServerTimeout), client:
            client.read_rate_limits()
    finally:
        watchdog.cancel()
        watchdog.join(timeout=0.2)
        client.__exit__(None, None, None)
    elapsed = time.monotonic() - started
    assert 0.8 <= elapsed < 5
    assert not watchdog_fired.is_set()


@pytest.mark.parametrize("stderr,expected,message", [
    ("unauthorized: fixture credentials rejected", errors.AuthExpired, "authentication"),
    ("fixture storage exploded", errors.AppServerError, "fixture storage exploded"),
])
def test_early_exit_reports_auth_or_server_error(tmp_path, codex_root, stderr, expected, message):
    with pytest.raises(expected, match=message), appserver.AppServerClient(
        codex_root, timeout=2, codex_bin=fake_server(tmp_path, stderr=stderr),
    ):
        pytest.fail("The fake exits before completing initialization")


def test_json_rpc_error_carries_message(tmp_path, codex_root):
    with appserver.AppServerClient(
        codex_root, timeout=2, codex_bin=fake_server(tmp_path, rpc_error=True),
    ) as client, pytest.raises(errors.AppServerError, match="fixture RPC failure"):
        client.read_rate_limits()


@pytest.mark.parametrize("result", [
    {"outcome": "reset"}, {"result": {"outcome": "reset"}}, "reset",
], ids=["outcome-object", "nested-result", "bare-string"])
@pytest.mark.parametrize("credit_id", [None, "RateLimitResetCredit_soonest"])
def test_consume_normalizes_result_and_sends_optional_credit(tmp_path, codex_root, result, credit_id):
    with appserver.AppServerClient(
        codex_root, timeout=2, codex_bin=fake_server(tmp_path, consume_result=result),
    ) as client:
        assert client.consume_reset_credit("fixed-idempotency-key", credit_id=credit_id) == "reset"
        echoed_params = client.request("test/lastConsume")
    expected = {"idempotencyKey": "fixed-idempotency-key"}
    if credit_id is not None:
        expected["creditId"] = credit_id
    assert echoed_params == expected


@pytest.mark.parametrize("result", [None, {}, {"result": {"unexpected": "reset"}}, ["reset"]])
def test_consume_rejects_unrecognized_result(tmp_path, codex_root, result):
    with appserver.AppServerClient(
        codex_root, timeout=2, codex_bin=fake_server(tmp_path, consume_result=result),
    ) as client, pytest.raises(errors.AppServerError, match="[Uu]nrecognised"):
        client.consume_reset_credit("fixed-idempotency-key")


def test_context_exit_reaps_child_and_is_idempotent(tmp_path, codex_root):
    client = appserver.AppServerClient(codex_root, timeout=2, codex_bin=fake_server(tmp_path))
    with client:
        proc = client._process
        assert proc is not None
        assert proc.poll() is None
        client.read_rate_limits()
    assert proc.poll() is not None
    client.__exit__(None, None, None)
    client.__exit__(None, None, None)
    assert proc.poll() is not None


def test_nonexistent_codex_bin_override_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_BIN", str(tmp_path / "does-not-exist.exe"))
    with pytest.raises(errors.CodexBinaryNotFound, match="CODEX_BIN"):
        appserver.find_codex_binary()
