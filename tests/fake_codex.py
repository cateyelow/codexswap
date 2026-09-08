"""A disk-backed Codex double, with no imports from codexswap.

``write_fake_codex`` returns the Python argv prefix accepted by AppServerClient;
``accounts`` maps CODEX_HOME directories to initial fake-profile.json objects.
The generated script reads that file afresh for every RPC and records requests in
fake-calls.jsonl in the same home. Set FAKE_CODEX_TEST_ROOT to the temporary root;
the fake refuses to access a CODEX_HOME outside it.

For the *unmodified installed CLI*, CODEX_BIN is a single filename and Windows
find_codex_binary explicitly requires .exe. An environment variable cannot make
Python interpret ``app-server`` as our script, and Python handles ``--version``
and unknown flags before startup hooks. The list override is available only to
Python API callers. Consequently the proposed exe-free alternatives cannot cover
the CLI. write_fake_codex_exe uses a tiny native shim on Windows, built using only
struct: an ordinary x86 PE importing the Windows CRT's argument parser and spawn
function. It runs [sys.executable, script, *argv], inherits environment and stdio,
waits, and propagates the exit code. No compiler, shell, downloaded binary, copied
Python executable, startup hook, or production monkeypatch is involved. x86 also
runs on 64-bit Windows via WOW64. POSIX uses a quoted /bin/sh exec launcher.
"""

from __future__ import annotations

import json
import os
import shlex
import struct
import subprocess
import sys
from pathlib import Path

