from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from codexswap import mappings


def test_set_mapping_then_lookup_exact_directory(tmp_path):
    directory = tmp_path / "project"

    mappings.set_mapping(directory, 2)

    assert mappings.lookup(directory) == 2


def test_lookup_uses_nearest_mapped_ancestor(tmp_path):
    project = tmp_path / "project"
    deeper = project / "packages" / "worker"
    mappings.set_mapping(project, 1)
    mappings.set_mapping(deeper, 3)

    assert mappings.lookup(project / "src" / "nested") == 1
    assert mappings.lookup(deeper / "src" / "nested") == 3
    assert mappings.lookup(deeper) == 3
    assert mappings.lookup(project / "packages" / "other") == 1


def test_lookup_returns_none_for_unrelated_directory(tmp_path):
    mappings.set_mapping(tmp_path / "project", 1)

    assert mappings.lookup(tmp_path / "unrelated") is None
    assert mappings.lookup(tmp_path / "project-sibling") is None
    assert mappings.lookup(tmp_path) is None


def test_remove_mapping_reports_whether_it_removed_an_exact_mapping(tmp_path):
    project = tmp_path / "project"
    nested = project / "nested"
    mappings.set_mapping(project, 1)
    mappings.set_mapping(nested, 2)

    assert mappings.remove_mapping(nested / "child") is False
    assert mappings.remove_mapping(nested) is True
    assert mappings.remove_mapping(nested) is False
    assert mappings.lookup(nested) == 1
    assert mappings.remove_mapping(project) is True
    assert mappings.lookup(project) is None


def test_all_mappings_lists_every_pair(tmp_path):
    expected = {
        mappings.normalise_path(tmp_path / "zeta"): 3,
        mappings.normalise_path(tmp_path / "alpha"): 1,
        mappings.normalise_path(tmp_path / "alpha" / "nested"): 2,
    }
    for path, slot in expected.items():
        mappings.set_mapping(path, slot)

    pairs = mappings.all_mappings()

    assert len(pairs) == len(expected)
    assert set(pairs) == set(expected.items())


def test_normalise_path_makes_relative_path_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = mappings.normalise_path(Path("project") / "child" / "..")

    assert Path(result).is_absolute()
    assert result == mappings.normalise_path(tmp_path / "project")


def test_normalise_path_preserves_absolute_filesystem_root(tmp_path):
    result = mappings.normalise_path(Path(tmp_path.anchor))

    assert Path(result).is_absolute()
    assert Path(result).parent == Path(result)


@pytest.mark.skipif(os.name != "nt", reason="Windows paths are case-insensitive")
def test_windows_mapping_lookup_is_case_insensitive():
    mappings.set_mapping(r"C:\Foo", 3)

    assert mappings.normalise_path(r"C:\Foo") == mappings.normalise_path(r"c:\foo")
    assert mappings.lookup(r"c:\foo") == 3
    assert mappings.lookup(r"c:\FOO\nested") == 3


def test_mappings_persist_through_save_and_load(tmp_path, swap_home):
    mappings.set_mapping(tmp_path / "alpha", 1)
    mappings.set_mapping(tmp_path / "beta", 2)
    expected = {
        mappings.normalise_path(tmp_path / "alpha"): 1,
        mappings.normalise_path(tmp_path / "beta"): 2,
    }
    loaded = mappings.load_mappings()
    assert loaded == expected

    mappings.save_mappings({})
    assert mappings.load_mappings() == {}
    mappings.save_mappings(loaded)

    assert mappings.load_mappings() == expected
    assert json.loads((swap_home / "mappings.json").read_text(encoding="utf-8")) == expected
    assert mappings.lookup(tmp_path / "beta" / "child") == 2


def test_save_and_load_respect_explicit_root(tmp_path):
    alternate_root = tmp_path / "alternate-store"
    expected = {mappings.normalise_path(tmp_path / "project"): 4}

    mappings.save_mappings(expected, root=alternate_root)

    assert mappings.load_mappings(root=alternate_root) == expected
    assert mappings.load_mappings() == {}
