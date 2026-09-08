from __future__ import annotations

import io
import re
from dataclasses import replace

import pytest
from conftest import RATE_LIMITS_RESULT, make_auth

from codexswap import identity, render
from codexswap.models import Account, RateLimitWindow, UsageSnapshot

NOW = 1_788_912_000.0


class Terminal:
    def __init__(self, tty=True):
        self.tty = tty

    def isatty(self):
        return self.tty


@pytest.fixture
def account():
    return Account(slot=1, identity=identity.identity_from_auth(make_auth(
        id_exp=int(NOW + 3600), access_exp=int(NOW + 86400),
    )))


@pytest.fixture
def snapshot():
    return UsageSnapshot.from_api(RATE_LIMITS_RESULT, fetched_at=NOW)


@pytest.mark.parametrize("color", [False, True])
def test_accounts_only_emit_ansi_when_enabled(account, snapshot, color):
    text = render.render_accounts([account], {1: snapshot}, active_slot=1, color=color, now=NOW)
    assert ("\033" in text) is color
    assert "84%" in text
    assert text.isascii()


def test_color_explicit_settings_override_stream_and_environment(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    assert render.supports_color(Terminal(), "never") is False
    assert render.supports_color(io.StringIO(), "always") is True


def test_auto_color_respects_tty(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    assert render.supports_color(io.StringIO(), "auto") is False
    assert render.supports_color(Terminal(False), "auto") is False
    assert render.supports_color(Terminal(), "auto") is True


@pytest.mark.parametrize("variable,value", [("NO_COLOR", "1"), ("NO_COLOR", ""), ("TERM", "dumb")])
def test_auto_color_respects_environment(monkeypatch, variable, value):
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setenv(variable, value)
    assert render.supports_color(Terminal(), "auto") is False


@pytest.mark.parametrize("seconds,expected", [
    (None, "-"), (-1, "now"), (45, "45s"), (12 * 60, "12m"),
    (2 * 3600 + 7 * 60, "2h 7m"), (6 * 86400 + 7 * 3600, "6d 7h"),
])
def test_human_duration(seconds, expected):
    assert render.human_duration(seconds) == expected


def test_timestamp_format_is_local_month_day_and_time():
    assert render.format_ts(None) == "-"
    assert re.fullmatch(r"\d{2}-\d{2} \d{2}:\d{2}", render.format_ts(int(NOW)))


@pytest.mark.parametrize("weekly_in_primary", [True, False])
def test_window_labels_come_from_minutes_not_field_names(account, snapshot, weekly_in_primary):
    weekly = snapshot.primary
    short = RateLimitWindow(12, 300, int(NOW + 3600))
    snapshot = replace(snapshot, primary=weekly if weekly_in_primary else short,
                       secondary=short if weekly_in_primary else weekly)
    text = render.render_accounts([account], {1: snapshot}, active_slot=1, color=False, now=NOW)
    assert re.search(r"7d:\s+84%", text)
    assert re.search(r"5h:\s+12%", text)
    assert not re.search(r"5h:\s+84%", text)


def test_active_disabled_and_unavailable_accounts_are_marked(account):
    disabled = replace(account, slot=2, disabled=True)
    text = render.render_accounts([account, disabled], {}, active_slot=1, color=False, now=NOW)
    headings = [line for line in text.splitlines() if re.match(r"  \d+:", line)]
    assert "* active" in headings[0]
    assert "disabled" in headings[1]
    assert "* active" not in headings[1]
    assert text.count("usage unavailable") == 2


def test_reset_list_has_count_and_one_line_per_credit(snapshot):
    text = render.render_reset_list(1, snapshot.reset_credits, color=False, now=NOW)
    lines = text.splitlines()
    credit_lines = [line for line in lines if re.match(r"\s+\d+\.", line)]
    assert len(credit_lines) == 2
    assert "2 available" in lines[-1]
    for line, credit in zip(credit_lines, snapshot.reset_credits):
        assert credit.title in line
        assert credit.status in line
        assert render.format_ts(credit.expires_at) in line
    assert "\033" not in text


def test_reset_list_without_credits_is_clear():
    text = render.render_reset_list(1, [], color=False, now=NOW)
    assert "no reset credits available" in text.lower()


def test_config_aligns_values_and_default_markers():
    items = [("ui.color", "auto", True), ("autoswitch.threshold", 80, True),
             ("autoswitch.enabled", False, False)]
    lines = render.render_config(items).splitlines()
    assert len(lines) == 3
    values = ["auto", "80", "false"]
    positions = [line.index(value, len(key)) for line, (key, _, _), value in zip(lines, items, values)]
    assert len(set(positions)) == 1
    assert lines[0].index("(default)") == lines[1].index("(default)")
    assert "(default)" not in lines[2]


def test_mappings_render_pairs_and_empty_list():
    text = render.render_mappings([("/work/a", 1), ("/work/longer", 2)])
    lines = text.splitlines()
    assert len(lines) == 2
    assert re.search(r"/work/a\s+-> 1$", lines[0])
    assert re.search(r"/work/longer\s+-> 2$", lines[1])
    assert lines[0].index("->") == lines[1].index("->")
    assert "no directory mappings" in render.render_mappings([])


def test_token_status_shows_derived_expiry_without_token_material(account, snapshot):
    text = render.render_accounts([account], {1: snapshot}, active_slot=1, color=False,
                                  token_status=True, now=NOW)
    assert "token: access exp" in text
    assert render.format_ts(account.identity.access_token_exp) in text
    assert "source slot" in text
    assert "eyJ" not in text
    assert "rt.1.TESTONLY" not in text
