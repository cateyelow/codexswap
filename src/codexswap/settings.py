"""Typed, validated user settings backed by settings.json."""

from __future__ import annotations

# CONTRACT: Preserve the documented typing annotations for Python 3.9 callers.
# ruff: noqa: UP006, UP007, UP035, UP037
import copy
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import paths
from .errors import UserError
from .locking import FileLock


@dataclass(frozen=True)
class SettingSpec:
    key: str
    type: str
    default: Any
    choices: Optional[Tuple[str, ...]]
    minimum: Optional[int]
    maximum: Optional[int]
    help: str


SPECS: Dict[str, SettingSpec] = {
    spec.key: spec
    for spec in (
        SettingSpec(
            "autoswitch.enabled", "bool", True, None, None, None,
            "Enable automatic account switching and reset-credit decisions.",
        ),
        SettingSpec(
            "autoswitch.threshold", "int", 80, None, 1, 100,
            "Consider switching when the active account reaches this usage percent.",
        ),
        SettingSpec(
            "autoswitch.intervalSeconds", "int", 60, None, 10, 3600,
            "Wait this many seconds between automatic usage checks.",
        ),
        SettingSpec(
            "autoswitch.cooldownSeconds", "int", 300, None, 0, 86400,
            "Pause automatic actions for this many seconds after a switch or reset.",
        ),
        SettingSpec(
            "autoswitch.hysteresisPct", "int", 10, None, 0, 50,
            "Require targets to be this many percentage points below the threshold.",
        ),
        SettingSpec(
            "autoswitch.strategy", "str", "best", ("best", "next-available"), None, None,
            "Choose the lowest-usage account or the next eligible slot.",
        ),
        SettingSpec(
            "autoswitch.unhealthyTicks", "int", 3, None, 1, 20,
            "Switch away after this many consecutive failures to probe an account.",
        ),
        SettingSpec(
            "reset.policy", "str", "expiring", ("never", "expiring", "exhausted", "always"),
            None, None, "Choose when automatic checks may redeem a reset credit.",
        ),
        SettingSpec(
            "reset.expiryDays", "int", 3, None, 0, 30,
            "Redeem expiring credits when this many days or fewer remain.",
        ),
        SettingSpec(
            "reset.minUsagePercent", "int", 50, None, 0, 100,
            "Require at least this usage percent before automatically redeeming a credit.",
        ),
        SettingSpec(
            "reset.maxPerDay", "int", 1, None, 0, 10,
            "Limit redemptions in the last 24 hours; zero means no cap.",
        ),
        SettingSpec(
            "probe.timeoutSeconds", "int", 45, None, 5, 300,
            "Allow this many seconds for a Codex app-server probe.",
        ),
        SettingSpec(
            "probe.staleSeconds", "int", 120, None, 0, 86400,
            "Reuse cached usage for this many seconds before probing again.",
        ),
        SettingSpec(
            "probe.allowBackendFallback", "bool", False, None, None, None,
            "Allow undocumented backend requests when app-server probing fails.",
        ),
        SettingSpec(
            "ui.color", "str", "auto", ("auto", "always", "never"), None, None,
            "Use automatic terminal detection, always emit color, or disable color.",
        ),
    )
}


def validate(spec: SettingSpec, value: Any) -> bool:
    """Whether a value decoded from JSON is usable for this setting.

    `set()` validates what the user types, but a stored file can be edited by hand
    or written by an older release. An unvalidated value reaches decisions that
    spend reset credits, where `"false"` is truthy and `maxPerDay: -1` removes the
    cap, so anything that does not match the spec is treated as absent.
    """
    if spec.type == "bool":
        return isinstance(value, bool)
    if spec.type == "int":
        # bool is a subclass of int; true is not the number 1 for these keys.
        if not isinstance(value, int) or isinstance(value, bool):
            return False
        if spec.minimum is not None and value < spec.minimum:
            return False
        return not (spec.maximum is not None and value > spec.maximum)
    if not isinstance(value, str):
        return False
    return spec.choices is None or value in spec.choices


