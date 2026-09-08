from __future__ import annotations

import argparse
import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from conftest import make_apikey_auth, make_auth

from codexswap import appserver, auto, cli, doctor, errors, paths, resets, switcher, transfer
from codexswap.models import UsageSnapshot
from codexswap.settings import Settings
from codexswap.store import AccountStore

REAL_POPEN = subprocess.Popen


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("unexpected subprocess, probe, or redemption")

    monkeypatch.setattr(appserver, "probe_usage", forbidden)
    monkeypatch.setattr(appserver.AppServerClient, "consume_reset_credit", forbidden)
    monkeypatch.setattr(resets, "redeem", forbidden)
    monkeypatch.setattr(appserver, "find_codex_binary", forbidden)
    monkeypatch.setattr(switcher, "detect_running_codex", lambda: [])
    for name in ("run", "call", "Popen"):
        monkeypatch.setattr(subprocess, name, forbidden)


def test_add_seeds_exact_config_and_readd_preserves_it(codex_root):
    config = b'model = "recognisable-model"\r\n# seeded verbatim\r\n'
    (codex_root / "config.toml").write_bytes(config)
    paths.atomic_write_json(paths.live_auth_path(), make_auth())
    store = AccountStore.load()
    switcher.capture_current(store)
    destination = paths.slot_home(1) / "config.toml"
    assert destination.read_bytes() == config
    destination.write_text('model = "slot-only"\n')
    switcher.capture_current(store)
    assert destination.read_text() == 'model = "slot-only"\n'


def test_seed_reads_current_home_and_custom_store_root(tmp_path, monkeypatch):
    second = tmp_path / "second-live"
    second.mkdir()
    (second / "config.toml").write_text("# second live\n")
    store = AccountStore(tmp_path / "explicit-store")
    monkeypatch.setenv("CODEX_HOME", str(second))
    store.add_from_auth(make_auth())
    assert (tmp_path / "explicit-store/homes/1/config.toml").read_text() == "# second live\n"


def test_absent_config_is_not_created():
    AccountStore.load().add_from_auth(make_auth())
    assert not (paths.slot_home(1) / "config.toml").exists()


def test_import_seeds_and_force_import_keeps_config(codex_root, tmp_path):
    (codex_root / "config.toml").write_text("# live\n")
    envelope = tmp_path / "export.json"
    paths.atomic_write_json(envelope, {"format": "codexswap-export", "version": 1,
                                      "accounts": [{"slot": 1, "auth": make_auth()}]})
    store = AccountStore.load()
    transfer.import_accounts(store, envelope)
    config = paths.slot_home(1) / "config.toml"
    assert config.read_text() == "# live\n"
    config.write_text("# private MCP credentials\n")
    transfer.import_accounts(store, envelope, force=True)
    assert config.read_text() == "# private MCP credentials\n"


