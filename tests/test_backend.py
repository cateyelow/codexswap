from __future__ import annotations

import ast
import errno
import json
import os
import socket
import sys
import sysconfig
import threading
import time
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Queue
from socketserver import TCPServer
from uuid import UUID

import pytest

from codexswap import __version__, backend, errors
from codexswap.models import ResetCredit

TOKEN = "test-token-not-real-0123456789abcdef"
ACCOUNT_ID = "test-account-not-real-9876543210"
CREDIT_ID = "RateLimitResetCredit_TEST_ONLY"
OPERATIONS = ("list_reset_credits", "consume_reset_credit", "read_usage")
HTTP_ERRORS = (400, 401, 403, 404, 429, 500, 503)
BODY_MARKER = "fixture backend failure"
SNAKE_CREDIT = {
    "id": CREDIT_ID,
    "reset_type": "codexRateLimits",
    "status": "available",
    "granted_at": 1787358028,
    "expires_at": 1789950028,
    "title": "Full reset",
    "description": "Thanks for using Codex!",
}


class _LocalServer(ThreadingHTTPServer):
    # server_close joins every request thread. Each handler has bounded socket
    # reads, and teardown releases the deliberately silent handler first.
    daemon_threads = False
    block_on_close = True

    def server_bind(self):
        # HTTPServer normally performs a reverse DNS lookup for its display
        # name. Numeric loopback needs none, and must never query external DNS.
        TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]

    def __init__(self):
        self.requests = Queue()
        self.status = 200
        self.body = b"{}"
        self.silent = False
        self.release = threading.Event()
        super().__init__(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(
            target=self.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
        )
        self.thread.start()
        self.stopped = False

    def respond(self, payload, *, status=200):
        self.status = status
        self.body = json.dumps(payload).encode("utf-8")

    def stop(self):
        if self.stopped:
            return
        self.release.set()
        try:
            self.shutdown()
        finally:
            self.server_close()
            self.thread.join(timeout=1)
            self.stopped = True
        assert not self.thread.is_alive(), "Local HTTP server thread leaked"

    def received(self):
        request = self.requests.get(timeout=1)
        assert self.requests.empty(), "Unexpected extra request (possibly a redemption retry)"
        return request


class _Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(1)
        super().setup()

    def log_message(self, format, *args):
        # Request headers contain fake credentials; no logging is needed.
        pass

    def do_GET(self):
        self._respond()

    def do_POST(self):
        self._respond()

    def _respond(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.requests.put((self.command, self.path, self.headers, body))
        if self.server.silent:
            self.server.release.wait()
            return
        self.send_response(self.server.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.server.body)))
        self.end_headers()
        self.wfile.write(self.server.body)


@pytest.fixture(autouse=True)
def local_backend(monkeypatch):
    """Every test gets a real ephemeral loopback server, never the real backend."""
    server = _LocalServer()
    address = server.server_address
    blocked = []
    original_getaddrinfo = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def check_address(candidate):
        if candidate != address:
            blocked.append(candidate)
            raise AssertionError("Network access outside this test's loopback server is forbidden")

    def local_getaddrinfo(host, port, *args, **kwargs):
        check_address((host, port))
        return original_getaddrinfo(host, port, *args, **kwargs)

    def local_connect(sock, destination):
        check_address(destination)
        return original_connect(sock, destination)

    def local_connect_ex(sock, destination):
        check_address(destination)
        return original_connect_ex(sock, destination)

    try:
        # Bypass environment and Windows proxy discovery without replacing any
        # urllib code. DNS and socket guards also reject external redirects.
        monkeypatch.setenv("NO_PROXY", "*")
        monkeypatch.setenv("no_proxy", "*")
        monkeypatch.setattr(socket, "getaddrinfo", local_getaddrinfo)
        monkeypatch.setattr(socket.socket, "connect", local_connect)
        monkeypatch.setattr(socket.socket, "connect_ex", local_connect_ex)
        monkeypatch.setattr(backend, "BASE_URL", f"http://{address[0]}:{address[1]}/backend-api")
        yield server
    finally:
        server.stop()
    assert not blocked, "A backend call attempted to leave the local test server"


def _interpreter_dirs():
    """Directories the running interpreter loads its own code from."""
    candidates = {sys.prefix, sys.base_prefix, os.path.dirname(os.__file__)}
    for name in ("stdlib", "platstdlib", "purelib", "platlib"):
        with suppress(KeyError):
            candidates.add(sysconfig.get_paths()[name])
    return tuple(sorted(
        os.path.normcase(os.path.abspath(path)) + os.sep for path in candidates if path
    ))


