"""Black-box tests of the installed ``python -m codexswap`` command.

Install the checkout first (``python -m pip install -e .``). Children run outside
the repository, without PYTHONPATH injection or production monkeypatches. Every
home and fake process is confined to tmp_path; neither the user's credentials nor
their Codex executable is used. Manual switches use the documented --force flag
so unrelated Codex sessions on a developer's machine cannot block these tests.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import make_auth, make_jwt
from fake_codex import write_fake_codex, write_fake_codex_exe


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(value, indent=2) + "\n").encode("utf-8"))


def file_bytes(path: Path):
    return path.read_bytes() if path.exists() else None


def profile(slot: int, primary=30, secondary=None, *, credits=0, failure=None) -> dict:
    now = int(time.time())
    return {
        "accountId": f"fake-account-{slot}",
        "primary": {"usedPercent": primary, "windowDurationMins": 300, "resetsAt": now + 18000},
        "secondary": {"usedPercent": primary if secondary is None else secondary,
                      "windowDurationMins": 10080, "resetsAt": now + 604800},
        "credits": [
            {"id": f"credit-{slot}-{index}", "resetType": "codexRateLimits",
             "status": "available", "grantedAt": now - 86400,
             "expiresAt": now + (index + 1) * 86400, "title": "Full reset",
             "description": "Synthetic integration-test credit"}
            for index in range(credits)
        ],
        "failure": failure,
    }


class CLI:
    def __init__(self, root: Path, fake_binary):
        self.root = root.resolve()
        self.home = self.root / "swap"
        self.live = self.root / "live"
        self.user = self.root / "user"
        self.cwd = self.root / "working directory"
        for directory in (self.home, self.live, self.user, self.cwd):
            directory.mkdir()
        self.prefix, self.binary = fake_binary
        self.env = os.environ.copy()
        for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT"):
            self.env.pop(key, None)
        self.env.update({
            "CODEXSWAP_HOME": str(self.home), "CODEX_HOME": str(self.live),
            "CODEX_BIN": str(self.binary), "FAKE_CODEX_TEST_ROOT": str(self.root),
            "HOME": str(self.user), "USERPROFILE": str(self.user),
            "APPDATA": str(self.user / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(self.user / "AppData" / "Local"),
            "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "NO_COLOR": "1",
        })
        # Bound accidental hangs without changing the default cache behavior.
        write_json(self.home / "settings.json", {
            "probe": {"timeoutSeconds": 5, "allowBackendFallback": False},
        })

    @property
    def auth_path(self):
        return self.live / "auth.json"

    def slot_home(self, slot, *, home=None):
        return (self.home if home is None else home) / "homes" / str(slot)

    def slot_auth(self, slot, *, home=None):
        return self.slot_home(slot, home=home) / "auth.json"

    def profile_path(self, slot, *, home=None):
        return self.slot_home(slot, home=home) / "fake-profile.json"

    def calls(self, slot):
        path = self.slot_home(slot) / "fake-calls.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def run(self, *args, home=None, missing_binary=False, timeout=12):
        env = dict(self.env)
        if home is not None:
            env["CODEXSWAP_HOME"] = str(home)
        if missing_binary:
            env["CODEX_BIN"] = str(self.root / "nonexistent-fake.exe")
        for key in ("CODEXSWAP_HOME", "CODEX_HOME"):
            Path(env[key]).resolve().relative_to(self.root)
        assert env["CODEX_BIN"] == str(
            self.root / "nonexistent-fake.exe" if missing_binary else self.binary,
        )
        return subprocess.run(
            [sys.executable, "-m", "codexswap", *map(str, args)], cwd=self.cwd,
            env=env, input="", capture_output=True, text=True,
            encoding="utf-8", timeout=timeout,
        )

    def ok(self, *args, **kwargs):
        result = self.run(*args, **kwargs)
        assert result.returncode == 0, (
            f"{args!r}: exit {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        return result

    def json(self, *args, **kwargs):
        # Parse the WHOLE stdout, so banners or other extra output fail the test.
        return json.loads(self.ok(*args, **kwargs).stdout)

    def add_accounts(self, *profiles):
        for slot, value in enumerate(profiles, 1):
            alias = chr(ord("a") + slot - 1)
            write_json(self.auth_path, make_auth(
                email=f"{alias}@example.com", name=f"Account {alias.upper()}",
                account_id=f"fake-account-{slot}",
            ))
            result = self.ok("add", "--alias", alias)
            assert f"saved slot {slot}" in result.stdout
            assert f"{alias}@example.com" in result.stdout
            write_json(self.profile_path(slot), value)
        if len(profiles) > 1:
            # Establish a known live/registry active account after simulating logins.
            result = self.ok("switch", "1", "--force")
            assert "switched" in result.stdout

    def seed_accounts(self, *profiles):
        """Clone pristine CLI-created accounts; each test owns every mutable file."""
        template = self.templates(len(profiles))
        shutil.copytree(template.home, self.home, dirs_exist_ok=True)
        shutil.copytree(template.live, self.live, dirs_exist_ok=True)
        for slot, value in enumerate(profiles, 1):
            write_json(self.profile_path(slot), value)


@pytest.fixture(scope="session")
def fake_binary(tmp_path_factory):
    # Share only immutable executable code to avoid repeated Windows loader scans.
    directory = tmp_path_factory.mktemp("fake codex bin")
    prefix = write_fake_codex(directory, accounts={})
    return prefix, write_fake_codex_exe(directory)


@pytest.fixture(scope="session")
def account_templates(tmp_path_factory, fake_binary):
    templates = {}

    def get(count):
        if count not in templates:
            template = CLI(tmp_path_factory.mktemp(f"accounts-{count}"), fake_binary)
            template.add_accounts(*(profile(slot) for slot in range(1, count + 1)))
            templates[count] = template
        return templates[count]

    return get


@pytest.fixture
def cli(tmp_path, fake_binary, account_templates):
    instance = CLI(tmp_path, fake_binary)
    instance.templates = account_templates
    return instance


def assert_identity(entry, slot, alias):
    assert entry["slot"] == slot
    assert entry["email"] == f"{alias}@example.com"
    assert entry["alias"] == alias


def assert_usage(entry, primary, secondary):
    usage = entry["usage"]
    assert usage is not None, f"fake usage missing for slot {entry['slot']}"
    assert usage["primary"]["usedPercent"] == primary
    assert usage["secondary"]["usedPercent"] == secondary
    assert usage["bindingPercent"] == max(primary, secondary)
    assert usage["stale"] is False


def test_two_account_lifecycle(cli):
    cli.add_accounts(profile(1, 24, 39), profile(2, 52, 67))
    listing = cli.ok("list").stdout
    assert re.search(r"1: a@example\.com.*\* active", listing)
    assert "2: b@example.com" in listing
    assert all(f"{percent}%" in listing for percent in (24, 39, 52, 67))
    data = cli.json("list", "--json")
    assert len(data["accounts"]) == 2
    assert_identity(data["accounts"][0], 1, "a")
    assert_identity(data["accounts"][1], 2, "b")
    assert_usage(data["accounts"][0], 24, 39)
    assert_usage(data["accounts"][1], 52, 67)
    status = cli.ok("status").stdout
    assert re.search(r"1: a@example\.com.*\* active", status)
    assert "b@example.com" not in status


def test_switch_round_trip_preserves_other_slot_bytes(cli):
    cli.seed_accounts(profile(1), profile(2))
    original_a = cli.slot_auth(1).read_bytes()
    original_b = cli.slot_auth(2).read_bytes()
    assert "switched 1 -> 2" in cli.ok("switch", "b", "--force").stdout
    assert cli.auth_path.read_bytes() == original_b
    assert cli.slot_auth(1).read_bytes() == original_a
    assert cli.json("status", "--json")["activeSlot"] == 2
    assert "switched 2 -> 1" in cli.ok("switch", "1", "--force").stdout
    assert cli.auth_path.read_bytes() == original_a
    assert cli.slot_auth(2).read_bytes() == original_b
    assert cli.json("status", "--json")["activeSlot"] == 1


def test_switch_preserves_live_token_refresh(cli):
    cli.seed_accounts(profile(1), profile(2))
    old_auth = cli.slot_auth(1).read_bytes()
    refreshed = read_json(cli.auth_path)
    refreshed["last_refresh"] = "2026-09-09T12:34:56.123Z"
    refreshed["tokens"]["access_token"] = make_jwt({
        "exp": int(time.time()) + 7200, "integration_refresh": "new-access-token",
    })
    write_json(cli.auth_path, refreshed)
    assert "switched 1 -> 2" in cli.ok("switch", "b", "--force").stdout
    assert read_json(cli.slot_auth(1)) == refreshed
    assert cli.slot_auth(1).read_bytes() != old_auth
    assert "switched 2 -> 1" in cli.ok("switch", "1", "--force").stdout
    assert read_json(cli.auth_path) == refreshed
    assert cli.auth_path.read_bytes() == cli.slot_auth(1).read_bytes()


@pytest.mark.parametrize("strategy,target", [("best", 3), ("next-available", 2)])
def test_switch_strategies(cli, strategy, target):
    # The 60% slot precedes 20%, so these strategies must choose different targets.
    cli.seed_accounts(profile(1, 90), profile(2, 60), profile(3, 20))
    result = cli.ok("switch", "--strategy", strategy, "--force")
    assert f"switched 1 -> {target}" in result.stdout
    assert cli.auth_path.read_bytes() == cli.slot_auth(target).read_bytes()
    assert cli.json("status", "--json")["activeSlot"] == target
    if strategy == "next-available":
        cli.ok("switch", "3", "--force")
        assert "switched 3 -> 2" in cli.ok(
            "switch", "--strategy", strategy, "--force",
        ).stdout


def test_strategy_rejects_alternatives_above_hysteresis(cli):
    cli.seed_accounts(profile(1, 90), profile(2, 20, 71), profile(3, 81))
    before = cli.auth_path.read_bytes()
    result = cli.run("switch", "--strategy", "best", "--force")
    assert result.returncode == 2, result.stderr
    assert result.stdout == ""
    assert "no eligible target" in result.stderr
    assert "70%" in result.stderr and "hysteresis" in result.stderr
    assert cli.auth_path.read_bytes() == before


def test_corrupt_slot_cannot_damage_live_auth(cli):
    cli.seed_accounts(profile(1), profile(2))
    before = cli.auth_path.read_bytes()
    cli.slot_auth(2).write_bytes(b"not JSON: deliberately corrupt\xff")
    result = cli.run("switch", "b", "--force")
    assert result.returncode != 0
    assert result.stdout == ""
    assert "error:" in result.stderr
    assert cli.auth_path.read_bytes() == before
    assert read_json(cli.home / "accounts.json")["activeSlot"] == 1


@pytest.mark.parametrize("forwarded", [
    ["--resume", "extra"],
    ["--resume", "two words", "", 'embedded"quote', "trailing\\", "unicode-\u03bb"],
])
def test_run_isolates_home_and_forwards_arguments(cli, forwarded):
    cli.seed_accounts(profile(1), profile(2))
    before = cli.auth_path.read_bytes()
    registry = (cli.home / "accounts.json").read_bytes()
    result = cli.ok("run", "2", "--", *forwarded)
    assert json.loads(result.stdout) == forwarded
    calls = cli.calls(2)
    assert calls == [{"argv": forwarded, "codexHome": str(cli.slot_home(2).resolve())}]
    assert cli.auth_path.read_bytes() == before
    assert (cli.home / "accounts.json").read_bytes() == registry


@pytest.mark.parametrize("view", ["credits", "windows"])
def test_reset_redemption_is_visible_to_next_read(cli, view):
    cli.seed_accounts(profile(1, 92, 85, credits=2))
    before_live = cli.auth_path.read_bytes()
    result = cli.ok("reset", "use", "--yes")
    assert "reset slot 1: reset" in result.stdout
    after = read_json(cli.profile_path(1))
    assert [c["status"] for c in after["credits"]] == ["redeemed", "available"]
    assert after["primary"]["usedPercent"] == after["secondary"]["usedPercent"] == 0
    assert cli.auth_path.read_bytes() == before_live
    calls = [c["request"] for c in cli.calls(1) if "request" in c]
    consume = [r for r in calls if r["method"] == "account/rateLimitResetCredit/consume"]
    assert len(consume) == 1
    assert consume[0]["params"]["creditId"] == "credit-1-0"
    assert consume[0]["params"]["idempotencyKey"]
    if view == "credits":
        following_reset = cli.ok("reset").stdout
        assert "1 available" in following_reset, following_reset
    else:
        assert_usage(cli.json("list", "--json")["accounts"][0], 0, 0)


def test_reset_dry_run_does_not_redeem(cli):
    cli.seed_accounts(profile(1, 92, 85, credits=2))
    before = cli.profile_path(1).read_bytes()
    auth_before = cli.auth_path.read_bytes()
    state_before = file_bytes(cli.home / "state.json")
    result = cli.ok("reset", "use", "--dry-run")
    assert "would redeem credit for slot 1" in result.stdout
    assert "credit-1-0" in result.stdout
    assert cli.profile_path(1).read_bytes() == before
    assert cli.auth_path.read_bytes() == auth_before
    assert file_bytes(cli.home / "state.json") == state_before
    assert not any(c.get("request", {}).get("method") == "account/rateLimitResetCredit/consume"
                   for c in cli.calls(1))


def test_auto_once_switches_exhausted_account(cli):
    cli.seed_accounts(profile(1, 95), profile(2, 10))
    result = cli.ok("auto", "--once")
    assert "switched:" in result.stdout and "1 -> 2" in result.stdout
    assert cli.auth_path.read_bytes() == cli.slot_auth(2).read_bytes()
    assert read_json(cli.home / "accounts.json")["activeSlot"] == 2
    state = read_json(cli.home / "state.json")
    assert state["lastSwitchAt"] is not None
    assert state["cooldownUntil"] > state["lastSwitchAt"]


def test_auto_once_is_idle_below_threshold(cli):
    cli.seed_accounts(profile(1, 10), profile(2, 95))
    before = cli.auth_path.read_bytes()
    result = cli.ok("auto", "--once")
    assert "idle:" in result.stdout and "10% below 80%" in result.stdout
    assert cli.auth_path.read_bytes() == before
    assert read_json(cli.home / "accounts.json")["activeSlot"] == 1


@pytest.mark.parametrize("existing_state", [False, True], ids=["no-state", "existing-state"])
def test_auto_dry_run_preserves_live_auth_and_state(cli, existing_state):
    cli.seed_accounts(profile(1, 95), profile(2, 10))
    state_path = cli.home / "state.json"
    if existing_state:
        write_json(state_path, {
            "lastSwitchAt": None, "cooldownUntil": None,
            "unhealthy": {"1": 2}, "redemptions": [],
        })
    before_state = file_bytes(state_path)
    before_auth = cli.auth_path.read_bytes()
    result = cli.ok("auto", "--once", "--dry-run")
    assert "switched:" in result.stdout and "[dry-run]" in result.stdout
    assert "1 -> 2" in result.stdout
    assert cli.auth_path.read_bytes() == before_auth
    assert read_json(cli.home / "accounts.json")["activeSlot"] == 1
    assert file_bytes(state_path) == before_state


def test_auto_redeems_by_policy_then_observes_cooldown(cli):
    cli.seed_accounts(profile(1, 95, credits=1), profile(2, 10))
    for key, value in (("reset.policy", "always"), ("reset.minUsagePercent", "50")):
        assert f"set {key}" in cli.ok("config", "set", key, value).stdout
    before = cli.auth_path.read_bytes()
    result = cli.ok("auto", "--once")
    assert "redeemed:" in result.stdout
    assert "switched:" not in result.stdout
    assert cli.auth_path.read_bytes() == before
    assert read_json(cli.home / "accounts.json")["activeSlot"] == 1
    after = read_json(cli.profile_path(1))
    assert after["credits"][0]["status"] == "redeemed"
    assert after["primary"]["usedPercent"] == after["secondary"]["usedPercent"] == 0
    state = read_json(cli.home / "state.json")
    assert len(state["redemptions"]) == 1
    assert state["redemptions"][0]["creditId"] == "credit-1-0"
    before_calls = cli.calls(1)
    assert "cooldown:" in cli.ok("auto", "--once").stdout
    assert cli.calls(1) == before_calls
    assert read_json(cli.profile_path(1)) == after


def test_export_import_remaps_and_force_overwrites(cli):
    cli.seed_accounts(profile(1, 23), profile(2, 47))
    exported = cli.root / "accounts export.json"
    result = cli.ok("export", exported)
    assert "exported accounts" in result.stdout
    assert "refresh tokens" in result.stderr
    envelope = read_json(exported)
    assert {"format", "version", "exportedAt", "activeSlot", "accounts"} <= envelope.keys()
    assert envelope["format"] == "codexswap-export" and envelope["version"] == 1
    if os.name != "nt":
        assert exported.stat().st_mode & 0o777 == 0o600
    second = cli.root / "second swap home"
    write_json(second / "settings.json", read_json(cli.home / "settings.json"))
    assert "imported accounts" in cli.ok("import", exported, home=second).stdout
    for slot in (1, 2):
        assert cli.slot_auth(slot, home=second).read_bytes() == cli.slot_auth(slot).read_bytes()
        # The export format transfers credentials, not the fake service's private data.
        write_json(cli.profile_path(slot, home=second), profile(slot, 23 if slot == 1 else 47))
    listing = cli.ok("list", home=second).stdout
    assert "a@example.com" in listing and "b@example.com" in listing
    data = cli.json("list", "--json", home=second)
    assert len(data["accounts"]) == 2
    assert data["activeSlot"] == 1
    for slot, alias in ((1, "a"), (2, "b")):
        assert_identity(data["accounts"][slot - 1], slot, alias)
        assert_usage(data["accounts"][slot - 1], 23 if slot == 1 else 47, 23 if slot == 1 else 47)
    remapped = cli.ok("import", exported, home=second).stdout
    assert "remapped" in remapped and "1 -> 3" in remapped and "2 -> 4" in remapped
    assert [a["slot"] for a in read_json(second / "accounts.json")["accounts"]] == [1, 2, 3, 4]
    assert cli.slot_auth(3, home=second).read_bytes() == cli.slot_auth(1).read_bytes()
    assert cli.slot_auth(4, home=second).read_bytes() == cli.slot_auth(2).read_bytes()
    # Make overwriting observable: alter an occupied slot's alias and credentials.
    cli.ok("alias", "1", "changed", home=second)
    cli.slot_auth(1, home=second).write_bytes(b"deliberately replaced credential")
    forced = cli.ok("import", exported, "--force", home=second).stdout
    assert "imported accounts" in forced and "remapped" not in forced
    registry = read_json(second / "accounts.json")
    assert len(registry["accounts"]) == 4
    assert registry["accounts"][0]["alias"] == "a"
    assert cli.slot_auth(1, home=second).read_bytes() == cli.slot_auth(1).read_bytes()


def test_unauthorized_probe_shows_relogin_without_crashing(cli):
    cli.seed_accounts(profile(1, failure="unauthorized"))
    result = cli.ok("list")
    assert "a@example.com" in result.stdout
    assert "re-login needed" in result.stdout
    assert "authentication was rejected" in result.stdout
    entry = cli.json("list", "--json")["accounts"][0]
    assert entry["health"] == "expired"
    assert entry["usage"] is None


def test_timeout_probe_returns_stale_usage_promptly(cli):
    cli.seed_accounts(profile(1, 37))
    assert "37%" in cli.ok("list").stdout  # Establish a real previously successful probe.
    for key in ("probe.timeoutSeconds", "probe.staleSeconds"):
        assert f"set {key}" in cli.ok("config", "set", key, "5" if "timeout" in key else "0").stdout
    write_json(cli.profile_path(1), profile(1, 37, failure="timeout"))
    start = time.monotonic()
    result = cli.ok("list", timeout=30)
    elapsed = time.monotonic() - start
    # probe.timeoutSeconds is at its documented minimum of 5, and the rest is spawning
    # the CLI and the fake Codex, which costs seconds on a loaded Windows box. What
    # this pins is that a timed-out probe returns at all, with the stale reading,
    # instead of waiting out the full request or hanging.
    assert elapsed < 20, f"timed-out probe took {elapsed:.2f}s"
    assert "a@example.com" in result.stdout
    assert "37%" in result.stdout and "stale" in result.stdout


@pytest.mark.parametrize("failure", ["crash", "error"])
def test_other_probe_failures_remain_listable(cli, failure):
    cli.seed_accounts(profile(1, failure=failure))
    result = cli.ok("list")
    assert "a@example.com" in result.stdout and "usage unavailable" in result.stdout
    assert "re-login needed" not in result.stdout
    assert "Traceback" not in result.stderr


def test_json_commands_emit_one_complete_document(cli):
    cli.seed_accounts(profile(1, 34, 56, credits=2), profile(2, 15, 26, credits=1))
    listing = cli.json("list", "--json")
    assert {"activeSlot", "accounts"} <= listing.keys()
    assert listing["activeSlot"] == 1 and len(listing["accounts"]) == 2
    account_keys = {"slot", "email", "alias", "disabled", "planType", "health", "usage"}
    for entry in listing["accounts"]:
        assert account_keys <= entry.keys()
        assert {"bindingPercent", "primary", "secondary", "resetCreditsAvailable",
                "fetchedAt", "stale"} <= entry["usage"].keys()
        for window in ("primary", "secondary"):
            assert {"usedPercent", "windowMinutes", "resetsAt"} <= entry["usage"][window].keys()
    status = cli.json("status", "--json")
    assert {"activeSlot", "account"} <= status.keys()
    assert status["activeSlot"] == status["account"]["slot"] == 1
    assert status["account"]["email"] == "a@example.com"
    switched = cli.json("switch", "b", "--force", "--json")
    assert {"from", "to", "account"} <= switched.keys()
    assert switched == {"from": 1, "to": 2, "account": {"slot": 2, "email": "b@example.com"}}
    reset = cli.json("reset", "--json")
    assert {"slot", "availableCount", "credits"} <= reset.keys()
    assert reset["slot"] == 2 and reset["availableCount"] == 1
    assert {"id", "status", "expiresAt", "daysUntilExpiry", "title"} <= reset["credits"][0].keys()
    before = cli.profile_path(2).read_bytes()
    dry = cli.json("reset", "--json", "use", "--dry-run")
    assert {"slot", "creditId", "outcome", "dryRun"} <= dry.keys()
    assert dry == {"slot": 2, "creditId": "credit-2-0", "outcome": None, "dryRun": True}
    assert cli.profile_path(2).read_bytes() == before


def test_unknown_account_exit_code(cli):
    cli.seed_accounts(profile(1))
    result = cli.run("switch", "does-not-exist", "--force")
    assert result.returncode == 2
    assert result.stdout == "" and "Account not found" in result.stderr


def test_missing_binary_exit_code(cli):
    cli.seed_accounts(profile(1))
    # Keep CODEX_BIN set to a nonexistent test path: never search the real PATH.
    result = cli.run("run", "1", "--", "--resume", missing_binary=True)
    assert result.returncode == 3  # CONTRACT 4: CodexBinaryNotFound.
    assert result.stdout == "" and "CODEX_BIN" in result.stderr


def test_purge_with_closed_stdin_requires_confirmation(cli):
    cli.seed_accounts(profile(1))
    before = {p.relative_to(cli.home): p.read_bytes() for p in cli.home.rglob("*") if p.is_file()}
    live_before = cli.auth_path.read_bytes()
    result = cli.run("purge")
    assert result.returncode == 2
    assert "will delete" in result.stdout and "purged" not in result.stdout
    assert "requires --yes" in result.stderr
    assert {p.relative_to(cli.home): p.read_bytes()
            for p in cli.home.rglob("*") if p.is_file()} == before
    assert cli.auth_path.read_bytes() == live_before


def test_home_flag_overrides_environment(cli):
    override = cli.root / "override home"
    write_json(cli.auth_path, make_auth(email="override@example.com", account_id="fake-override"))
    result = cli.ok("--home", override, "add", "--alias", "override")
    assert "saved slot 1" in result.stdout and "override@example.com" in result.stdout
    assert cli.slot_auth(1, home=override).exists()
    assert not (cli.home / "accounts.json").exists()
    assert not cli.slot_auth(1).exists()
    assert cli.json("list", "--json", "--no-probe")["accounts"] == []
    data = cli.json("--home", override, "list", "--json", "--no-probe")
    assert len(data["accounts"]) == 1
    assert data["accounts"][0]["alias"] == "override"
    assert data["accounts"][0]["email"] == "override@example.com"


def test_fake_version_through_installed_cli(cli):
    result = cli.ok("version")
    assert result.stdout.splitlines()[0].startswith("codexswap ")
    assert result.stdout.splitlines()[1] == "codex 0.153.4-fake"


@pytest.mark.parametrize("launcher", ["prefix", "binary"])
def test_fake_protocol_and_launcher_conformance(cli, launcher):
    """Validate the double independently, including pipes and child exit codes."""
    cli.seed_accounts(profile(1))
    assert "a@example.com" in cli.ok("list", "--no-probe").stdout
    command = cli.prefix if launcher == "prefix" else [str(cli.binary)]
    write_json(cli.live / "fake-profile.json", profile(1, 91, 84, credits=2))
    requests = [
        {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "check", "version": "1"}}},
        {"method": "initialized", "params": None},
        {"id": 2, "method": "account/rateLimits/read"},
        {"id": 3, "method": "account/rateLimitResetCredit/consume",
         "params": {"idempotencyKey": "same-request", "creditId": "credit-1-0"}},
        {"id": 4, "method": "account/rateLimitResetCredit/consume",
         "params": {"idempotencyKey": "same-request", "creditId": "credit-1-0"}},
        {"id": 5, "method": "account/rateLimits/read"},
    ]
    result = subprocess.run(
        [*command, "app-server"], input="".join(json.dumps(r) + "\n" for r in requests),
        env=cli.env, cwd=cli.cwd, capture_output=True, text=True, encoding="utf-8", timeout=5,
    )
    assert result.returncode == 0, result.stderr
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert [r["id"] for r in responses] == [1, 2, 3, 4, 5]
    assert responses[1]["result"]["rateLimitResetCredits"]["availableCount"] == 2
    assert responses[2]["result"]["outcome"] == responses[3]["result"]["outcome"] == "reset"
    final = responses[4]["result"]
    assert final["rateLimitResetCredits"]["availableCount"] == 1
    assert final["rateLimits"]["primary"]["usedPercent"] == 0
    assert final["rateLimits"]["secondary"]["usedPercent"] == 0
    for mode, code, message in (("unauthorized", 1, "unauthorized"),
                                ("crash", 2, "simulated engine failure")):
        write_json(cli.live / "fake-profile.json", profile(1, failure=mode))
        failed = subprocess.run(
            [*command, "app-server"], input=json.dumps(requests[0]) + "\n",
            env=cli.env, cwd=cli.cwd, capture_output=True, text=True, timeout=5,
        )
        assert failed.returncode == code
        assert failed.stdout == "" and message in failed.stderr