def test_sync_all_conflicts_and_force(codex_root, capsys):
    store = AccountStore.load()
    store.add_from_auth(make_auth())
    store.add_from_auth(make_auth(email="second@example.com", account_id="second"))
    config = paths.slot_home(1) / "config.toml"
    config.write_text("# private\n")
    (codex_root / "config.toml").write_text("# live\n")
    assert cli.main(["sync-config"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "slot 1: skipped (different config.toml; use --force to overwrite)", "slot 2: copied"]
    assert config.read_text() == "# private\n"
    assert cli.main(["sync-config", "--force"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "slot 1: overwritten", "slot 2: unchanged (already identical)"]
    assert config.read_text() == "# live\n"


@pytest.mark.parametrize("use_directory", [False, True])
def test_sync_from_file_or_directory_one_alias(tmp_path, capsys, use_directory):
    store = AccountStore.load()
    store.add_from_auth(make_auth(), alias="main")
    source = tmp_path / "config.toml"
    source.write_text("# alternate\n")
    assert cli.main(["sync-config", "main", "--from",
                     str(source.parent if use_directory else source)]) == 0
    assert capsys.readouterr().out == "slot 1: copied\n"
    assert (paths.slot_home(1) / "config.toml").read_text() == "# alternate\n"


def test_sync_missing_source_is_user_error(capsys):
    assert cli.main(["sync-config"]) == 2
    assert "Cannot read source" in capsys.readouterr().err


def test_add_token_stdin_shape_mode_and_config(monkeypatch, codex_root, capsys):
    (codex_root / "config.toml").write_text("# token slot\n")
    monkeypatch.setattr(sys, "stdin", io.StringIO("opaque-test-secret\nunused line\n"))
    assert cli.main(["add-token", "-", "--slot", "7", "--alias", "service"]) == 0
    assert sys.stdin.readline() == "unused line\n"
    assert capsys.readouterr().out == "saved slot 7 (service (api-key-7@token.local))\n"
    assert paths.read_json(paths.slot_auth_path(7)) == make_apikey_auth("opaque-test-secret")
    assert (paths.slot_home(7) / "config.toml").read_text() == "# token slot\n"
    store = AccountStore.load()
    assert store.active_slot is None
    assert store.get(7).identity.plan_type == "api key"
    if os.name != "nt":
        assert stat.S_IMODE(paths.slot_auth_path(7).stat().st_mode) == 0o600


def test_add_token_hidden_prompt(monkeypatch, capsys):
    stream = io.StringIO()
    monkeypatch.setattr(stream, "isatty", lambda: True)
    monkeypatch.setattr(sys, "stdin", stream)
    prompt = Mock(return_value="opaque-prompt-secret")
    monkeypatch.setattr(cli.getpass, "getpass", prompt)
    assert cli.main(["add-token", "--email", "service@example.com"]) == 0
    prompt.assert_called_once()
    assert "service@example.com" in capsys.readouterr().out


@pytest.mark.parametrize("token", ["", " ", "\t\r\n"])
def test_add_token_empty_rejected(token):
    with pytest.raises(errors.UserError, match="must not be empty"):
        AccountStore.load().add_token(token)


def test_add_token_non_tty_requires_dash(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("secret\n"))
    assert cli.main(["add-token"]) == 2
    assert "use add-token -" in capsys.readouterr().err
    assert not paths.accounts_path().exists()


def test_add_token_occupied_slot_and_duplicate_email(capsys):
    store = AccountStore.load()
    store.add_token("key-first", slot=3, email="service@example.com")
    original = paths.slot_auth_path(3).read_bytes()
    assert cli.main(["add-token", "key-second", "--slot", "3"]) == 2
    assert cli.main(["add-token", "key-second", "--email", "service@example.com"]) == 2
    assert "key-second" not in str(capsys.readouterr())
    assert paths.slot_auth_path(3).read_bytes() == original


@pytest.mark.parametrize("extra", [["--slot", "invalid"], ["unexpected"], ["--bad-option"]])
def test_add_token_parser_never_echoes_secret(extra, capsys):
    key = "opaque-parser-secret"
    assert cli.main(["add-token", key] + extra) == 2
    assert key not in str(capsys.readouterr())


def test_add_token_rejects_secret_in_metadata(capsys):
    assert cli.main(["add-token", "opaque-secret", "--alias", "opaque-secret"]) == 2
    assert "opaque-secret" not in str(capsys.readouterr())


@pytest.mark.parametrize("command", [
    ["list"], ["list", "--json"], ["list", "--no-probe", "--json"],
    ["list", "--token-status"], ["status"], ["status", "--json"],
    ["probe"], ["probe", "1", "--json"], ["alias"],
])
def test_api_display_preserves_label_without_probe_or_leaks(command, capsys):
    store = AccountStore.load()
    store.add_token("opaque-display-secret", alias="service")
    store.set_active(1)
    store.record_usage(1, UsageSnapshot.from_api(
        {"rateLimits": {"primary": {"usedPercent": 99}}}, fetched_at=cli.time.time()))
    assert cli.main(command) == 0
    output = capsys.readouterr()
    assert "opaque-display-secret" not in output.out + output.err
    assert "api-key-1@token.local" in output.out
    if command[0] != "alias":
        assert "api key" in output.out
        if "--json" in command:
            document = json.loads(output.out)
            entry = document.get("account", document.get("accounts", [None])[0])
            assert entry["usage"] is None and entry["health"] == "ok"
        else:
            assert "usage unavailable" in output.out
            assert "re-login" not in output.out and "stale" not in output.out


def test_api_export_import_preserves_email(tmp_path):
    store = AccountStore.load()
    store.add_token("opaque-export-secret", email="service@example.com")
    archive = tmp_path / "accounts-export.json"
    transfer.export_accounts(store, archive)
    store.remove(1)
    transfer.import_accounts(store, archive)
    assert store.get(1).identity.email == "service@example.com"
    assert store.get(1).identity.plan_type == "api key"


@pytest.mark.parametrize("strategy", ["best", "next-available"])
@pytest.mark.parametrize("ordinary", [20, 99, None, "failure", "disabled"])
def test_auto_api_is_last_resort(strategy, ordinary, monkeypatch, capsys):
    store = AccountStore.load()
    store.add_from_auth(make_auth())
    store.add_token("opaque-auto-secret")
    store.add_from_auth(make_auth(email="other@example.com", account_id="other"))
    store.set_active(1)
    if ordinary == "disabled":
        store.set_disabled(3, True)
    settings = Settings.load()
    settings.set("autoswitch.strategy", strategy)
    settings.set("reset.policy", "never")
    settings.save()

    def probe(home, **kwargs):
        slot = int(home.name)
        assert slot != 2
        if slot == 3 and ordinary == "failure":
            raise errors.AppServerError("unavailable")
        percent = 99 if slot == 1 else ordinary
        payload = {} if percent is None else {"rateLimits": {"primary": {"usedPercent": percent}}}
        return UsageSnapshot.from_api(payload, fetched_at=cli.time.time())

    monkeypatch.setattr(appserver, "probe_usage", probe)
    activate = Mock()
    monkeypatch.setattr(switcher, "activate", activate)
    assert cli.main(["auto", "--once"]) == 0
    expected = 3 if ordinary in (20, None) else 2
    assert activate.call_args.args[1].slot == expected
    assert "opaque-auto-secret" not in str(capsys.readouterr())


def test_auto_only_api_and_dry_run_never_probe_or_redeem(capsys):
    store = AccountStore.load()
    store.add_token("opaque-auto-secret")
    assert cli.main(["auto", "--once", "--dry-run"]) == 0
    assert "[dry-run]" in capsys.readouterr().out
    assert AccountStore.load().active_slot is None
    store.set_active(1)
    assert cli.main(["auto", "--once"]) == 0
    assert auto.AutoState.load().unhealthy.get(1) == 0


@pytest.fixture
def healthy_doctor(monkeypatch):
    monkeypatch.setattr(appserver, "find_codex_binary", lambda: "/fake/codex")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "codex-cli fake\n", ""))
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    factory = Mock(return_value=client)
    monkeypatch.setattr(appserver, "AppServerClient", factory)
    return factory, client


