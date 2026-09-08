from __future__ import annotations

import json

import pytest

from codexswap import errors
from codexswap.settings import SPECS, Settings

# CONTRACT section 5: this table deliberately does not derive values from SPECS.
CONTRACT_SPECS = (
    ("autoswitch.enabled", "bool", True, None, None, None),
    ("autoswitch.threshold", "int", 80, None, 1, 100),
    ("autoswitch.intervalSeconds", "int", 60, None, 10, 3600),
    ("autoswitch.cooldownSeconds", "int", 300, None, 0, 86400),
    ("autoswitch.hysteresisPct", "int", 10, None, 0, 50),
    ("autoswitch.strategy", "str", "best", ("best", "next-available"), None, None),
    ("autoswitch.unhealthyTicks", "int", 3, None, 1, 20),
    ("autoswitch.model", "str", "", None, None, None),
    ("reset.policy", "str", "expiring", ("never", "expiring", "exhausted", "always"), None, None),
    ("reset.expiryDays", "int", 3, None, 0, 30),
    ("reset.minUsagePercent", "int", 50, None, 0, 100),
    ("reset.maxPerDay", "int", 1, None, 0, 10),
    ("probe.timeoutSeconds", "int", 45, None, 5, 300),
    ("probe.staleSeconds", "int", 120, None, 0, 86400),
    ("probe.allowBackendFallback", "bool", False, None, None, None),
    ("ui.color", "str", "auto", ("auto", "always", "never"), None, None),
)

TYPED_PROPERTIES = (
    ("enabled", "autoswitch.enabled", "false"),
    ("threshold", "autoswitch.threshold", "90"),
    ("interval_seconds", "autoswitch.intervalSeconds", "120"),
    ("cooldown_seconds", "autoswitch.cooldownSeconds", "600"),
    ("hysteresis_pct", "autoswitch.hysteresisPct", "5"),
    ("strategy", "autoswitch.strategy", "next-available"),
    ("unhealthy_ticks", "autoswitch.unhealthyTicks", "4"),
    ("reset_policy", "reset.policy", "never"),
    ("reset_expiry_days", "reset.expiryDays", "4"),
    ("reset_min_usage_percent", "reset.minUsagePercent", "60"),
    ("reset_max_per_day", "reset.maxPerDay", "2"),
    ("probe_timeout", "probe.timeoutSeconds", "50"),
    ("probe_stale_seconds", "probe.staleSeconds", "240"),
    ("allow_backend_fallback", "probe.allowBackendFallback", "true"),
    ("ui_color", "ui.color", "never"),
)

# `models` parses its stored string into a tuple instead of returning it, so the
# identity assertions below cannot cover it. Its own tests do.
DERIVED_PROPERTIES = ("models",)


def test_specs_have_exactly_the_contract_keys_in_order():
    assert tuple(SPECS) == tuple(row[0] for row in CONTRACT_SPECS)
    assert len(SPECS) == 16


@pytest.mark.parametrize("key,kind,default,choices,minimum,maximum", CONTRACT_SPECS)
def test_spec_matches_contract(key, kind, default, choices, minimum, maximum):
    spec = SPECS[key]

    assert (spec.key, spec.type, spec.default, spec.choices, spec.minimum, spec.maximum) == (
        key, kind, default, choices, minimum, maximum,
    )
    assert type(spec.default) is type(default)


def test_missing_file_loads_all_defaults(swap_home):
    assert not (swap_home / "settings.json").exists()

    settings = Settings.load()

    for key, _, default, _, _, _ in CONTRACT_SPECS:
        assert settings.get(key) == default
        assert type(settings.get(key)) is type(default)
        assert settings.is_default(key) is True
    assert settings.items() == [(row[0], row[2], True) for row in CONTRACT_SPECS]


@pytest.mark.parametrize(
    "key,value",
    [(key, value) for key, kind, default, _, low, high in CONTRACT_SPECS
     if kind == "int" for value in (low, default, high)],
)
def test_set_coerces_integers_including_both_bounds(key, value):
    settings = Settings.load()

    result = settings.set(key, str(value))

    assert result == value
    assert type(result) is int
    assert settings.get(key) == value
    assert settings.is_default(key) is False


@pytest.mark.parametrize("key", ["autoswitch.enabled", "probe.allowBackendFallback"])
@pytest.mark.parametrize(
    "raw,expected",
    [(variant, expected) for token, expected in (
        ("true", True), ("false", False), ("1", True), ("0", False),
        ("yes", True), ("no", False), ("on", True), ("off", False),
    ) for variant in dict.fromkeys((token, token.upper(), token.title()))],
)
def test_set_coerces_every_boolean_token_case_insensitively(key, raw, expected):
    settings = Settings.load()

    assert settings.set(key, raw) is expected
    assert settings.get(key) is expected
    assert settings.is_default(key) is False


@pytest.mark.parametrize(
    "key,choice",
    [(key, choice) for key, kind, _, choices, _, _ in CONTRACT_SPECS
     if kind == "str" and choices is not None for choice in choices],
)
def test_set_accepts_every_string_choice(key, choice):
    settings = Settings.load()

    assert settings.set(key, choice) == choice
    assert settings.get(key) == choice
    assert type(settings.get(key)) is str