class Settings:
    """Explicit overrides and preserved unknown fields from settings.json."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self._path = Path(root) / "settings.json" if root is not None else paths.settings_path()
        self._values: Dict[str, Any] = {}
        self._extra: Dict[str, Any] = {}
        # Keys whose stored value failed validation. `get` reports the default for
        # these, but a caller about to spend something irreversible can tell the
        # difference between "the user never set it" and "the user set nonsense".
        self.invalid: Dict[str, Any] = {}

    @classmethod
    def load(cls, root: Optional[Path] = None) -> "Settings":
        settings = cls(root)
        missing = object()
        data = paths.read_json_tolerant(settings._path, missing)
        if data is missing:
            if settings._path.is_file():
                print("warning: settings.json is not valid JSON, using defaults", file=sys.stderr)
            return settings
        # CONTRACT: A settings document must be a nested dict; other JSON shapes use defaults.
        if not isinstance(data, dict):
            return settings
        settings._extra = copy.deepcopy(data)
        for key, spec in SPECS.items():
            group, name = key.split(".", 1)
            section = settings._extra.get(group)
            if not isinstance(section, dict) or name not in section:
                continue
            if not validate(spec, section[name]):
                # Leave it in _extra so save() round-trips the file the user wrote,
                # and report the default until they correct or overwrite the value.
                settings.invalid[key] = section[name]
                continue
            settings._values[key] = section.pop(name)
            if not section:
                del settings._extra[group]
        if settings.invalid:
            print(
                "warning: ignoring invalid settings, using defaults: "
                + ", ".join(sorted(settings.invalid)),
                file=sys.stderr,
            )
        return settings

    @staticmethod
    def _spec(key: str) -> SettingSpec:
        if key not in SPECS:
            valid = ", ".join(list(SPECS)[:8]) + ", ..."
            raise UserError(f"unknown setting '{key}'; valid keys: {valid}")
        return SPECS[key]

    def get(self, key: str) -> Any:
        spec = self._spec(key)
        return self._values.get(key, spec.default)

    def is_default(self, key: str) -> bool:
        self._spec(key)
        return key not in self._values

    def set(self, key: str, raw: str) -> Any:
        spec = self._spec(key)
        value: Any
        if spec.type == "int":
            try:
                value = int(raw)
            except ValueError:
                raise UserError(f"{key} requires an integer, got {raw!r}") from None
            if ((spec.minimum is not None and value < spec.minimum)
                    or (spec.maximum is not None and value > spec.maximum)):
                raise UserError(f"{key} must be in the range {spec.minimum}..{spec.maximum}")
        elif spec.type == "bool":
            tokens = {
                "true": True, "1": True, "yes": True, "on": True,
                "false": False, "0": False, "no": False, "off": False,
            }
            token = raw.lower()
            if token not in tokens:
                raise UserError(f"{key} requires true/false/1/0/yes/no/on/off, got {raw!r}")
            value = tokens[token]
        else:
            value = raw
            if spec.choices is not None and value not in spec.choices:
                raise UserError(f"{key} must be one of: {', '.join(spec.choices)}")
        self._values[key] = value
        return value

    def unset(self, key: str) -> None:
        self._spec(key)
        self._values.pop(key, None)

    def items(self) -> List[Tuple[str, Any, bool]]:
        return [(key, self.get(key), self.is_default(key)) for key in SPECS]

    def save(self) -> None:
        data = copy.deepcopy(self._extra)
        for key, value in self._values.items():
            group, name = key.split(".", 1)
            if not isinstance(data.get(group), dict):
                data[group] = {}
            data[group][name] = value
        # Two `config set` commands touching different keys must not lose each other.
        with FileLock(self._path.parent / ".lock"):
            paths.atomic_write_json(self._path, data, mode=0o644)

    @property
    def threshold(self) -> int:
        return self.get("autoswitch.threshold")

    @property
    def interval_seconds(self) -> int:
        return self.get("autoswitch.intervalSeconds")

    @property
    def cooldown_seconds(self) -> int:
        return self.get("autoswitch.cooldownSeconds")

    @property
    def hysteresis_pct(self) -> int:
        return self.get("autoswitch.hysteresisPct")

    @property
    def strategy(self) -> str:
        return self.get("autoswitch.strategy")

    @property
    def unhealthy_ticks(self) -> int:
        return self.get("autoswitch.unhealthyTicks")

    @property
    def enabled(self) -> bool:
        return self.get("autoswitch.enabled")

    @property
    def reset_policy(self) -> str:
        return self.get("reset.policy")

    @property
    def reset_expiry_days(self) -> int:
        return self.get("reset.expiryDays")

    @property
    def reset_min_usage_percent(self) -> int:
        return self.get("reset.minUsagePercent")

    @property
    def reset_max_per_day(self) -> int:
        return self.get("reset.maxPerDay")

    @property
    def probe_timeout(self) -> int:
        return self.get("probe.timeoutSeconds")

    @property
    def probe_stale_seconds(self) -> int:
        return self.get("probe.staleSeconds")

    @property
    def allow_backend_fallback(self) -> bool:
        return self.get("probe.allowBackendFallback")

    @property
    def ui_color(self) -> str:
        return self.get("ui.color")
