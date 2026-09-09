"""The stash that keeps a switch from destroying a login nothing owns."""

from __future__ import annotations

import json
import os
import stat

import pytest
from conftest import make_apikey_auth, make_auth

from codexswap import cli, errors, paths, unclaimed
from codexswap.store import AccountStore

REASON = "live login belonged to no registered account"


def test_stash_records_identity_and_never_exposes_the_credential(swap_home):
    auth = make_auth(email="stranger@example.com", account_id="acct-stray")
    entry_id = unclaimed.stash(auth, reason=REASON)

    listed = unclaimed.entries()
    assert [item.id for item in listed] == [entry_id]
    entry = listed[0]
    assert entry.email == "stranger@example.com"
    assert entry.account_id == "acct-stray"
    assert entry.plan_type == "pro"
    assert entry.auth_mode == "chatgpt"
    assert entry.reason == REASON
    assert entry.stashed_at.endswith("Z")
    assert entry.label() == "stranger@example.com"
    # The dataclass a caller renders must not carry the token at all.
    assert "refresh_token" not in repr(entry)
    assert auth["tokens"]["refresh_token"] not in repr(entry)
    assert (swap_home / "unclaimed" / (entry_id + ".json")).is_file()


def test_the_same_credential_twice_is_one_rescue(swap_home):
    auth = make_auth()
    first = unclaimed.stash(auth, reason=REASON)
    second = unclaimed.stash(json.loads(json.dumps(auth)), reason="a different reason")
    assert first == second
    assert len(unclaimed.entries()) == 1
    # The first reason is the one kept; the entry is not rewritten.
    assert unclaimed.entries()[0].reason == REASON


def test_a_different_credential_gets_its_own_entry(swap_home):
    unclaimed.stash(make_auth(email="one@example.com"), reason=REASON)
    unclaimed.stash(make_auth(email="two@example.com", account_id="acct-two"), reason=REASON)
    assert sorted(item.email for item in unclaimed.entries()) == [
        "one@example.com", "two@example.com"]


def test_the_credential_comes_back_verbatim(swap_home):
    auth = make_auth()
    entry_id = unclaimed.stash(auth, reason=REASON)
    assert unclaimed.credential(entry_id) == auth


def test_discard_removes_the_entry_and_then_says_what_is_left(swap_home):
    keep = unclaimed.stash(make_auth(email="keep@example.com"), reason=REASON)
    drop = unclaimed.stash(make_auth(email="drop@example.com", account_id="acct-b"),
                           reason=REASON)
    unclaimed.discard(drop)
    assert [item.id for item in unclaimed.entries()] == [keep]
    with pytest.raises(errors.UserError) as excinfo:
        unclaimed.resolve(drop)
    assert keep in str(excinfo.value)


def test_an_id_cannot_name_a_file_outside_the_stash(swap_home):
    unclaimed.stash(make_auth(), reason=REASON)
    for hostile in ("../accounts", "..", ".", "", "sub/entry", "C:/Windows/system"):
        with pytest.raises(errors.UserError):
            unclaimed.resolve(hostile)


def test_an_empty_credential_is_refused(swap_home):
    for value in ({}, None, "auth", []):
        with pytest.raises(errors.UserError):
            unclaimed.stash(value, reason=REASON)
    assert unclaimed.entries() == []


def test_an_api_key_stashes_without_an_email(swap_home):
    entry_id = unclaimed.stash(make_apikey_auth("sk-test-stash"), reason=REASON)
    entry = unclaimed.resolve(entry_id)
    assert entry.auth_mode == "apikey"
    assert entry.email is None
    assert entry.label() == "api key"
    assert unclaimed.credential(entry_id)["OPENAI_API_KEY"] == "sk-test-stash"


def test_a_credential_with_no_readable_identity_still_stashes(swap_home):
    entry_id = unclaimed.stash({"tokens": {"access_token": "opaque"}}, reason=REASON)
    entry = unclaimed.resolve(entry_id)
    assert entry.email is None and entry.account_id is None
    assert entry.label() == "unidentified login"
    assert unclaimed.credential(entry_id) == {"tokens": {"access_token": "opaque"}}


def test_junk_in_the_directory_is_skipped_rather_than_fatal(swap_home):
    good = unclaimed.stash(make_auth(), reason=REASON)
    home = swap_home / "unclaimed"
    (home / "not-json.json").write_text("{ this is not json", encoding="utf-8")
    (home / "no-auth.json").write_text(json.dumps({"reason": "x"}), encoding="utf-8")
    (home / "ignored.txt").write_text("nothing", encoding="utf-8")
    assert [item.id for item in unclaimed.entries()] == [good]


def test_a_missing_directory_lists_nothing(swap_home):
    assert unclaimed.entries() == []
    assert not (swap_home / "unclaimed").exists()