_SCRIPT = r'''"""Generated fake Codex: private test data only."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def emit(value):
    print(json.dumps(value), flush=True)


def main():
    config = json.loads(Path(__file__).with_name("fake-version.json").read_text(encoding="utf-8"))
    root = Path(os.environ["FAKE_CODEX_TEST_ROOT"]).resolve()
    home = Path(os.environ["CODEX_HOME"]).resolve()
    home.relative_to(root)  # Fail closed before reading or writing any account file.
    args = sys.argv[1:]
    home.mkdir(parents=True, exist_ok=True)

    def record(**entry):
        with (home / "fake-calls.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(dict(entry, codexHome=str(home))) + "\n")

    record(argv=args)
    if args == ["--version"]:
        print(config["version"], flush=True)
        return 0
    if args != ["app-server"]:
        emit(args)
        return 0

    profile_path = home / "fake-profile.json"
    initialized = False
    acknowledged = False
    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        record(request=request)
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        mode = profile.get("failure")
        if mode == "timeout":
            # Never reply, but exit on EOF so timeout cleanup leaves no orphan.
            continue
        if mode in ("unauthorized", "crash"):
            print("unauthorized" if mode == "unauthorized" else "simulated engine failure",
                  file=sys.stderr, flush=True)
            return 1 if mode == "unauthorized" else 2
        request_id = request.get("id")

        def error(code, message):
            emit({"id": request_id, "error": {"code": code, "message": message}})

        if method == "initialize":
            info = (request.get("params") or {}).get("clientInfo", {})
            if initialized or not info.get("name") or not info.get("version"):
                error(-32600, "invalid initialize handshake")
                continue
            initialized = True
            emit({"id": request_id, "result": {"userAgent": config["version"]}})
            continue
        if method == "initialized":
            # CONTRACT 1.4: this is a CLIENT notification, not a server reply.
            if not initialized or "id" in request or request.get("params") is not None:
                return 2
            acknowledged = True
            continue
        if not acknowledged:
            error(-32000, "initialized notification required before account requests")
            continue
        if mode == "error":
            error(-32001, "simulated rate-limit service error")
            continue
        if method == "account/rateLimits/read":
            if request.get("params") is not None:
                error(-32602, "rateLimits/read requires omitted or null params")
                continue
            limit = {
                "limitId": "codex", "limitName": None,
                "primary": profile["primary"], "secondary": profile["secondary"],
                "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
                "individualLimit": None, "spendControlReached": False,
                "planType": "pro", "rateLimitReachedType": None,
            }
            credits = profile.get("credits", [])
            emit({"id": request_id, "result": {
                "rateLimits": limit, "rateLimitsByLimitId": {"codex": limit},
                "rateLimitResetCredits": {
                    "availableCount": sum(c["status"] == "available" for c in credits),
                    "credits": credits,
                },
                "accountId": profile["accountId"], "rateLimitUpsell": None,
            }})
        elif method == "account/rateLimitResetCredit/consume":
            params = request.get("params") or {}
            key = params.get("idempotencyKey")
            if not isinstance(key, str) or not key:
                error(-32602, "idempotencyKey is required")
                continue
            outcomes = profile.setdefault("redemptions", {})
            if key in outcomes:
                outcome = outcomes[key]
            else:
                credits = profile.get("credits", [])
                credit_id = params.get("creditId")
                credit = next((c for c in credits if c["id"] == credit_id), None)
                if credit_id is None:
                    available = [c for c in credits if c["status"] == "available"]
                    credit = min(available, key=lambda c: c.get("expiresAt") or float("inf"),
                                 default=None)
                if credit is None:
                    outcome = "noCredit"
                elif credit["status"] != "available":
                    outcome = "alreadyRedeemed"
                elif not any(profile[w] and profile[w]["usedPercent"]
                             for w in ("primary", "secondary")):
                    outcome = "nothingToReset"
                else:
                    credit["status"] = "redeemed"
                    for window in ("primary", "secondary"):
                        if profile[window] is not None:
                            profile[window]["usedPercent"] = 0
                    outcome = "reset"
                outcomes[key] = outcome
                temporary = profile_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
                os.replace(temporary, profile_path)
            emit({"id": request_id, "result": {"outcome": outcome}})
        else:
            error(-32601, "unknown method")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def write_fake_codex(
    directory: Path, *, accounts: dict, version: str = "codex-cli 0.153.4-fake",
) -> list[str]:
    """Write the standalone script and profiles keyed by account home directory."""
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "fake-codex.py"
    script.write_text(_SCRIPT, encoding="utf-8")
    (directory / "fake-version.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    for home, profile in accounts.items():
        home = Path(home)
        home.mkdir(parents=True, exist_ok=True)
        (home / "fake-profile.json").write_text(json.dumps(profile), encoding="utf-8")
    return [sys.executable, str(script)]


def _windows_shim(script: Path) -> bytes:
    """Build a minimal PE32 console launcher using Windows' own msvcrt.dll.

    The straight-line x86 instructions below correspond to:
      __wgetmainargs(&argc, &argv, &env, 0, &startup_info);
      tail = original command line after the executable name;
      child = [quoted_python, quoted_script, tail, NULL];
      exit(_wspawnv(_P_WAIT, python, child));
    _wspawnv joins its arguments without quoting. Preserve the original argument
    tail verbatim and quote only the two new paths with subprocess.list2cmdline.
    """
    image_base, text_rva, data_rva = 0x400000, 0x1000, 0x2000
    data = bytearray(40)  # One import descriptor followed by a null descriptor.

    def allocate(raw: bytes, alignment: int = 4) -> int:
        data.extend(b"\0" * (-len(data) % alignment))
        address = image_base + data_rva + len(data)
        data.extend(raw)
        return address

    def word(value: int) -> bytes:
        return struct.pack("<I", value)

    dll = allocate(b"msvcrt.dll\0")
    names = ["__wgetmainargs", "_wcmdln", "_wspawnv", "exit"]
    hints = [allocate(b"\0\0" + name.encode("ascii") + b"\0", 2) for name in names]
    thunks = b"".join(word(address - image_base) for address in hints) + word(0)
    lookup = allocate(thunks)
    iat = allocate(thunks)
    struct.pack_into("<IIIII", data, 0, lookup - image_base, 0, 0,
                     dll - image_base, iat - image_base)
    argc, argv, env, startup = [allocate(word(0)) for _ in range(4)]
    python = allocate((sys.executable + "\0").encode("utf-16le"))
    quoted = [allocate((subprocess.list2cmdline([p]) + "\0").encode("utf-16le"))
              for p in (sys.executable, str(script))]
    child = allocate(b"".join(word(p) for p in [*quoted, 0, 0]))
    code = bytearray()
    labels = {}
    jumps = []

    def push(value: int) -> None:
        code.extend(b"\x68" + word(value))

    def call(name: str) -> None:
        code.extend(b"\xff\x15" + word(iat + 4 * names.index(name)))

    def jump(opcode: int, label: str) -> None:
        code.extend(bytes([opcode, 0]))
        jumps.append((len(code) - 1, label))

    for value in (startup, 0, env, argv, argc):
        push(value)
    call("__wgetmainargs")
    code.extend(b"\x83\xc4\x14")  # add esp, 20
    code.extend(b"\xa1" + word(iat + 4) + b"\x8b\x30\xfc")  # esi = _wcmdln; cld
    code.extend(b"\x66\xad\x66\x83\xf8\x22")  # lodsw; cmp ax, '"'
    jump(0x74, "quoted")
    labels["unquoted"] = len(code)
    code.extend(b"\x66\x85\xc0")  # test ax, ax
    jump(0x74, "end")
    code.extend(b"\x66\x83\xf8\x20")  # cmp ax, ' '
    jump(0x74, "tail")
    code.extend(b"\x66\xad")
    jump(0xEB, "unquoted")
    labels["quoted"] = len(code)
    code.extend(b"\x66\xad\x66\x83\xf8\x22")  # find closing quote
    jump(0x75, "quoted")
    jump(0xEB, "tail")
    labels["end"] = len(code)
    code.extend(b"\x83\xee\x02")  # back up to the terminating NUL
    labels["tail"] = len(code)
    code.extend(b"\x89\x35" + word(child + 8))  # child[2] = untouched argument tail
    push(child)
    push(python)
    push(0)  # _P_WAIT
    call("_wspawnv")
    code.extend(b"\x83\xc4\x0c\x50")  # push returned exit status
    call("exit")
    for position, label in jumps:
        struct.pack_into("<b", code, position, labels[label] - position - 1)

    headers = bytearray(512)
    headers[:2] = b"MZ"
    struct.pack_into("<I", headers, 0x3C, 0x80)
    headers[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", headers, 0x84, 0x14C, 2, 0, 0, 0, 224, 0x103)
    optional = 0x98
    data_size = (len(data) + 511) // 512 * 512
    struct.pack_into("<H", headers, optional, 0x10B)  # PE32
    struct.pack_into("<IIIIIIIII", headers, optional + 4,
                     512, data_size, 0, text_rva, text_rva, data_rva,
                     image_base, 4096, 512)
    struct.pack_into("<HHHHHH", headers, optional + 40, 6, 0, 0, 0, 6, 0)
    image_size = (data_rva + len(data) + 4095) // 4096 * 4096
    struct.pack_into("<II", headers, optional + 56, image_size, 512)
    struct.pack_into("<HH", headers, optional + 68, 3, 0x100)  # Console, NX compatible
    struct.pack_into("<IIIIII", headers, optional + 72, 1 << 20, 4096, 1 << 20, 4096, 0, 16)
    struct.pack_into("<II", headers, optional + 104, data_rva, 40)  # Import directory
    struct.pack_into("<II", headers, optional + 192, iat - image_base, len(thunks))
    for index, (name, size, rva, raw_size, offset, flags) in enumerate((
        (b".text", len(code), text_rva, 512, 512, 0x60000020),
        (b".data", len(data), data_rva, data_size, 1024, 0xC0000040),
    )):
        struct.pack_into("<8sIIIIIIHHI", headers, optional + 224 + index * 40,
                         name, size, rva, raw_size, offset, 0, 0, 0, 0, flags)
    assert len(code) <= 512
    return bytes(headers + code.ljust(512, b"\0") + data.ljust(data_size, b"\0"))


def write_fake_codex_exe(directory: Path) -> Path:
    """Return one directly executable CODEX_BIN path; preserve an existing script."""
    directory = Path(directory).resolve()
    script = directory / "fake-codex.py"
    if not script.exists():
        write_fake_codex(directory, accounts={})
    if os.name == "nt":
        launcher = directory / "fake-codex.exe"
        launcher.write_bytes(_windows_shim(script))
    else:
        launcher = directory / "fake-codex"
        # exec preserves status/signals; quoting preserves spaces in both paths.
        launcher.write_text(
            "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " "
            + shlex.quote(str(script)) + ' "$@"\n', encoding="utf-8",
        )
        launcher.chmod(0o755)
    return launcher