@pytest.fixture(scope="module")
def filesystem_guard():
    """Reject file access during backend calls, including pathlib and os APIs."""
    active = False
    attempts = []
    file_events = {
        "open", "os.listdir", "os.scandir", "os.mkdir", "os.remove", "os.rmdir",
        "os.rename", "os.link", "os.symlink", "os.truncate", "os.chmod",
        "os.chown", "os.utime", "os.setxattr", "os.removexattr",
    }
    # CPython lazily imports codec modules from inside socket.getaddrinfo, so a read
    # under the interpreter's own directories is an import, not the backend touching
    # user data. Anything else, the Codex home above all, still fails this test.
    # Do not build a traceback here: linecache would open the source and re-enter.
    ignored = _interpreter_dirs()

    def audit(event, args):
        if not active or event not in file_events or not args:
            return
        if not isinstance(args[0], (str, bytes, os.PathLike)):
            return
        target = os.path.normcase(os.path.abspath(os.fsdecode(args[0])))
        if target.startswith(ignored):
            return
        attempts.append(f"{event}({target})")
        raise AssertionError("Backend attempted filesystem access: " + attempts[-1])

    # Python audit hooks cannot be removed; this hook is inert outside the
    # tightly scoped call below, including all subsequent test modules.
    sys.addaudithook(audit)

    @contextmanager
    def guard():
        nonlocal active
        attempts.clear()
        active = True
        try:
            yield
        finally:
            active = False
            assert not attempts, "Backend accessed files: " + ", ".join(attempts)

    return guard


@pytest.fixture
def call_backend(local_backend, filesystem_guard):
    def call(operation, *, timeout=1.0, **kwargs):
        if operation == "consume_reset_credit":
            kwargs.setdefault("credit_id", CREDIT_ID)
        with filesystem_guard():
            return getattr(backend, operation)(TOKEN, ACCOUNT_ID, timeout=timeout, **kwargs)

    return call


def assert_no_credentials(exc):
    message = str(exc)
    assert TOKEN not in message, "Exception leaked the bearer token"
    assert ACCOUNT_ID not in message, "Exception leaked the account id"
    # Every substring longer than eight characters contains a nine-character
    # substring. Checking these windows covers prefixes, middles, and suffixes.
    assert not any(TOKEN[i:i + 9] in message for i in range(len(TOKEN) - 8)), (
        "Exception leaked more than eight consecutive token characters"
    )


@contextmanager
def backend_error(expected=errors.BackendError):
    with pytest.raises(Exception) as caught:
        yield caught
    # Inspect even unexpected exceptions for credentials, then require the
    # exact public error type (never URLError, JSONDecodeError, or KeyError).
    assert_no_credentials(caught.value)
    assert type(caught.value) is expected


def assert_common_headers(headers):
    assert headers["Authorization"] == "Bearer " + TOKEN
    assert headers["ChatGPT-Account-Id"] == ACCOUNT_ID
    assert headers["Accept"] == "application/json"
    assert "codexswap" in headers["User-Agent"]
    assert __version__ in headers["User-Agent"]


def test_list_reset_credits_get_headers(local_backend, call_backend):
    local_backend.respond({"credits": []})
    assert call_backend("list_reset_credits") == []
    method, path, headers, body = local_backend.received()
    assert (method, path) == ("GET", "/backend-api/wham/rate-limit-reset-credits")
    assert_common_headers(headers)
    assert body == b""


@pytest.mark.parametrize("redeem_request_id", [None, "test-explicit-idempotency-key-\u00e9"])
def test_consume_reset_credit_post_json(local_backend, call_backend, redeem_request_id):
    local_backend.respond({"outcome": "reset"})
    assert call_backend(
        "consume_reset_credit", redeem_request_id=redeem_request_id,
        credit_id=CREDIT_ID + "-\u00e9",
    ) == "reset"
    method, path, headers, body = local_backend.received()
    assert (method, path) == ("POST", "/backend-api/wham/rate-limit-reset-credits/consume")
    assert_common_headers(headers)
    assert headers["Content-Type"] == "application/json"
    assert int(headers["Content-Length"]) == len(body)
    payload = json.loads(body.decode("utf-8"))
    assert set(payload) == {"credit_id", "redeem_request_id"}
    assert payload["credit_id"] == CREDIT_ID + "-\u00e9"
    if redeem_request_id is None:
        assert isinstance(payload["redeem_request_id"], str)
        assert UUID(payload["redeem_request_id"]).version == 4
    else:
        assert payload["redeem_request_id"] == redeem_request_id