def test_doctor_json_and_isolated_initialize_only(healthy_doctor, capsys):
    AccountStore.load().add_token("opaque-doctor-secret")
    assert cli.main(["doctor", "--json"]) == 0
    output = capsys.readouterr()
    document = json.loads(output.out)
    assert output.err == ""
    assert document["checks"]
    assert "opaque-doctor-secret" not in output.out
    factory, client = healthy_doctor
    home = factory.call_args.args[0]
    assert home != paths.codex_home() and home != paths.slot_home(1)
    assert not home.exists()
    assert factory.call_args.kwargs["timeout"] == 5
    client.__enter__.assert_called_once()
    client.__exit__.assert_called_once()
    client.request.assert_not_called()
    client.read_rate_limits.assert_not_called()
    client.consume_reset_credit.assert_not_called()


def test_doctor_reports_bad_registry_settings_auth_without_mutation(healthy_doctor, capsys):
    registry = paths.accounts_path()
    registry.write_text("broken-json")
    paths.settings_path().write_text('{"probe": {"timeoutSeconds": "bad", "extra": true}}')
    paths.live_auth_path().write_text("[]")
    assert cli.main(["doctor", "--json"]) == 1
    checks = {item["name"]: item for item in json.loads(capsys.readouterr().out)["checks"]}
    assert checks["accounts"]["status"] == "fail"
    assert checks["live.auth"]["status"] == "fail"
    assert checks["settings"]["status"] == "fail"
    assert checks["settings.unknown"]["detail"] == "unrecognised keys: probe.extra"
    assert registry.read_text() == "broken-json"
    assert not list(registry.parent.glob("*.corrupt-*"))


