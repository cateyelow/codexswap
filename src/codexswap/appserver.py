"""Client for Codex's SUPPORTED app-server protocol over newline JSON on stdio.

Unlike backend.py, this module uses the installed CLI's supported interface and
does not call undocumented HTTP endpoints or fall back to them automatically.
"""

# The public contract explicitly requires typing.Optional/List/Dict annotations.
# ruff: noqa: UP006, UP007, UP035

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, TextIO, Union

from . import __version__, paths, redaction
from .errors import AppServerError, AppServerTimeout, AuthExpired, CodexBinaryNotFound
from .models import UsageSnapshot

_NOT_FOUND = "codex CLI not found on PATH; install it or set CODEX_BIN"
_AUTH_MARKERS = ("unauthorized", "401", "login", "refresh token", "not logged in")
_EOF = object()
# Codex forwards the upstream HTTP status as the JSON-RPC code for auth failures.
# The generic JSON-RPC range (-32000 and below) is deliberately excluded: those are
# ordinary server faults, and treating one as expired would mislabel a healthy account.
_AUTH_CODES = (401, 403)
# Kept here rather than imported from resets.py, which imports this module lazily.
_RESET_OUTCOMES = ("reset", "nothingToReset", "noCredit", "alreadyRedeemed")


def _is_auth_error(error: Dict[str, Any]) -> bool:
    code = error.get("code")
    if isinstance(code, int) and not isinstance(code, bool) and code in _AUTH_CODES:
        return True
    text = " ".join(
        str(part) for part in (error.get("message"), error.get("data")) if part is not None
    ).lower()
    return any(marker in text for marker in _AUTH_MARKERS)


_CREDENTIAL = re.compile(
    r"(?i)(\bbearer\s+)[^\s\"'<>]+"
    r"|((?:[\"']?(?:access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"openai_api_key|api[_-]?key|authorization)[\"']?\s*[:=]\s*))"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
    r"|\b(?:eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]*)?"
    r"|sk-[A-Za-z0-9_-]+|rt\.[A-Za-z0-9._-]+)"
)


def _native_windows_binary(shim: Path) -> Optional[str]:
    pattern = "node_modules/@openai/codex/node_modules/@openai/codex-*/vendor/*/bin/codex.exe"
    for binary in sorted(shim.parent.glob(pattern)):
        if binary.is_file():
            return str(binary.resolve())
    return None


def find_codex_binary() -> str:
    """Find Codex, preferring a native executable over Windows npm shims."""
    override = os.environ.get("CODEX_BIN")
    if override is not None:
        binary = Path(override).expanduser()
        if not override or not binary.is_file():
            raise CodexBinaryNotFound(
                "CODEX_BIN does not name an existing file; set it to the Codex executable"
            )
        if os.name == "nt" and binary.suffix.lower() != ".exe":
            native = _native_windows_binary(binary)
            if native is None:
                raise CodexBinaryNotFound(
                    "CODEX_BIN points to a shim; set it to the native codex.exe executable"
                )
            return native
        return str(binary.resolve())

    candidates = [shutil.which("codex"), shutil.which("codex.exe")]
    for candidate in candidates:
        if not candidate:
            continue
        binary = Path(candidate)
        if not binary.is_file():
            continue
        if os.name != "nt" or binary.suffix.lower() == ".exe":
            return str(binary.resolve())
        native = _native_windows_binary(binary)
        if native is not None:
            return native
    raise CodexBinaryNotFound(_NOT_FOUND)


