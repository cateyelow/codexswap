from __future__ import annotations

import io
import json

import pytest
from conftest import RATE_LIMITS_RESULT, make_auth

from codexswap import appserver, cli, errors, paths, resets, switcher
from codexswap.models import UsageSnapshot
from codexswap.store import AccountStore

NOW = 1_788_912_000.0

# CONTRACT section 9: keep each command/alias explicit so omissions are visible.
COMMAND_ARGV = [
    ["help"], ["version"],
    ["list"], ["list", "--json", "--token-status", "--no-probe"], ["ls"],
    ["status", "--json"], ["current"], ["st"],
    ["switch"], ["switch", "2", "--force", "--json"],
    ["switch", "--strategy", "best"], ["switch", "--strategy", "next-available"],
    ["add"], ["add", "--slot", "2", "--alias", "work"],
    ["remove", "2"], ["rm", "2"], ["disable", "2"], ["enable", "2"],
    ["alias"], ["alias", "2", "work"], ["alias", "2", "--unset"],
    ["swap", "1", "2"], ["move", "1", "3"],
    ["run"], ["run", "2"], ["run", "2", "--", "--resume"], ["run", "--", "--resume"],
    ["map"], ["map", "2"], ["map", "2", "project"],
    ["unmap"], ["unmap", "project"],
    ["probe"], ["probe", "2", "--json"],
    ["reset"], ["reset", "--json"], ["reset", "list"], ["reset", "list", "--json"],
    ["reset", "use"], ["reset", "use", "2", "--credit", "fixture-credit", "--yes", "--dry-run"],
    ["auto"], ["auto", "--once", "--dry-run", "--interval", "60", "--threshold", "80"],
    ["config"], ["config", "--json"],
    ["config", "set", "autoswitch.threshold", "70", "--json"],
    ["config", "unset", "autoswitch.threshold", "--json"],
    ["export", "accounts.json"], ["export", "accounts.json", "--account", "2"],
    ["import", "accounts.json"], ["import", "accounts.json", "--force"],
    ["purge"], ["purge", "--yes"],
]