@pytest.mark.parametrize("auth", [None, "[]", '{"auth_mode": "apikey", "OPENAI_API_KEY": " "}'])
def test_doctor_bad_slot_auth_fails(healthy_doctor, auth):
    store = AccountStore.load()
    store.add_token("opaque-doctor-secret")
    path = paths.slot_auth_path(1)
    if auth is None:
        path.unlink()
    else:
        path.write_text(auth)
    checks = {item["name"]: item for item in doctor.collect_checks()["checks"]}
    assert checks["slot.1.auth"]["status"] == "fail"


def test_doctor_matches_live_api_and_redacts_external_output(healthy_doctor, monkeypatch, capsys):
    key = "opaque-external-output"
    AccountStore.load().add_token(key)
    paths.atomic_write_json(paths.live_auth_path(), make_apikey_auth(key))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, key, ""))
    assert cli.main(["doctor"]) == 0
    output = capsys.readouterr().out
    assert key not in output
    assert "matches slot 1" in output
    assert "[redacted]" in output


def test_doctor_missing_binary_fails(monkeypatch, capsys):
    def missing():
        raise errors.CodexBinaryNotFound("unavailable")

    monkeypatch.setattr(appserver, "find_codex_binary", missing)
    assert cli.main(["doctor", "--json"]) == 1
    checks = json.loads(capsys.readouterr().out)["checks"]
    assert {item["name"] for item in checks if item["status"] == "fail"} == {"codex.binary", "app-server"}


def test_doctor_handshake_timeout(healthy_doctor, capsys):
    healthy_doctor[1].__enter__.side_effect = errors.AppServerTimeout("opaque-do-not-print")
    assert cli.main(["doctor", "--json"]) == 1
    output = capsys.readouterr().out
    assert "opaque-do-not-print" not in output
    assert "initialize failed" in output


def test_doctor_busy_lock(monkeypatch):
    paths.lock_path().write_bytes(b"\0")

    def busy(handle):
        raise PermissionError(13, "locked")

    monkeypatch.setattr(doctor.locking, "_acquire", busy)
    assert doctor._lock_check(paths.lock_path())["status"] == "warn"


def test_doctor_real_protocol_sends_only_initialize(tmp_path, monkeypatch):
    protocol = tmp_path / "protocol.jsonl"
    fake = tmp_path / "initialize_only.py"
    fake.write_text(
        "import json, os, sys\n"
        "with open(os.environ['DOCTOR_PROTOCOL'], 'w') as log:\n"
        "    for line in sys.stdin:\n"
        "        request = json.loads(line)\n"
        "        log.write(json.dumps(request) + '\\n'); log.flush()\n"
        "        if request['method'] == 'initialize':\n"
        "            print(json.dumps({'id': request['id'], 'result': {}}), flush=True)\n"
        "        elif request['method'] != 'initialized':\n"
        "            raise SystemExit(2)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCTOR_PROTOCOL", str(protocol))
    monkeypatch.setattr(subprocess, "Popen", REAL_POPEN)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "codex fake", ""))
    monkeypatch.setattr(appserver, "find_codex_binary", lambda: sys.executable)
    factory = appserver.AppServerClient
    monkeypatch.setattr(appserver, "AppServerClient", lambda home, **kwargs: factory(
        home, timeout=kwargs["timeout"], codex_bin=[sys.executable, str(fake)]))
    report = doctor.collect_checks()
    assert next(check for check in report["checks"] if check["name"] == "app-server")["status"] == "ok"
    assert [json.loads(line)["method"] for line in protocol.read_text().splitlines()] == [
        "initialize", "initialized"]


@pytest.mark.parametrize("contents", ["[]", "null", "broken"])
def test_doctor_invalid_settings(healthy_doctor, contents):
    paths.settings_path().write_text(contents)
    assert doctor._settings_checks(paths.codexswap_home())[0]["status"] == "fail"