class AppServerClient:
    """A context-managed, synchronous app-server connection.

    ``codex_bin`` accepts an executable string or a list of command arguments,
    such as ``[sys.executable, fake_server_script]``. ``app-server`` is appended
    in either case. Requests on a client are serialized; use separate clients
    to probe multiple accounts concurrently.
    """

    def __init__(
        self,
        codex_home: Path,
        *,
        timeout: float = 45.0,
        codex_bin: Optional[Union[str, List[str]]] = None,
        client_name: str = "codexswap",
        client_version: str = __version__,
    ) -> None:
        self.codex_home = Path(codex_home)
        self.timeout = timeout
        self.codex_bin = codex_bin
        self.client_name = client_name
        self.client_version = client_version
        self._process: Optional[subprocess.Popen] = None
        self._messages: queue.Queue = queue.Queue()
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._stderr_tail = ""
        self._auth_failure = False
        self._secrets: Set[str] = set()
        self._next_id = 1
        self._request_lock = threading.Lock()

    def __repr__(self) -> str:
        return f"AppServerClient(timeout={self.timeout!r})"

    def _remember_credentials(self) -> None:
        # Also refresh this set before formatting errors: Codex can rotate tokens.
        with suppress(OSError, ValueError):
            with (self.codex_home / "auth.json").open(encoding="utf-8") as stream:
                auth = json.load(stream)
            if isinstance(auth, dict):
                values = [auth.get("OPENAI_API_KEY")]
                tokens = auth.get("tokens")
                if isinstance(tokens, dict):
                    values.extend(
                        tokens.get(key) for key in ("access_token", "refresh_token", "id_token")
                    )
                self._secrets.update(value for value in values if isinstance(value, str) and value)

    def _redact(self, message: str) -> str:
        """Remove every credential this client already knows about.

        Separate from `_safe_message` because the stderr reader runs this on each
        chunk, and re-reading auth.json once per kilobyte of output would be absurd.
        A token that rotates mid-run stays raw in the tail until the final
        `_safe_message` pass, which refreshes the set and redacts the whole thing.
        """
        message = redaction.redact_known(message, self._secrets)
        message = _CREDENTIAL.sub(
            lambda match: (match.group(1) or match.group(2) or "") + "[redacted]",
            message,
        )
        # Last: a token echoed back truncated, split, or percent-encoded survives
        # every substitution above, and a fragment of a credential is still one.
        for secret in self._secrets:
            message = redaction.redact_fragments(message, secret)
        return message

    def _safe_message(self, message: str) -> str:
        self._remember_credentials()
        return self._redact(message)

    def __enter__(self) -> AppServerClient:
        if self._process is not None:
            raise AppServerError("App-server client is already open")
        paths.ensure_dir(self.codex_home)
        binary = self.codex_bin if self.codex_bin is not None else find_codex_binary()
        command = [binary] if isinstance(binary, str) else list(binary)
        if not command:
            raise CodexBinaryNotFound(_NOT_FOUND)
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.codex_home)
        self._secrets = {
            value
            for key, value in env.items()
            if value and any(part in key.upper() for part in ("TOKEN", "API_KEY", "PASSWORD"))
        }
        self._remember_credentials()
        self._messages = queue.Queue()
        self._stderr_tail = ""
        self._auth_failure = False
        self._next_id = 1
        options: Dict[str, Any] = {}
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            try:
                self._process = subprocess.Popen(
                    command + ["app-server"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=env,
                    **options,
                )
            except OSError as exc:
                raise CodexBinaryNotFound(
                    "Could not start Codex; install it or set CODEX_BIN to a native executable "
                    f"(OS error {exc.errno})"
                ) from None
            self._stdout_thread = threading.Thread(
                target=self._read_stdout,
                args=(self._process.stdout,),
                name="codexswap-appserver-stdout",
                daemon=True,
            )
            self._stderr_thread = threading.Thread(
                target=self._read_stderr,
                args=(self._process.stderr,),
                name="codexswap-appserver-stderr",
                daemon=True,
            )
            self._stdout_thread.start()
            self._stderr_thread.start()
            self.request(
                "initialize",
                {"clientInfo": {"name": self.client_name, "version": self.client_version}},
            )
            self._write({"method": "initialized", "params": None})
            return self
        except BaseException:
            self.__exit__()
            raise

    def _read_stdout(self, stream: TextIO) -> None:
        try:
            for line in stream:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    self._messages.put(message)
        except (OSError, ValueError):
            pass
        finally:
            self._messages.put(_EOF)

    def _read_stderr(self, stream: TextIO) -> None:
        try:
            while True:
                chunk = stream.read(1024)
                if not chunk:
                    break
                combined = self._stderr_tail + chunk
                if any(marker in combined.lower() for marker in _AUTH_MARKERS):
                    self._auth_failure = True
                # Redact before dropping the head. Cutting first leaves the tail end of
                # a token whose other half is gone, which nothing can then recognise.
                self._stderr_tail = self._redact(combined)[-16384:]
        except (OSError, ValueError):
            pass

    def _connection_error(self, method: str, deadline: Optional[float] = None) -> AppServerError:
        if self._stderr_thread is not None:
            remaining = 1.0 if deadline is None else max(0.0, deadline - time.monotonic())
            self._stderr_thread.join(timeout=min(1.0, remaining))
        if self._auth_failure:
            raise AuthExpired("Codex authentication failed; re-login for this account")
        detail = self._safe_message(self._stderr_tail)[-500:].strip()
        message = f"Codex app-server exited or closed its pipe before answering {method}"
        if detail:
            message += ": " + detail
        return AppServerError(self._safe_message(message))

    def _write(self, message: Dict[str, Any], deadline: Optional[float] = None) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise AppServerError("App-server client is not open; use it as a context manager")
        try:
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            process.stdin.flush()
        except (OSError, ValueError):
            raise self._connection_error(message["method"], deadline) from None

    def request(self, method: str, params: Any = None) -> Any:
        """Send one request, ignoring banners, notifications, and unrelated ids."""
        with self._request_lock:
            request_id = self._next_id
            self._next_id += 1
            message: Dict[str, Any] = {"id": request_id, "method": method}
            if params is not None:
                message["params"] = params
            deadline = time.monotonic() + self.timeout
            self._write(message, deadline)
            deadline = time.monotonic() + self.timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AppServerTimeout(
                        self._safe_message(
                            f"Codex app-server request {method} timed out after {self.timeout} seconds"
                        )
                    )
                try:
                    response = self._messages.get(timeout=remaining)
                except queue.Empty:
                    if self._process is not None and self._process.poll() is not None:
                        raise self._connection_error(method, deadline) from None
                    raise AppServerTimeout(
                        self._safe_message(
                            f"Codex app-server request {method} timed out after {self.timeout} seconds"
                        )
                    ) from None
                if response is _EOF:
                    # Preserve EOF so a subsequent request cannot hang on a closed pipe.
                    self._messages.put(_EOF)
                    raise self._connection_error(method, deadline)
                if response.get("id") != request_id or "method" in response:
                    continue
                error = response.get("error")
                if isinstance(error, dict):
                    message = self._safe_message(
                        "{} (code {})".format(
                            error.get("message", "App-server error"), error.get("code")
                        )
                    )
                    if _is_auth_error(error):
                        # Health must show a rejected credential, not a generic fault.
                        raise AuthExpired(message + "; re-login for this account")
                    raise AppServerError(message)
                return response.get("result")

    def read_rate_limits(self) -> UsageSnapshot:
        result = self.request("account/rateLimits/read")
        return UsageSnapshot.from_api(result, fetched_at=time.time())

    def consume_reset_credit(self, idempotency_key: str, credit_id: Optional[str] = None) -> str:
        params = {"idempotencyKey": idempotency_key}
        if credit_id is not None:
            params["creditId"] = credit_id
        result = self.request("account/rateLimitResetCredit/consume", params)
        if isinstance(result, dict):
            result = result.get("outcome", result.get("result"))
            if isinstance(result, dict):
                result = result.get("outcome")
        # Only the four documented outcomes may leave this call. An arbitrary string
        # would be printed and logged verbatim, straight past the redactor.
        if isinstance(result, str) and result in _RESET_OUTCOMES:
            return result
        raise AppServerError("Unrecognised app-server reset-credit outcome")

    def __exit__(self, *exc: Any) -> None:
        process = self._process
        if process is None:
            return
        # Cleanup must preserve the original error, including a failed handshake.
        if process.stdin is not None:
            with suppress(BaseException):
                process.stdin.close()
        for action in (None, process.terminate, process.kill):
            with suppress(BaseException):
                if action is not None:
                    action()
                process.wait(timeout=3)
                break
        for thread in (self._stdout_thread, self._stderr_thread):
            if thread is not None:
                with suppress(BaseException):
                    thread.join(timeout=3)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                with suppress(BaseException):
                    stream.close()
        self._process = None
        self._secrets.clear()
        self._stderr_tail = ""


def probe_usage(
    codex_home: Path,
    *,
    timeout: float = 45.0,
    codex_bin: Optional[Union[str, List[str]]] = None,
) -> UsageSnapshot:
    """Read a usage snapshot using an isolated Codex home and close the server."""
    with AppServerClient(codex_home, timeout=timeout, codex_bin=codex_bin) as client:
        return client.read_rate_limits()
