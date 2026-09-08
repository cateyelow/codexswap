"""Unattended account rotation and reset-credit redemption."""

from __future__ import annotations

import logging
import math
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from . import errors, paths, resets, strategy
from .locking import FileLock
from .models import Account, UsageSnapshot
from .settings import Settings
from .store import AccountStore

_logger = logging.getLogger(__name__)
_DAY = 86400
_MAX_LOG_BYTES = 2 * 1024 * 1024
# Outcomes that prove the credit was NOT spent. Every other value, including a
# crashed attempt left as "pending", counts against the cap: there is no proof.
_UNSPENT_OUTCOMES = ("noCredit",)


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
        with FileLock(destination.parent / ".lock"):
            paths.atomic_write_json(destination, self.to_dict(), mode=0o600, indent=2)

    def redeemed_last_24h(self, now: float) -> int:
        return sum(
            now - _DAY <= entry["at"] <= now
            and entry.get("outcome") not in _UNSPENT_OUTCOMES
            for entry in self.redemptions
        )

    def prune(self, now: float) -> None:
        recent = [entry for entry in self.redemptions if entry["at"] >= now - 30 * _DAY]
        self.redemptions[:] = sorted(recent, key=lambda entry: entry["at"])[-200:]


class RedemptionJournal:
    """Record a redemption attempt before it is made, and its outcome afterwards.

    The daily cap is shared by every process using the same codexswap home, so it has
    to be re-checked and the attempt written down under one lock, before the request
    goes out. Two daemons that each read an empty history would otherwise each spend a
    credit against a cap of one, and a crash between the request and its answer would
    leave no record of the attempt at all.

    `persist=False` keeps everything in memory, which is what `tick`'s own unit tests
    want: no lock, no file, no cap re-check.
    """

    def __init__(
        self, state: AutoState, *, root: Optional[Path] = None, persist: bool = True,
    ) -> None:
        self.state = state
        self.root = root
        self.persist = persist

    def _lock(self) -> FileLock:
        return FileLock(paths.state_path().parent / ".lock" if self.root is None
                        else Path(self.root) / ".lock")

    @staticmethod
    def _key(entry: Dict[str, Any]) -> tuple:
        return (entry.get("attempt"), entry.get("at"), entry.get("slot"),
                entry.get("creditId"))

    def _adopt_other_processes(self) -> None:
        known = {self._key(entry) for entry in self.state.redemptions}
        for entry in AutoState.load(self.root).redemptions:
            if self._key(entry) not in known:
                self.state.redemptions.append(dict(entry))

    def reserve(self, entry: Dict[str, Any], *, cap: int, now: float) -> bool:
        """Claim one unit of the shared daily allowance, or report that it is gone."""
        if not self.persist:
            self.state.redemptions.append(entry)
            return True
        with self._lock():
            self._adopt_other_processes()
            if cap > 0 and self.state.redeemed_last_24h(now) >= cap:
                return False
            self.state.redemptions.append(entry)
            self.state.save(self.root)
            return True

    def settle(self, entry: Dict[str, Any]) -> None:
        """Persist the outcome the caller has already written into `entry`."""
        if self.persist:
            self.state.save(self.root)


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
    journal: Optional[RedemptionJournal] = None,
) -> TickResult:
    """Evaluate one tick using injected operations; persistence belongs to run()."""
    if journal is None:
        journal = RedemptionJournal(state, persist=False)

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
            # A named model can be exhausted while the totals still look fine.
            active_percent = active_snapshot.percent_for(settings.models)

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
        candidates.append(strategy.Candidate(account, snapshot, settings.models))
    # CONTRACT: the reset policy asks whether another account still has headroom,
    # which is the plain threshold. Hysteresis is a switch-target rule, and applying it
    # here spent a credit while an account at 75% sat idle under a threshold of 80.
    # Unknown usage counts as headroom: never spend a credit on a maybe.
    alternatives_available = any(
        candidate.percent is None or candidate.percent < settings.threshold
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
            # Write the attempt down before making it. A crash after this point leaves
            # a "pending" entry that still counts against the cap, because nothing
            # proves the credit survived.
            entry: Dict[str, Any] = {
                "at": now, "slot": active_slot, "creditId": decision.credit.id,
                "attempt": str(uuid.uuid4()), "outcome": "pending",
            }
            if not journal.reserve(entry, cap=settings.reset_max_per_day, now=now):
                _logger.info("reset redemption skipped: another process used the cap")
            else:
                outcome = redeemer(active_account, decision.credit.id)
                entry["outcome"] = outcome
                journal.settle(entry)
                if resets.outcome_is_success(outcome):
                    # The snapshot just used is now wrong in every field; drop it so
                    # the next tick and any concurrent list re-probe rather than
                    # trusting it.
                    store.forget_usage(active_account.slot)
                    state.cooldown_until = now + settings.cooldown_seconds
                    return result("redeemed", detail, active_slot)
                _logger.warning("reset redemption failed for slot %s: %s",
                                active_slot, outcome)

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


class _AutoAccounts:
    """Adapt the existing tick engine to API-key accounts without probing their usage."""

    def __init__(self, store, settings, now):
        self.store, self.settings, self.now = store, settings, now
        self.readings: Dict[int, UsageSnapshot] = {}
        self.failures: Set[int] = set()

    def __getattr__(self, name):
        return getattr(self.store, name)

    def cached_usage(self, slot, **kwargs):
        # Always re-evaluate API-key eligibility; they have no real usage cache.
        if self.store.get(slot).identity.auth_mode == "apikey":
            return None
        return self.readings.get(slot) or self.store.cached_usage(slot, **kwargs)

    def probe(self, account):
        from . import appserver, strategy

        if account.identity.auth_mode != "apikey":
            if account.slot in self.failures:
                raise errors.AppServerError("usage probe failed")
            if account.slot not in self.readings:
                try:
                    self.readings[account.slot] = appserver.probe_usage(
                        self.store._path(paths.slot_home(account.slot)),
                        timeout=self.settings.probe_timeout,
                    )
                except (errors.CodexSwapError, OSError):
                    self.failures.add(account.slot)
                    raise
            return self.readings[account.slot]
        if account.slot != self.store.active_slot:
            for other in self.store.enabled_accounts():
                if other.slot == self.store.active_slot or other.identity.auth_mode == "apikey":
                    continue
                snapshot = self.cached_usage(other.slot, max_age=self.settings.probe_stale_seconds,
                                             now=self.now)
                if snapshot is None:
                    try:
                        snapshot = self.probe(other)
                    except (errors.CodexSwapError, OSError):
                        continue
                if strategy.eligible(strategy.Candidate(other, snapshot),
                                     current_slot=self.store.active_slot,
                                     threshold=self.settings.threshold,
                                     hysteresis=self.settings.hysteresis_pct):
                    # CONTRACT: the tick engine excludes failed candidate probes.
                    # Suppress this API-key candidate whenever an ordinary one qualifies.
                    raise errors.AppServerError("API key reserved as a last-resort target")
        return UsageSnapshot.from_api({}, fetched_at=self.now)

    def record_usage(self, slot, snapshot):
        if self.store.get(slot).identity.auth_mode != "apikey":
            self.store.record_usage(slot, snapshot)


def run(
    *,
    once: bool = False,
    dry_run: bool = False,
    interval: Optional[int] = None,
    threshold: Optional[int] = None,
    models: Optional[str] = None,
    log: Callable[[str], None] = print,
) -> int:
    """The daemon loop: one implementation, so a fix here cannot miss a second copy."""
    # Importing auto itself must not require a Codex binary or switcher setup.
    from . import switcher

    def emit(result: TickResult, *, now: float) -> None:
        line = format_tick(result, now=now)
        log(line)
        _append_log(line)

    def overrides(settings: Settings) -> Settings:
        # Command-line values outrank the file on every reload, not just the first.
        if interval is not None:
            settings.set("autoswitch.intervalSeconds", str(interval))
        if threshold is not None:
            settings.set("autoswitch.threshold", str(threshold))
        if models is not None:
            settings.set("autoswitch.model", models)
        return settings

    try:
        settings = overrides(Settings.load())
        state = AutoState.load()
        consecutive_failures = 0
        previous_action: Optional[str] = None
        idle_ticks = 0
        while True:
            now = time.time()
            delay = settings.interval_seconds
            try:
                # Reload both so `config set` and external switches take effect in a
                # running daemon; a stale policy would keep spending reset credits.
                settings = overrides(Settings.load())
                store = AccountStore.load()
                # Reload state too: the daily cap is shared with any other process
                # using this home, and its history lives only in state.json.
                state = AutoState.load()
                accounts = _AutoAccounts(store, settings, now)
                result = tick(
                    accounts, settings, state, now=now, probe=accounts.probe,
                    journal=RedemptionJournal(state, persist=not dry_run),
                    redeemer=lambda account, credit_id, store=store, timeout=(
                        settings.probe_timeout
                    ): resets.redeem(
                        store._path(paths.slot_home(account.slot)),
                        credit_id=credit_id, timeout=timeout,
                    ),
                    activator=lambda account, store=store: switcher.activate(
                        # The unattended daemon must switch while Codex is running.
                        store, account, force=True, sync_back=True,
                    ),
                    dry_run=dry_run,
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
                # A dry run reports what would happen; it must leave nothing behind.
                if not dry_run:
                    state.save()
            if once:
                return 0
            time.sleep(delay)
    except KeyboardInterrupt:
        emit(TickResult("stopped", "automatic switching stopped"), now=time.time())
        return 0
