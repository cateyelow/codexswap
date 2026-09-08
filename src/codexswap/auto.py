"""Unattended account rotation and reset-credit redemption."""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import errors, paths, resets, strategy
from .models import Account, UsageSnapshot
from .settings import Settings
from .store import AccountStore

_logger = logging.getLogger(__name__)
_DAY = 86400
_MAX_LOG_BYTES = 2 * 1024 * 1024


def _timestamp(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


@dataclass
class AutoState:
    last_switch_at: Optional[float] = None
    cooldown_until: Optional[float] = None
    unhealthy: Dict[int, int] = field(default_factory=dict)
    redemptions: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lastSwitchAt": self.last_switch_at,
            "cooldownUntil": self.cooldown_until,
            "unhealthy": {str(slot): count for slot, count in self.unhealthy.items()},
            "redemptions": [dict(entry) for entry in self.redemptions],
        }

    @classmethod
    def from_dict(cls, d: Any) -> AutoState:
        if not isinstance(d, dict):
            return cls()
        state = cls(
            last_switch_at=_timestamp(d.get("lastSwitchAt")),
            cooldown_until=_timestamp(d.get("cooldownUntil")),
        )
        unhealthy = d.get("unhealthy")
        if isinstance(unhealthy, dict):
            for slot, count in unhealthy.items():
                try:
                    state.unhealthy[int(slot)] = max(0, int(count))
                except (TypeError, ValueError, OverflowError):
                    continue
        redemptions = d.get("redemptions")
        if isinstance(redemptions, list):
            for entry in redemptions:
                if not isinstance(entry, dict):
                    continue
                at = _timestamp(entry.get("at"))
                if at is not None:
                    state.redemptions.append(dict(entry, at=at))
        return state

    @classmethod
    def load(cls, root: Optional[Path] = None) -> AutoState:
        destination = Path(root) / "state.json" if root is not None else paths.state_path()
        return cls.from_dict(paths.read_json_tolerant(destination, {}))

    def save(self, root: Optional[Path] = None) -> None:
        self.prune(time.time())
        destination = Path(root) / "state.json" if root is not None else paths.state_path()
        paths.atomic_write_json(destination, self.to_dict(), mode=0o600, indent=2)

    def redeemed_last_24h(self, now: float) -> int:
        return sum(now - _DAY <= entry["at"] <= now for entry in self.redemptions)

    def prune(self, now: float) -> None:
        recent = [entry for entry in self.redemptions if entry["at"] >= now - 30 * _DAY]
        self.redemptions[:] = sorted(recent, key=lambda entry: entry["at"])[-200:]


@dataclass(frozen=True)
class TickResult:
    action: str
    detail: str
    slot: Optional[int] = None


def tick(
    store: AccountStore,
    settings: Settings,
    state: AutoState,
    *,
    now: float,
    probe: Callable[[Account], UsageSnapshot],
    redeemer: Callable[[Account, str], str],
    activator: Callable[[Account], Any],
    dry_run: bool = False,
) -> TickResult:
    """Evaluate one tick using injected operations; persistence belongs to run()."""

    def result(action: str, detail: str, slot: Optional[int] = None) -> TickResult:
        prefix = "[dry-run] " if dry_run else ""
        return TickResult(action, prefix + detail, slot)

    # 1. Respect the automatic-switching setting before doing any reads.
    if not settings.enabled:
        return result("disabled", "automatic switching is disabled")

    # 2. Disabled accounts never participate in selection.
    accounts = list(store.enabled_accounts())
    if not accounts:
        return result("no-accounts", "no enabled accounts")

    # 3. Avoid probing during the cooldown following a switch or redemption.
    active_slot = store.active_slot
    if state.cooldown_until is not None and now < state.cooldown_until:
        return result(
            "cooldown", f"{state.cooldown_until - now:g}s remaining", active_slot,
        )

    # 4. Always refresh the active account, escalating consecutive probe failures.
    active_account = store.get(active_slot) if active_slot is not None else None
    active_snapshot: Optional[UsageSnapshot] = None
    active_percent: Optional[float] = 100
    if active_account is not None:
        try:
            active_snapshot = probe(active_account)
        except (errors.CodexSwapError, OSError) as exc:
            failures = state.unhealthy.get(active_slot, 0) + 1
            state.unhealthy[active_slot] = failures
            if failures < settings.unhealthy_ticks:
                return result("probe-failed", str(exc), active_slot)
            # CONTRACT: An unusable active account has effective usage 100%; no
            # fresh snapshot exists with which to decide on a reset credit.
        else:
            store.record_usage(active_account.slot, active_snapshot)
            state.unhealthy[active_account.slot] = 0
            active_percent = active_snapshot.binding_percent

    # 5. Only known usage below the threshold establishes that we can stay idle.
    if (active_account is not None and active_percent is not None
            and active_percent < settings.threshold):
        return result("idle", f"{active_percent:g}% below {settings.threshold}%", active_slot)

    # 6. Prefer fresh cached snapshots when considering other enabled accounts.
    candidates: List[strategy.Candidate] = []
    for account in accounts:
        if account.slot == active_slot:
            continue
        snapshot = store.cached_usage(
            account.slot, max_age=settings.probe_stale_seconds, now=now,
        )
        if snapshot is None:
            try:
                snapshot = probe(account)
            except (errors.CodexSwapError, OSError):
                # CONTRACT: Failed probes are excluded; a successful probe with
                # unknown usage is still eligible under the selection strategy.
                continue
            store.record_usage(account.slot, snapshot)
            state.unhealthy[account.slot] = 0
        candidates.append(strategy.Candidate(account, snapshot))
    alternatives_available = any(
        strategy.eligible(
            candidate, current_slot=active_slot, threshold=settings.threshold,
            hysteresis=settings.hysteresis_pct,
        )
        for candidate in candidates
    )

    # 7. Evaluate reset policy before choosing a switch target.
    if active_account is not None and active_snapshot is not None:
        decision = resets.decide(
            active_snapshot, settings, now=now,
            redeemed_last_24h=state.redeemed_last_24h(now),
            alternatives_available=alternatives_available,
        )
        if decision.should_redeem and decision.credit is not None:
            detail = resets.describe(decision)
            if dry_run:
                return result("redeemed", detail, active_slot)
            outcome = redeemer(active_account, decision.credit.id)
            if resets.outcome_is_success(outcome):
                state.redemptions.append({
                    "at": now, "slot": active_slot,
                    "creditId": decision.credit.id, "outcome": outcome,
                })
                state.cooldown_until = now + settings.cooldown_seconds
                return result("redeemed", detail, active_slot)
            _logger.warning("reset redemption failed for slot %s: %s", active_slot, outcome)

    # 8. The shared strategy also enforces hysteresis and unknown-usage ordering.
    target = strategy.pick_target(
        candidates, current_slot=active_slot, strategy=settings.strategy,
        threshold=settings.threshold, hysteresis=settings.hysteresis_pct,
    )
    if target is None:
        return result("no-target", "no eligible alternative account", active_slot)

    # 9. Commit only real switches; dry runs preserve all action history.
    target_percent = next(c.percent for c in candidates if c.account.slot == target.slot)
    percent_text = f"{target_percent:g}" if target_percent is not None else "unknown"
    if not dry_run:
        activator(target)
        state.last_switch_at = now
        state.cooldown_until = now + settings.cooldown_seconds
    return result("switched", f"{active_slot} -> {target.slot}, {percent_text}%", target.slot)