def test_entries_are_ordered_oldest_first(swap_home, monkeypatch):
    """The id leads with the timestamp, so listing in name order is chronological."""
    stamps = iter(["2026-09-09T23:59:59Z", "2026-09-10T00:00:00Z"])
    monkeypatch.setattr(paths, "iso_now", lambda: next(stamps))
    older = unclaimed.stash(make_auth(email="older@example.com"), reason=REASON)
    newer = unclaimed.stash(make_auth(email="newer@example.com", account_id="acct-2"),
                            reason=REASON)
    assert older.startswith("20260909T235959Z-")
    assert newer.startswith("20260910T000000Z-")
    listed = unclaimed.entries()
    assert [item.id for item in listed] == [older, newer]
    assert [item.stashed_at for item in listed] == [
        "2026-09-09T23:59:59Z", "2026-09-10T00:00:00Z"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes; Windows chmod is a no-op")
def test_the_stash_is_not_world_readable(swap_home):
    entry_id = unclaimed.stash(make_auth(), reason=REASON)
    entry_path = swap_home / "unclaimed" / (entry_id + ".json")
    assert stat.S_IMODE(entry_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((swap_home / "unclaimed").stat().st_mode) == 0o700


def test_the_fingerprint_reveals_nothing_and_separates_credentials():
    auth = make_auth()
    mark = unclaimed.fingerprint(auth)
    assert len(mark) == 64 and all(char in "0123456789abcdef" for char in mark)
    assert auth["tokens"]["refresh_token"] not in mark
    assert mark != unclaimed.fingerprint(make_auth(email="other@example.com"))
    # Key order is not part of the credential.
    assert mark == unclaimed.fingerprint(dict(reversed(list(auth.items()))))


# --- the `unclaimed` command ---------------------------------------------------


def _stash_one(email="rescued@example.com", account_id="acct-rescued"):
    return unclaimed.stash(make_auth(email=email, account_id=account_id), reason=REASON)


def test_command_says_so_when_nothing_was_rescued(swap_home, capsys):
    assert cli.main(["unclaimed"]) == 0
    assert capsys.readouterr().out.strip() == "no rescued credentials"


def test_command_lists_the_entry_without_printing_the_credential(swap_home, capsys):
    auth = make_auth(email="rescued@example.com", account_id="acct-rescued")
    entry_id = unclaimed.stash(auth, reason=REASON)
    assert cli.main(["unclaimed"]) == 0
    out = capsys.readouterr().out
    assert entry_id in out
    assert "rescued@example.com" in out
    assert "--claim" in out
    for secret in (auth["tokens"]["refresh_token"], auth["tokens"]["access_token"],
                   auth["tokens"]["id_token"]):
        assert secret not in out


def test_command_json_shape(swap_home, capsys):
    entry_id = _stash_one()
    assert cli.main(["unclaimed", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document == {"unclaimed": [{
        "id": entry_id, "stashedAt": unclaimed.resolve(entry_id).stashed_at,
        "reason": REASON, "email": "rescued@example.com",
        "accountId": "acct-rescued", "planType": "pro", "authMode": "chatgpt"}]}


def test_claim_registers_the_account_and_clears_the_stash(swap_home, capsys):
    entry_id = _stash_one()
    assert cli.main(["unclaimed", "--claim", entry_id]) == 0
    assert "claimed" in capsys.readouterr().out
    store = AccountStore.load()
    account = store.find_by_email("rescued@example.com")
    assert account is not None
    stored = json.loads((paths.slot_home(account.slot) / "auth.json").read_text("utf-8"))
    assert stored["tokens"]["account_id"] == "acct-rescued"
    assert unclaimed.entries() == []


def test_claim_honours_slot_and_alias(swap_home, capsys):
    entry_id = _stash_one()
    assert cli.main(["unclaimed", "--claim", entry_id, "--slot", "4",
                     "--alias", "work"]) == 0
    account = AccountStore.load().get(4)
    assert account.alias == "work"


def test_claim_json_reports_the_slot(swap_home, capsys):
    entry_id = _stash_one()
    assert cli.main(["unclaimed", "--claim", entry_id, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "claimed": entry_id, "slot": 1, "email": "rescued@example.com"}


def test_purge_drops_the_entry(swap_home, capsys):
    entry_id = _stash_one()
    assert cli.main(["unclaimed", "--purge", entry_id]) == 0
    assert entry_id in capsys.readouterr().out
    assert unclaimed.entries() == []
    assert AccountStore.load().accounts == {}


def test_a_failed_claim_keeps_the_rescue_copy(swap_home, capsys):
    """If the registry refuses the credential, the only copy must still exist."""
    entry_id = unclaimed.stash({"auth_mode": "chatgpt", "tokens": {"id_token": "nope"}},
                               reason=REASON)
    # AuthFileInvalid, exit 1: the payload could not authenticate anything.
    assert cli.main(["unclaimed", "--claim", entry_id]) == 1
    assert "no usable credential" in capsys.readouterr().err
    assert [item.id for item in unclaimed.entries()] == [entry_id]
    assert AccountStore.load().accounts == {}


def test_the_flags_that_cannot_be_combined(swap_home, capsys):
    entry_id = _stash_one()
    assert cli.main(["unclaimed", "--claim", entry_id, "--purge", entry_id]) == 2
    assert "not both" in capsys.readouterr().err
    assert cli.main(["unclaimed", "--slot", "3"]) == 2
    assert "apply to --claim" in capsys.readouterr().err
    assert cli.main(["unclaimed", "--alias", "x"]) == 2
    assert "apply to --claim" in capsys.readouterr().err
    # Nothing above may have touched the stash.
    assert [item.id for item in unclaimed.entries()] == [entry_id]


def test_an_unknown_id_names_what_is_actually_stashed(swap_home, capsys):
    entry_id = _stash_one()
    for flag in ("--claim", "--purge"):
        assert cli.main(["unclaimed", flag, "missing"]) == 2
        assert entry_id in capsys.readouterr().err


def _doctor_checks(capsys):
    """Doctor's own exit code depends on whether Codex is installed, which is not
    what these assertions are about; the report is printed either way."""
    cli.main(["doctor", "--json"])
    return {check["name"]: check for check in json.loads(capsys.readouterr().out)["checks"]}


def test_doctor_reports_a_rescued_credential(swap_home, capsys):
    quiet = _doctor_checks(capsys)
    assert quiet["unclaimed"]["status"] == "ok"

    entry_id = _stash_one()
    loud = _doctor_checks(capsys)
    assert loud["unclaimed"]["status"] == "warn"
    assert entry_id in loud["unclaimed"]["detail"]
    assert "codexswap unclaimed" in loud["unclaimed"]["detail"]