def test_read_usage_get_headers_and_payload(local_backend, call_backend):
    payload = {
        "plan_type": "pro",
        "rate_limit": {
            "allowed": True,
            "primary_window": {"used_percent": 84, "reset_at": 1789435573},
            "secondary_window": None,
        },
        "credits": {"balance": "0", "unlimited": False},
    }
    local_backend.respond(payload)
    assert call_backend("read_usage") == payload
    method, path, headers, body = local_backend.received()
    assert (method, path) == ("GET", "/backend-api/wham/usage")
    assert_common_headers(headers)
    assert body == b""


@pytest.mark.parametrize("wrapped", [True, False], ids=["credits-object", "bare-list"])
def test_list_normalizes_snake_case_credits(local_backend, call_backend, wrapped):
    credits = [
        {**SNAKE_CREDIT, "unknown_future_field": {"ignored": True}},
        {**SNAKE_CREDIT, "id": CREDIT_ID + "_no_expiry", "expires_at": None},
    ]
    local_backend.respond({"credits": credits} if wrapped else credits)
    assert call_backend("list_reset_credits") == [
        ResetCredit(
            id=CREDIT_ID, reset_type="codexRateLimits", status="available",
            granted_at=1787358028, expires_at=1789950028,
            title="Full reset", description="Thanks for using Codex!",
        ),
        ResetCredit(
            id=CREDIT_ID + "_no_expiry", reset_type="codexRateLimits", status="available",
            granted_at=1787358028, expires_at=None,
            title="Full reset", description="Thanks for using Codex!",
        ),
    ]


@pytest.mark.parametrize("payload", [
    {"credits": []}, [], {}, None, {"credits": None},
    {"rate_limit_reset_credits": [SNAKE_CREDIT]},
    {"data": {"credits": [SNAKE_CREDIT]}},
], ids=["empty-credits", "empty-list", "missing", "null", "null-credits",
        "alternative-key", "data-wrapper"])
def test_list_empty_or_unrecognized_wrapper_is_safe(local_backend, call_backend, payload):
    local_backend.respond(payload)
    # The undocumented alternative keys are not supported; returning no credits
    # is a conservative fallback and must not invent redeemable credits.
    assert call_backend("list_reset_credits") == []


@pytest.mark.parametrize("payload", [
    TOKEN + " " + ACCOUNT_ID, 42, True,
    {"credits": {"id": CREDIT_ID}}, {"credits": "not-a-list"},
    {"credits": [None]}, {"credits": [SNAKE_CREDIT, TOKEN, ACCOUNT_ID]},
], ids=["string", "number", "boolean", "object-credits", "string-credits",
        "null-item", "mixed-items"])
def test_list_rejects_malformed_payload(local_backend, call_backend, payload):
    local_backend.respond(payload)
    with backend_error():
        call_backend("list_reset_credits")


@pytest.mark.parametrize("outcome", ["reset", "nothingToReset", "noCredit", "alreadyRedeemed"])
@pytest.mark.parametrize("shape", ["flat", "nested", "result-string", "bare-string"])
def test_consume_documented_outcomes(local_backend, call_backend, outcome, shape):
    payloads = {
        "flat": {"outcome": outcome},
        "nested": {"result": {"outcome": outcome}},
        "result-string": {"result": outcome},
        "bare-string": outcome,
    }
    local_backend.respond(payloads[shape])
    assert call_backend("consume_reset_credit") == outcome
    local_backend.received()  # Each logical redemption sends exactly one POST.


@pytest.mark.parametrize("payload", [
    None, {}, {"outcome": None}, {"outcome": 12},
    {"result": {"unexpected": TOKEN + " " + ACCOUNT_ID}}, ["reset"],
], ids=["null", "missing", "null-outcome", "number-outcome", "bad-nested", "list"])
def test_consume_rejects_malformed_outcome(local_backend, call_backend, payload):
    local_backend.respond(payload)
    with backend_error():
        call_backend("consume_reset_credit")


@pytest.mark.parametrize("payload", [None, [], TOKEN + " " + ACCOUNT_ID, 42])
def test_usage_rejects_non_object_payload(local_backend, call_backend, payload):
    local_backend.respond(payload)
    with backend_error():
        call_backend("read_usage")


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("status", HTTP_ERRORS)
def test_http_error_mapping_and_full_token_redaction(local_backend, call_backend, operation, status):
    local_backend.respond({"error": BODY_MARKER + ": Bearer " + TOKEN}, status=status)
    expected = errors.AuthExpired if status in (401, 403) else errors.BackendError
    with backend_error(expected) as caught:
        call_backend(operation)
    assert str(status) in str(caught.value)
    assert BODY_MARKER in str(caught.value)
    local_backend.received()