def test_doctor_permissions_processes_and_disk(healthy_doctor, monkeypatch):
    monkeypatch.setattr(os, "access", lambda *args: False)
    monkeypatch.setattr(switcher, "detect_running_codex", lambda: [
        switcher.ProcessInfo(123, "codex", "secret command line")])
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda path: argparse.Namespace(free=1024))
    checks = {check["name"]: check for check in doctor.collect_checks()["checks"]}
    assert checks["CODEX_HOME"]["status"] == "fail"
    assert checks["CODEXSWAP_HOME"]["status"] == "fail"
    assert checks["processes"]["detail"] == "running Codex PIDs: 123"
    assert checks["disk"]["status"] == "warn"


@pytest.mark.parametrize("strategy", ["best", "next-available"])
def test_strategy_switch_prioritises_ordinary_unknown_usage(strategy, monkeypatch):
    from codexswap import strategy as selection

    store = AccountStore.load()
    token = store.add_token("opaque-priority-secret")
    ordinary = store.add_from_auth(make_auth())
    target = switcher.pick_target(
        [selection.Candidate(token, None), selection.Candidate(ordinary, None)],
        current_slot=None, strategy=strategy, threshold=80, hysteresis=10)
    assert target is ordinary


def test_slot_move_and_swap_preserve_account_configs(codex_root):
    (codex_root / "config.toml").write_text("# one\n")
    store = AccountStore.load()
    store.add_token("opaque-first")
    (codex_root / "config.toml").write_text("# two\n")
    store.add_token("opaque-second")
    store.swap_slots(1, 2)
    store.move_slot(2, 3)
    assert (paths.slot_home(1) / "config.toml").read_text() == "# two\n"
    assert (paths.slot_home(3) / "config.toml").read_text() == "# one\n"


@pytest.mark.parametrize("interval", ["4", "3601", "0", "-1", "nope"])
def test_watch_bad_interval(interval, capsys):
    assert cli.main(["watch", "--interval", interval]) == 2


@pytest.mark.parametrize("tty,color,clears", [(True, "always", True), (True, "never", False), (False, "always", False)])
def test_watch_two_frames_interrupt_and_failure(monkeypatch, tty, color, clears):
    stream = io.StringIO()
    monkeypatch.setattr(stream, "isatty", lambda: tty)
    monkeypatch.setattr(sys, "stdout", stream)
    settings = Settings.load()
    settings.set("ui.color", color)
    settings.save()
    show = Mock(side_effect=[errors.AppServerError("secret failure"), None])
    monkeypatch.setattr(cli, "_show_accounts", show)
    sleep = Mock(side_effect=[None, KeyboardInterrupt])
    monkeypatch.setattr(cli.time, "sleep", sleep)
    assert cli.main(["watch", "--interval", "5"]) == 0
    assert show.call_count == 2
    assert [call.args[0] for call in sleep.call_args_list] == [5, 5]
    output = stream.getvalue()
    assert ("\033[2J\033[H" in output) is clears
    assert "secret failure" not in output
    assert output.endswith("\n")
    if not clears:
        assert output.count("--- ") == 2


@pytest.mark.parametrize("prefix,package,method", [
    ("/home/a/.local/share/uv/tools/codexswap", "/checkout/src/codexswap/__init__.py", "uv"),
    ("C:\\Users\\a\\AppData\\Roaming\\uv\\tools\\codexswap", "/checkout/__init__.py", "uv"),
    ("/home/a/.local/share/pipx/venvs/codexswap", "/checkout/__init__.py", "pipx"),
    ("C:\\Users\\a\\pipx\\venvs\\codexswap", "/checkout/__init__.py", "pipx"),
    ("/python", "/python/lib/site-packages/codexswap/__init__.py", "pip"),
    ("/python", "/checkout/src/codexswap/__init__.py", None),
])
def test_upgrade_detection(monkeypatch, prefix, package, method):
    import codexswap

    monkeypatch.setattr(sys, "prefix", prefix)
    monkeypatch.setattr(sys, "argv", ["codexswap"])
    monkeypatch.setattr(codexswap, "__file__", package)
    assert cli._upgrade_method() == method