@pytest.fixture(autouse=True)
def offline_cli(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("A CLI test attempted a real probe, redemption, or subprocess")

    monkeypatch.setattr(appserver, "probe_usage", forbidden)
    monkeypatch.setattr(resets, "redeem", forbidden)
    monkeypatch.setattr(appserver, "find_codex_binary", forbidden)
    for name in ("run", "call", "Popen"):
        monkeypatch.setattr(cli.subprocess, name, forbidden)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(switcher, "detect_running_codex", lambda: [])
    monkeypatch.setattr(cli.time, "time", lambda: NOW)


@pytest.fixture
def seeded_store():
    store = AccountStore.load()
    store.add_from_auth(make_auth(id_exp=int(NOW + 3600), access_exp=int(NOW + 86400)),
                        alias="main", now=NOW)
    store.add_from_auth(make_auth(email="b@example.com", account_id="acct-second",
                                  plan_type="plus", id_exp=int(NOW + 3600),
                                  access_exp=int(NOW + 86400)), alias="backup", now=NOW)
    store.set_disabled(2, True)
    store.set_active(1)
    store.record_usage(1, UsageSnapshot.from_api(RATE_LIMITS_RESULT, fetched_at=NOW))
    return store


@pytest.mark.parametrize("argv", COMMAND_ARGV, ids=lambda argv: " ".join(argv))
def test_parser_accepts_every_contract_command_and_alias(argv):
    parsed = cli.build_parser().parse_args(argv)
    canonical = {"ls": "list", "current": "status", "st": "status", "rm": "remove"}
    assert parsed.command == canonical.get(argv[0], argv[0])
    if argv[:2] in (["reset", "list"], ["reset", "use"]):
        assert parsed.reset_command == argv[1]
    if argv[:2] in (["config", "set"], ["config", "unset"]):
        assert parsed.config_command == argv[1]


def test_parser_accepts_global_flags(tmp_path):
    parsed = cli.build_parser().parse_args([
        "--debug", "--no-color", "--home", str(tmp_path), "list", "--no-probe",
    ])
    assert parsed.debug is True
    assert parsed.no_color is True
    assert parsed.home == tmp_path
    assert parsed.command == "list"


@pytest.mark.parametrize("argv", [["--help"], [], ["help"]])
def test_help_returns_success_and_prints_usage(argv, capsys):
    assert cli.main(argv) == 0
    captured = capsys.readouterr()
    assert "usage:" in captured.out
    assert "COMMAND" in captured.out
    assert captured.err == ""


@pytest.mark.parametrize("argv", [["version"], ["--version"]])
def test_version_returns_success_without_launching_codex(argv, capsys):
    assert cli.main(argv) == 0
    captured = capsys.readouterr()
    assert "codexswap " in captured.out
    assert captured.err == ""


@pytest.mark.parametrize("error_class,exit_code", [
    (errors.CodexSwapError, 1), (errors.UserError, 2), (errors.AccountNotFound, 2),
    (errors.NoAccountsConfigured, 2), (errors.SlotInUse, 2), (errors.CodexRunning, 2),
    (errors.AuthFileMissing, 1), (errors.AuthFileInvalid, 1),
    (errors.AppServerError, 3), (errors.AppServerTimeout, 3),
    (errors.CodexBinaryNotFound, 3), (errors.AuthExpired, 4),
    (errors.BackendError, 5), (errors.LockBusy, 6),
])
def test_main_reports_domain_error_exit_codes(monkeypatch, capsys, error_class, exit_code):
    def fail_capture(*args, **kwargs):
        raise error_class("fixture failure")

    monkeypatch.setattr(switcher, "capture_current", fail_capture)
    assert cli.main(["add"]) == exit_code == error_class.exit_code
    captured = capsys.readouterr()
    assert captured.err == "error: fixture failure\n"
    assert captured.out == ""


def expected_first_account():
    return {
        "slot": 1, "email": "a@example.com", "alias": "main", "disabled": False,
        "planType": "pro", "health": "ok",
        "usage": {
            "bindingPercent": 84.0,
            "primary": {"usedPercent": 84.0, "windowMinutes": 10080, "resetsAt": 1789435573},
            "secondary": None, "resetCreditsAvailable": 2, "fetchedAt": NOW, "stale": False,
        },
    }


def test_list_no_probe_json_is_one_contract_object(seeded_store, capsys):
    assert cli.main(["list", "--no-probe", "--json"]) == 0
    captured = capsys.readouterr()
    # json.loads rejects both a second JSON object and trailing status text.
    document = json.loads(captured.out)
    assert document == {"activeSlot": 1, "accounts": [expected_first_account(), {
        "slot": 2, "email": "b@example.com", "alias": "backup", "disabled": True,
        "planType": "plus", "health": "ok", "usage": None,
    }]}
    assert captured.err == ""


def test_status_json_is_one_contract_object(seeded_store, capsys):
    assert cli.main(["status", "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"activeSlot": 1, "account": expected_first_account()}
    assert captured.err == ""


def test_switch_json_is_one_contract_object(seeded_store, live_auth, capsys):
    assert cli.main(["switch", "2", "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "from": 1, "to": 2, "account": {"slot": 2, "email": "b@example.com"},
    }
    assert AccountStore.load().active_slot == 2
    assert captured.err == ""


def test_run_separator_forwards_exact_codex_arguments(seeded_store, monkeypatch):
    calls = []

    def fake_run(store, account, argv):
        calls.append((account.slot, argv))
        return 0

    monkeypatch.setattr(switcher, "run_as", fake_run)
    assert cli.main(["run", "2", "--", "--resume"]) == 0
    assert calls == [(2, ["--resume"])]


def fake_reset_probe(monkeypatch):
    calls = []

    def probe(home, **kwargs):
        calls.append(home)
        return UsageSnapshot.from_api(RATE_LIMITS_RESULT, fetched_at=NOW)

    monkeypatch.setattr(appserver, "probe_usage", probe)
    return calls


def test_reset_dry_run_does_not_redeem_or_prompt(seeded_store, monkeypatch, capsys):
    calls = fake_reset_probe(monkeypatch)

    def forbidden_input(*args):
        pytest.fail("A dry run must not ask to redeem")

    monkeypatch.setattr("builtins.input", forbidden_input)
    # offline_cli makes any call to resets.redeem fail the test.
    assert cli.main(["reset", "use", "--dry-run"]) == 0
    assert calls == [paths.slot_home(1)]
    assert "would redeem" in capsys.readouterr().out


def test_declined_reset_confirmation_does_not_redeem(seeded_store, monkeypatch, capsys):
    calls = fake_reset_probe(monkeypatch)
    prompts = []

    def decline(*args):
        prompts.append(args)
        return "n"

    monkeypatch.setattr("builtins.input", decline)
    assert cli.main(["reset", "use"]) == 0
    assert len(prompts) == 1
    assert calls == [paths.slot_home(1)]
    assert "cancelled" in capsys.readouterr().out


def test_declined_purge_preserves_store_and_codex_home(seeded_store, codex_root, monkeypatch, capsys):
    sentinel = codex_root / "auth.json"
    sentinel.write_bytes(b"live-home-must-survive")
    store_before = {path.relative_to(paths.codexswap_home()): path.read_bytes()
                    for path in paths.codexswap_home().rglob("*") if path.is_file()}
    live_before = {path.relative_to(codex_root): path.read_bytes()
                   for path in codex_root.rglob("*") if path.is_file()}
    prompts = []

    class InteractiveInput(io.StringIO):
        def isatty(self):
            return True

    def decline(*args):
        prompts.append(args)
        return "n"

    monkeypatch.setattr(cli.sys, "stdin", InteractiveInput())
    monkeypatch.setattr("builtins.input", decline)
    assert cli.main(["purge"]) == 0
    assert len(prompts) == 1
    assert "cancelled" in capsys.readouterr().out
    assert {path.relative_to(paths.codexswap_home()): path.read_bytes()
            for path in paths.codexswap_home().rglob("*") if path.is_file()} == store_before
    assert {path.relative_to(codex_root): path.read_bytes()
            for path in codex_root.rglob("*") if path.is_file()} == live_before
    assert AccountStore.load().accounts == seeded_store.accounts


@pytest.mark.parametrize("cached", [False, True])
def test_probe_failure_still_lists_account(monkeypatch, capsys, cached):
    store = AccountStore.load()
    account = store.add_from_auth(make_auth(id_exp=int(NOW + 3600), access_exp=int(NOW + 86400)), now=NOW)
    if cached:
        store.record_usage(account.slot, UsageSnapshot.from_api(RATE_LIMITS_RESULT, fetched_at=NOW - 121))
    calls = []

    def fail_probe(home, **kwargs):
        calls.append(home)
        raise errors.AppServerError("fixture unavailable")

    monkeypatch.setattr(appserver, "probe_usage", fail_probe)
    assert cli.main(["list", "--no-color"]) == 0
    captured = capsys.readouterr()
    assert account.identity.email in captured.out
    assert calls == [paths.slot_home(account.slot)]
    assert "stale" in captured.out if cached else "unavailable" in captured.out
    assert captured.err == ""