@pytest.mark.parametrize(
    "key,raw",
    [(key, raw) for key, kind, _, _, low, high in CONTRACT_SPECS
     if kind == "int" for raw in (str(low - 1), str(high + 1), "not-an-integer")]
    + [("autoswitch.enabled", "maybe"), ("probe.allowBackendFallback", "maybe")]
    + [("autoswitch.strategy", "random"), ("reset.policy", "sometimes"), ("ui.color", "blue")]
    + [("unknown.setting", "true")],
)
def test_set_rejects_invalid_values_and_names_the_key(key, raw):
    settings = Settings.load()
    before = settings.items()

    with pytest.raises(errors.UserError) as caught:
        settings.set(key, raw)

    assert key in str(caught.value)
    assert settings.items() == before


@pytest.mark.parametrize("method", ["get", "is_default", "unset"])
def test_unknown_key_raises_user_error(method):
    with pytest.raises(errors.UserError, match="unknown.setting"):
        getattr(Settings.load(), method)("unknown.setting")


def test_save_round_trips_only_explicit_overrides(swap_home):
    settings = Settings.load()
    overrides = {"autoswitch.threshold": "91", "reset.policy": "never", "ui.color": "always"}
    for key, raw in overrides.items():
        settings.set(key, raw)

    settings.save()

    assert json.loads((swap_home / "settings.json").read_text(encoding="utf-8")) == {
        "autoswitch": {"threshold": 91}, "reset": {"policy": "never"}, "ui": {"color": "always"},
    }
    reloaded = Settings.load()
    assert reloaded.items() == settings.items()
    for key, *_ in CONTRACT_SPECS:
        assert reloaded.is_default(key) is (key not in overrides)


def test_explicitly_saved_default_value_is_still_an_override(swap_home):
    settings = Settings.load()
    settings.set("autoswitch.threshold", "80")
    settings.save()

    reloaded = Settings.load()

    assert reloaded.get("autoswitch.threshold") == 80
    assert reloaded.is_default("autoswitch.threshold") is False
    assert json.loads((swap_home / "settings.json").read_text(encoding="utf-8")) == {
        "autoswitch": {"threshold": 80},
    }


def test_unknown_file_keys_survive_set_and_save(swap_home):
    path = swap_home / "settings.json"
    path.write_text(json.dumps({
        "autoswitch": {"threshold": 85, "futureOption": {"enabled": True}},
        "futureGroup": {"values": [1, "keep", None]},
        "futureScalar": "preserve me",
    }), encoding="utf-8")
    settings = Settings.load()
    settings.set("autoswitch.threshold", "90")
    settings.set("reset.policy", "never")

    settings.save()

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "autoswitch": {"threshold": 90, "futureOption": {"enabled": True}},
        "futureGroup": {"values": [1, "keep", None]},
        "futureScalar": "preserve me",
        "reset": {"policy": "never"},
    }


def test_corrupt_json_warns_and_loads_defaults(swap_home, capsys):
    (swap_home / "settings.json").write_text('{"autoswitch":', encoding="utf-8")

    settings = Settings.load()

    assert settings.items() == [(row[0], row[2], True) for row in CONTRACT_SPECS]
    captured = capsys.readouterr()
    assert "warning" in captured.err.lower()
    assert "settings.json" in captured.err


def test_unset_restores_default_and_repeating_it_is_a_noop(swap_home):
    settings = Settings.load()
    settings.set("autoswitch.threshold", "91")
    settings.set("ui.color", "never")

    settings.unset("autoswitch.threshold")

    assert settings.get("autoswitch.threshold") == 80
    assert settings.is_default("autoswitch.threshold") is True
    before = settings.items()
    settings.unset("autoswitch.threshold")
    settings.unset("reset.policy")
    assert settings.items() == before
    settings.save()
    assert json.loads((swap_home / "settings.json").read_text(encoding="utf-8")) == {
        "ui": {"color": "never"},
    }


def test_property_table_covers_every_typed_property():
    assert {name for name, value in vars(Settings).items() if isinstance(value, property)} == {
        name for name, _, _ in TYPED_PROPERTIES
    } | set(DERIVED_PROPERTIES)


@pytest.mark.parametrize("name,key,raw", TYPED_PROPERTIES)
def test_typed_property_agrees_with_get_before_and_after_override(name, key, raw):
    settings = Settings.load()

    assert getattr(settings, name) == settings.get(key)
    assert type(getattr(settings, name)) is type(settings.get(key))
    value = settings.set(key, raw)
    assert getattr(settings, name) == settings.get(key) == value
    assert type(getattr(settings, name)) is type(value)


@pytest.mark.parametrize("raw,expected", [
    ("", ()),
    ("codex", ("codex",)),
    ("  codex  ", ("codex",)),
    ("codex,codex_bengalfox", ("codex", "codex_bengalfox")),
    ("codex , , codex_bengalfox ,", ("codex", "codex_bengalfox")),
    ("all", ("all",)),
])
def test_models_splits_and_trims_the_stored_list(raw, expected):
    settings = Settings.load()
    settings.set("autoswitch.model", raw)

    assert settings.models == expected


def test_models_keeps_the_written_order():
    settings = Settings.load()
    settings.set("autoswitch.model", "b,a,c")

    assert settings.models == ("b", "a", "c")


def test_models_is_empty_by_default():
    assert Settings.load().models == ()


@pytest.mark.parametrize("raw", ["", "codex", "a,b,c", "GPT-5.3-Codex-Spark", "all"])
def test_set_accepts_any_string_for_a_free_form_setting(raw):
    settings = Settings.load()

    assert settings.set("autoswitch.model", raw) == raw
    assert settings.get("autoswitch.model") == raw