@pytest.mark.parametrize("answer", ["n\n", "\n", "", "no\n"])
def test_upgrade_declines_without_spawning(monkeypatch, capsys, answer):
    monkeypatch.setattr(cli, "_upgrade_method", lambda: "uv")
    monkeypatch.setattr(sys, "stdin", io.StringIO(answer))
    assert cli.main(["upgrade"]) == 0
    output = capsys.readouterr().out
    assert "will run: uv tool upgrade codexswap" in output
    assert "upgrade cancelled" in output


@pytest.mark.parametrize("method", ["uv", "pipx", "pip"])
@pytest.mark.parametrize("yes", [False, True])
def test_upgrade_affirmative_and_json(monkeypatch, capsys, method, yes):
    monkeypatch.setattr(cli, "_upgrade_method", lambda: method)
    monkeypatch.setattr(sys, "stdin", io.StringIO("yes\n"))
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(subprocess, "run", run)
    assert cli.main(["upgrade", "--json"] + (["--yes"] if yes else [])) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["method"] == method and report["executed"] is True
    assert run.call_args.args[0] == report["command"]
    assert run.call_args.kwargs["stdout"] is sys.stderr
    assert "will run:" in captured.err


def test_upgrade_unknown_even_with_yes_never_runs(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_upgrade_method", lambda: None)
    assert cli.main(["upgrade", "--yes", "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert len(report["candidates"]) == 3 and report["executed"] is False


def test_upgrade_failure_exit_status(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_upgrade_method", lambda: "pipx")
    monkeypatch.setattr(subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 7)))
    assert cli.main(["upgrade", "--yes", "--json"]) == 7
    assert json.loads(capsys.readouterr().out)["returnCode"] == 7


def test_new_parser_defaults():
    args = cli.build_parser().parse_args(["watch"])
    assert args.interval == 30
    args = cli.build_parser().parse_args(["sync-config", "main", "--from", "source", "--force"])
    assert args.ref == "main" and args.source == Path("source") and args.force
    assert isinstance(cli.build_parser().parse_args(["doctor", "--json"]), argparse.Namespace)


def test_doctor_reports_a_slot_holding_another_account(healthy_doctor):
    store = AccountStore.load()
    store.add_from_auth(make_auth(email="a@example.com", account_id="acct-1"))
    # Overwrite the slot credential with a different account, as a hand copy would.
    paths.atomic_write_json(paths.slot_auth_path(1),
                            make_auth(email="b@example.com", account_id="acct-2"))

    checks = {item["name"]: item for item in doctor.collect_checks()["checks"]}

    assert checks["slot.1.identity"]["status"] == "fail"
    assert "different account" in checks["slot.1.identity"]["detail"]
    assert "account id" in checks["slot.1.identity"]["detail"]


def test_doctor_accepts_a_slot_holding_its_own_account(healthy_doctor):
    AccountStore.load().add_from_auth(make_auth(email="a@example.com", account_id="acct-1"))

    checks = {item["name"]: item for item in doctor.collect_checks()["checks"]}

    assert checks["slot.1.identity"]["status"] == "ok"
    assert checks["slot.1.identity"]["detail"] == "matches the registered account"


def test_doctor_reports_an_email_change_on_the_same_slot(healthy_doctor):
    store = AccountStore.load()
    store.add_from_auth(make_auth(email="a@example.com", account_id=""))
    paths.atomic_write_json(paths.slot_auth_path(1),
                            make_auth(email="b@example.com", account_id=""))

    checks = {item["name"]: item for item in doctor.collect_checks()["checks"]}

    assert checks["slot.1.identity"]["status"] == "fail"
    assert "email differs" in checks["slot.1.identity"]["detail"]


def test_doctor_has_no_identity_opinion_about_an_api_key_slot(healthy_doctor):
    AccountStore.load().add_token("opaque-doctor-secret")

    checks = {item["name"]: item for item in doctor.collect_checks()["checks"]}

    assert checks["slot.1.identity"]["status"] == "ok"
    assert "api key" in checks["slot.1.identity"]["detail"]