def format_tick(result: TickResult, *, now: float) -> str:
    timestamp = datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds")
    return f"[{timestamp}] {result.action}: {result.detail}"


def _append_log(line: str) -> None:
    destination = paths.log_path()
    paths.ensure_dir(destination.parent)
    with destination.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    if destination.stat().st_size > _MAX_LOG_BYTES:
        with destination.open("r", encoding="utf-8") as handle:
            tail = "".join(deque(handle, maxlen=1000))
        paths.atomic_write_text(destination, tail, mode=0o600)


def run(
    *,
    once: bool = False,
    dry_run: bool = False,
    interval: Optional[int] = None,
    threshold: Optional[int] = None,
    log: Callable[[str], None] = print,
) -> int:
    # Importing auto itself must not require a Codex binary or switcher setup.
    from . import appserver, switcher

    def emit(result: TickResult, *, now: float) -> None:
        line = format_tick(result, now=now)
        log(line)
        _append_log(line)

    try:
        settings = Settings.load()
        if interval is not None:
            settings.set("autoswitch.intervalSeconds", str(interval))
        if threshold is not None:
            settings.set("autoswitch.threshold", str(threshold))
        state = AutoState.load()

        def probe(account: Account) -> UsageSnapshot:
            return appserver.probe_usage(
                paths.slot_home(account.slot), timeout=settings.probe_timeout,
            )

        def redeemer(account: Account, credit_id: str) -> str:
            return resets.redeem(paths.slot_home(account.slot), credit_id=credit_id)

        def activator(target: Account) -> None:
            # The unattended daemon must be able to switch while Codex is running.
            switcher.activate(store, target, force=True, sync_back=True)

        consecutive_failures = 0
        previous_action: Optional[str] = None
        idle_ticks = 0
        while True:
            now = time.time()
            delay = settings.interval_seconds
            try:
                # Reload the registry so external switches and additions are visible.
                store = AccountStore.load()
                result = tick(
                    store, settings, state, now=now, probe=probe,
                    redeemer=redeemer, activator=activator, dry_run=dry_run,
                )
            except errors.CodexSwapError as exc:
                consecutive_failures += 1
                # Once capped, avoid constructing unbounded powers during outages.
                delay = min(settings.interval_seconds * 2 ** min(consecutive_failures, 10), 900)
                previous_action = "error"
                idle_ticks = 0
                emit(TickResult("error", str(exc)), now=now)
            else:
                consecutive_failures = 0
                if result.action == "idle" and previous_action == "idle":
                    idle_ticks += 1
                else:
                    idle_ticks = 0
                if result.action != "idle" or previous_action != "idle" or idle_ticks >= 10:
                    emit(result, now=now)
                    idle_ticks = 0
                previous_action = result.action
            finally:
                state.save()
            if once:
                return 0
            time.sleep(delay)
    except KeyboardInterrupt:
        emit(TickResult("stopped", "automatic switching stopped"), now=time.time())
        return 0
