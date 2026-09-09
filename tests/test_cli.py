from __future__ import annotations

import io
import json
from dataclasses import replace
from pathlib import Path

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
    ["switch", "--model", "all"], ["switch", "--strategy", "best", "--model", "codex"],
    ["add"], ["add", "--slot", "2", "--alias", "work"],
    ["remove", "2"], ["rm", "2"], ["disable", "2"], ["enable", "2"],
    ["alias"], ["alias", "2", "work"], ["alias", "2", "--unset"],
    ["swap", "1", "2"], ["move", "1", "3"],
    ["run"], ["run", "2"], ["run", "2", "--", "--resume"], ["run", "--", "--resume"],
    ["map"], ["map", "2"], ["map", "2", "project"],
    ["unmap"], ["unmap", "project"],
    ["probe"], ["probe", "2", "--json"], ["probe", "--backend"],
    ["reset"], ["reset", "--json"], ["reset", "list"], ["reset", "list", "--json"],
    ["reset", "use"], ["reset", "use", "2", "--credit", "fixture-credit", "--yes", "--dry-run"],
    ["auto"], ["auto", "--once", "--dry-run", "--interval", "60", "--threshold", "80"],
    ["auto", "--model", "codex,codex_bengalfox"],
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


def _failing_probe(*args, **kwargs):
    raise errors.AppServerError("fixture probe failure")


@pytest.mark.parametrize("enable", ["flag", "setting", "neither"])
def test_backend_fallback_is_reachable_only_through_its_two_switches(
    seeded_store, monkeypatch, capsys, enable,
):
    from codexswap import backend

    calls = []
    fresh = replace(
        UsageSnapshot.from_api(RATE_LIMITS_RESULT, fetched_at=NOW), account_id="acct-0000-1111",
    )

    def fallback(codex_home, *, timeout):
        calls.append(Path(codex_home).name)
        return fresh

    monkeypatch.setattr(appserver, "probe_usage", _failing_probe)
    monkeypatch.setattr(backend, "probe_usage", fallback)
    argv = ["probe", "1"]
    if enable == "flag":
        argv.append("--backend")
    elif enable == "setting":
        assert cli.main(["config", "set", "probe.allowBackendFallback", "true"]) == 0
        capsys.readouterr()

    assert cli.main(argv) == 0
    output = capsys.readouterr().out
    if enable == "neither":
        # The default must never reach an unsupported endpoint, not even on failure.
        assert calls == []
    else:
        assert calls == ["1"]
        assert "84" in output


def test_backend_fallback_reports_a_rejected_credential(seeded_store, monkeypatch, capsys):
    from codexswap import backend

    def rejected(codex_home, *, timeout):
        raise errors.AuthExpired("fixture rejection")

    monkeypatch.setattr(appserver, "probe_usage", _failing_probe)
    monkeypatch.setattr(backend, "probe_usage", rejected)
    assert cli.main(["probe", "1", "--backend"]) == 0
    assert "re-login" in capsys.readouterr().out


def test_legacy_console_encoding_does_not_fail_a_successful_mutation(
    seeded_store, monkeypatch, capsys,
):
    """A CP949 console must not turn an applied change into exit 1."""
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="cp949", newline="\n")
    monkeypatch.setattr(cli.sys, "stdout", stream)
    monkeypatch.setattr(cli.sys, "stderr", stream)

    assert cli.main(["alias", "1", "\U0001f98a"]) == 0
    stream.flush()
    printed = buffer.getvalue().decode("cp949")
    assert "set alias for slot 1" in printed
    assert AccountStore.load().get(1).alias == "\U0001f98a"
    # The name itself cannot survive CP949; an escape is the honest rendering.
    assert r"\U0001f98a" in printed


def _model_snapshot(percent, model_percent, *, account_id):
    """A cached snapshot whose totals and named-model usage disagree."""
    from codexswap.models import PerLimitUsage, RateLimitWindow

    return replace(
        UsageSnapshot.from_api(RATE_LIMITS_RESULT, fetched_at=NOW), account_id=account_id,
        primary=RateLimitWindow(percent, 10080, int(NOW + 86400)), secondary=None,
        per_limit=(PerLimitUsage(
            limit_id="codex_bengalfox", limit_name="GPT-5.3-Codex-Spark",
            primary=RateLimitWindow(model_percent, 300, int(NOW + 86400)),
            secondary=None, plan_type="pro",
        ),),
    )


@pytest.fixture
def model_store():
    """Three enabled accounts: slot 2 has the model exhausted, slot 3 does not."""
    store = AccountStore.load()
    for slot, email in ((1, "a@example.com"), (2, "b@example.com"), (3, "c@example.com")):
        store.add_from_auth(make_auth(email=email, account_id=f"acct-{slot}",
                                      id_exp=int(NOW + 3600), access_exp=int(NOW + 86400)),
                            now=NOW)
    store.set_active(1)
    store.record_usage(1, _model_snapshot(90, 90, account_id="acct-1"))
    store.record_usage(2, _model_snapshot(5, 99, account_id="acct-2"))
    store.record_usage(3, _model_snapshot(20, 1, account_id="acct-3"))
    return store


