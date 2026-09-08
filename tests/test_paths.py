"""Tests for path resolution and the atomic write helpers."""

from __future__ import annotations

import json
import os
import time

import pytest

from codexswap import errors, paths


def test_home_comes_from_environment(swap_home, codex_root):
    assert paths.codexswap_home() == swap_home
    assert paths.codex_home() == codex_root
    assert paths.live_auth_path() == codex_root / "auth.json"


def test_home_override_beats_environment(tmp_path):
    override = tmp_path / "override"
    paths.set_home_override(override)
    try:
        assert paths.codexswap_home() == override
    finally:
        paths.clear_home_override()
    assert paths.codexswap_home() != override


def test_derived_paths_live_under_root(swap_home):
    assert paths.accounts_path() == swap_home / "accounts.json"
    assert paths.settings_path() == swap_home / "settings.json"
    assert paths.state_path() == swap_home / "state.json"
    assert paths.mappings_path() == swap_home / "mappings.json"
    assert paths.usage_cache_path() == swap_home / "usage-cache.json"
    assert paths.slot_home(3) == swap_home / "homes" / "3"
    assert paths.slot_auth_path(3) == swap_home / "homes" / "3" / "auth.json"


def test_atomic_write_json_creates_and_replaces(tmp_path):
    target = tmp_path / "nested" / "data.json"
    paths.ensure_dir(target.parent)
    paths.atomic_write_json(target, {"a": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}

    paths.atomic_write_json(target, {"a": 2, "b": [1, 2]})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2, "b": [1, 2]}


def test_atomic_write_leaves_no_temp_files(tmp_path):
    # Use a dedicated subdirectory: the autouse fixture puts the two homes in tmp_path.
    workdir = tmp_path / "atomic"
    workdir.mkdir()
    target = workdir / "data.json"
    paths.atomic_write_json(target, {"x": 1})
    assert [p.name for p in workdir.iterdir()] == ["data.json"]


def test_atomic_write_text_roundtrip(tmp_path):
    target = tmp_path / "note.txt"
    paths.atomic_write_text(target, "hello\nworld\n")
    assert target.read_text(encoding="utf-8") == "hello\nworld\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
def test_atomic_write_json_uses_0600(tmp_path):
    target = tmp_path / "secret.json"
    paths.atomic_write_json(target, {"token": "x"})
    assert oct(target.stat().st_mode & 0o777) == "0o600"


def test_read_json_missing_returns_default(tmp_path):
    assert paths.read_json(tmp_path / "nope.json", default={"d": 1}) == {"d": 1}


def test_read_json_tolerant_swallows_corruption(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert paths.read_json_tolerant(bad, {"fallback": True}) == {"fallback": True}


def test_read_json_raises_on_corrupt_auth(tmp_path):
    bad = tmp_path / "auth.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(errors.AuthFileInvalid):
        paths.read_json(bad)


def test_quarantine_corrupt_moves_the_file(tmp_path):
    bad = tmp_path / "accounts.json"
    bad.write_text("garbage", encoding="utf-8")
    moved = paths.quarantine_corrupt(bad)
    assert not bad.exists()
    assert moved.exists()
    assert moved.name.startswith("accounts.json.corrupt-")


def test_secure_chmod_never_raises(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("x", encoding="utf-8")
    paths.secure_chmod(target, 0o600)
    paths.secure_chmod(tmp_path / "missing.txt", 0o600)


def test_iso_now_is_utc_zulu():
    value = paths.iso_now()
    assert value.endswith("Z")
    assert "T" in value
    assert len(value) >= len("2026-09-09T03:00:00Z")


def test_ensure_dir_is_idempotent(tmp_path):
    target = tmp_path / "a" / "b" / "c"
    assert paths.ensure_dir(target) == target
    assert paths.ensure_dir(target) == target
    assert target.is_dir()


def test_atomic_write_survives_rapid_rewrites(tmp_path):
    target = tmp_path / "hot.json"
    for i in range(25):
        paths.atomic_write_json(target, {"i": i, "t": time.time()})
    assert json.loads(target.read_text(encoding="utf-8"))["i"] == 24