@pytest.mark.parametrize("status", HTTP_ERRORS)
@pytest.mark.parametrize("echo", [
    "full-token", "account-id", "token-prefix", "token-middle", "token-suffix",
])
def test_http_errors_redact_account_and_token_fragments(local_backend, call_backend, status, echo):
    echoes = {
        "full-token": TOKEN,
        "account-id": ACCOUNT_ID,
        "token-prefix": TOKEN[:9],
        "token-middle": TOKEN[9:18],
        "token-suffix": TOKEN[-9:],
    }
    local_backend.respond({"error": BODY_MARKER + ": " + echoes[echo]}, status=status)
    expected = errors.AuthExpired if status in (401, 403) else errors.BackendError
    with backend_error(expected):
        call_backend("list_reset_credits")


@pytest.mark.parametrize("operation", OPERATIONS)
def test_connection_refused_is_backend_error(local_backend, call_backend, operation):
    # Keep the same guarded loopback address, but close its listener first.
    local_backend.stop()
    with backend_error() as caught:
        call_backend(operation, timeout=3)
    # Windows can take about one second to report WSAECONNREFUSED. A shorter
    # timeout would accidentally exercise the timeout branch instead of URLError.
    assert any(code in str(caught.value) for code in (str(errno.ECONNREFUSED), "10061"))
    assert local_backend.requests.empty()


@pytest.mark.parametrize("operation", OPERATIONS)
def test_silent_server_obeys_timeout(local_backend, call_backend, operation):
    local_backend.silent = True
    timeout = 0.15
    watchdog_fired = threading.Event()

    def release_hung_request():
        watchdog_fired.set()
        local_backend.release.set()

    # Even a regression that ignores timeout cannot leave this test hanging.
    watchdog = threading.Timer(1.5, release_hung_request)
    watchdog.daemon = True
    watchdog.start()
    started = time.monotonic()
    try:
        with backend_error():
            call_backend(operation, timeout=timeout)
        elapsed = time.monotonic() - started
    finally:
        watchdog.cancel()
        watchdog.join(timeout=1)
        local_backend.release.set()
    assert not watchdog.is_alive()
    assert not watchdog_fired.is_set(), "Backend ignored the supplied timeout"
    # Allow scheduling overhead on Windows while distinguishing the supplied
    # timeout from the 1.5s watchdog and the module's 20s default.
    assert timeout * 0.8 <= elapsed < timeout + 0.5
    local_backend.received()  # Proves the connection was accepted before timeout.


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("body", [
    ("<html>failure " + TOKEN + " " + ACCOUNT_ID + "</html>").encode("utf-8"),
    b"", b"\xff\xfe invalid UTF-8",
], ids=["html-with-credentials", "empty", "invalid-utf8"])
def test_invalid_json_is_backend_error(local_backend, call_backend, operation, body):
    local_backend.body = body
    with backend_error():
        call_backend(operation)


def test_backend_never_accesses_disk_or_codex_home(local_backend, call_backend, monkeypatch, tmp_path):
    # The audit guard surrounds every backend invocation in this file, on both
    # success and failure. Rejecting all file opens also protects the real
    # ~/.codex even if a regression ignores these isolated environment values.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    for operation, payload in zip(OPERATIONS, ({"credits": []}, {"outcome": "reset"}, {})):
        local_backend.respond(payload)
        call_backend(operation)
        local_backend.received()
    assert not (tmp_path / ".codex").exists()


def test_shipped_base_url_matches_contract():
    # Read source independently: the autouse fixture has already patched the
    # runtime attribute, so asserting that attribute would hide a bad default.
    source = Path(backend.__file__).read_text(encoding="utf-8")
    assignments = [
        node for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "BASE_URL" for target in node.targets)
    ]
    assert len(assignments) == 1
    assert ast.literal_eval(assignments[0].value) == "https://chatgpt.com/backend-api"


def test_module_docstring_explains_unsupported_opt_in_endpoints():
    doc = (backend.__doc__ or "").lower()
    assert "undocumented" in doc
    assert "unsupported" in doc
    assert "--backend" in doc
    assert "probe.allowbackendfallback" in doc