def test_switch_model_avoids_the_account_whose_model_is_exhausted(model_store, live_auth):
    assert cli.main(["switch", "--model", "codex_bengalfox"]) == 0

    assert AccountStore.load().active_slot == 3


def test_switch_without_model_prefers_the_lowest_total(model_store, live_auth):
    assert cli.main(["switch", "--strategy", "best"]) == 0

    assert AccountStore.load().active_slot == 2


def test_switch_model_reads_the_saved_setting(model_store, live_auth):
    settings = cli.Settings.load()
    settings.set("autoswitch.model", "codex_bengalfox")
    settings.save()

    assert cli.main(["switch", "--strategy", "best"]) == 0

    assert AccountStore.load().active_slot == 3


def test_switch_model_overrides_the_saved_setting(model_store, live_auth):
    settings = cli.Settings.load()
    settings.set("autoswitch.model", "codex_bengalfox")
    settings.save()

    # An empty --model asks for the totals alone, undoing the stored preference.
    assert cli.main(["switch", "--strategy", "best", "--model", ""]) == 0

    assert AccountStore.load().active_slot == 2


def test_bare_switch_still_rotates_without_reading_usage(model_store, live_auth):
    assert cli.main(["switch"]) == 0

    assert AccountStore.load().active_slot == 2


def test_switch_model_still_honours_an_explicit_ref(model_store, live_auth):
    assert cli.main(["switch", "2", "--model", "codex_bengalfox"]) == 0

    assert AccountStore.load().active_slot == 2


def _foreign_probe(monkeypatch, account_id="acct-somebody-else"):
    """Make every probe answer for an account the registry does not know."""
    homes = []

    def probe(home, **kwargs):
        homes.append(Path(home).name)
        return replace(UsageSnapshot.from_api(RATE_LIMITS_RESULT, fetched_at=NOW),
                       account_id=account_id)

    monkeypatch.setattr(appserver, "probe_usage", probe)
    return homes


def test_reset_use_refuses_when_the_slot_answers_for_another_account(
        seeded_store, monkeypatch, capsys):
    _foreign_probe(monkeypatch)
    # offline_cli makes any call to resets.redeem fail the test outright.
    assert cli.main(["reset", "use", "--yes"]) == errors.UserError.exit_code
    captured = capsys.readouterr()
    assert "different account" in captured.err
    assert captured.out == ""


def test_reset_dry_run_refuses_the_same_way(seeded_store, monkeypatch, capsys):
    _foreign_probe(monkeypatch)

    assert cli.main(["reset", "use", "--dry-run"]) == errors.UserError.exit_code
    assert "different account" in capsys.readouterr().err


def test_reset_list_refuses_rather_than_showing_another_accounts_credits(
        seeded_store, monkeypatch, capsys):
    # reset list reuses a fresh cache, so drop it to make the command actually probe.
    seeded_store.forget_usage(1)
    _foreign_probe(monkeypatch)

    assert cli.main(["reset", "list"]) == errors.UserError.exit_code
    assert "different account" in capsys.readouterr().err


def test_list_warns_on_stderr_and_keeps_json_parseable(seeded_store, monkeypatch, capsys):
    seeded_store.forget_usage(1)
    _foreign_probe(monkeypatch)

    assert cli.main(["list", "--json"]) == 0
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["accounts"][0]["usage"] is None
    assert "different account" in captured.err


def test_a_matching_probe_produces_no_warning(seeded_store, monkeypatch, capsys):
    seeded_store.forget_usage(1)
    _foreign_probe(monkeypatch, account_id="acct-0000-1111")

    assert cli.main(["probe", "1", "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["account"]["usage"] is not None
    assert captured.err == ""


def test_a_cached_snapshot_from_another_account_is_not_shown(seeded_store, capsys):
    seeded_store.record_usage(1, replace(
        UsageSnapshot.from_api(RATE_LIMITS_RESULT, fetched_at=NOW),
        account_id="acct-somebody-else",
    ))

    assert cli.main(["list", "--no-probe", "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["accounts"][0]["usage"] is None
    assert captured.err == ""


def test_a_slot_whose_credential_is_gone_is_not_reported_healthy(swap_home, capsys):
    """The registry's identity outlives the file it was read from."""
    store = AccountStore.load()
    store.add_from_auth(make_auth(email="gone@example.com", account_id="acct-gone"))
    (paths.slot_home(1) / "auth.json").unlink()

    assert cli.main(["list", "--json", "--no-probe"]) == 0
    entry = json.loads(capsys.readouterr().out)["accounts"][0]
    assert entry["health"] == "unknown"
    assert entry["email"] == "gone@example.com", "the account is still listed"

    assert cli.main(["list", "--no-probe", "--no-color"]) == 0
    assert "stored credential could not be read" in capsys.readouterr().out


def test_an_unparseable_slot_credential_is_not_reported_healthy(swap_home, capsys):
    store = AccountStore.load()
    store.add_from_auth(make_auth(email="broken@example.com", account_id="acct-broken"))
    (paths.slot_home(1) / "auth.json").write_text("{ truncated", encoding="utf-8")

    assert cli.main(["list", "--json", "--no-probe"]) == 0
    assert json.loads(capsys.readouterr().out)["accounts"][0]["health"] == "unknown"
