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


def _entry_key(entry: Dict[str, Any]) -> tuple:
    return (entry.get("attempt"), entry.get("at"), entry.get("slot"), entry.get("creditId"))


def _merge_history(
    stored: List[Dict[str, Any]], mine: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Union two redemption ledgers, preferring whichever copy knows the outcome.

    A settled entry is later information than the same entry still marked pending,
    so it wins whichever side holds it. Entries only one side has are always kept:
    losing one would give back allowance that a credit was already spent on.
    """
    merged: Dict[tuple, Dict[str, Any]] = {}
    for entry in list(stored) + list(mine):
        key = _entry_key(entry)
        current = merged.get(key)
        if current is None or (current.get("outcome") == "pending"
                               and entry.get("outcome") != "pending"):
            merged[key] = dict(entry)
    return sorted(merged.values(), key=lambda entry: entry["at"])


@dataclass
class AutoState:
    last_switch_at: Optional[float] = None
    cooldown_until: Optional[float] = None
    unhealthy: Dict[int, int] = field(default_factory=dict)
    redemptions: List[Dict[str, Any]] = field(default_factory=list)
    # True when state.json exists but could not be read. An empty history and a
    # history we failed to read look identical otherwise, and the second must never
    # be allowed to authorise a redemption. Not serialised: it describes this read.
    unreadable: bool = field(default=False, compare=False)

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
        missing = object()
        data = paths.read_json_tolerant(destination, missing)
        if data is missing:
            state = cls()
            state.unreadable = destination.exists()
            return state
        return cls.from_dict(data)

    def save(self, root: Optional[Path] = None) -> None:
        self.prune(time.time())
        destination = Path(root) / "state.json" if root is not None else paths.state_path()
        with FileLock(destination.parent / ".lock"):
            # Every other field belongs to this process, but the redemption history is
            # a shared ledger of irreversible acts. Writing our copy over it would
            # erase another daemon's spend and hand back its share of the daily cap.
            document = self.to_dict()
            document["redemptions"] = _merge_history(
                AutoState.load(root).redemptions, self.redemptions,
            )
            paths.atomic_write_json(destination, document, mode=0o600, indent=2)

    def redeemed_last_24h(self, now: float) -> int:
        return sum(
            now - _DAY <= entry["at"] <= now
            and entry.get("outcome") not in _UNSPENT_OUTCOMES
            for entry in self.redemptions
        )

    def prune(self, now: float) -> None:
        recent = sorted(
            (entry for entry in self.redemptions if entry["at"] >= now - 30 * _DAY),
            key=lambda entry: entry["at"],
        )
        # The count cap bounds the file, but it must never evict an entry the daily
        # cap still counts: a burst of noCredit attempts would otherwise push a real
        # spend out of the window and hand back allowance already used. Those attempts
        # are themselves droppable, so the bound still holds in the case it exists for.
        def counted(entry: Dict[str, Any]) -> bool:
            return (entry["at"] >= now - _DAY
                    and entry.get("outcome") not in _UNSPENT_OUTCOMES)

        protected = [entry for entry in recent if counted(entry)]
        droppable = [entry for entry in recent if not counted(entry)]
        keep = droppable[-max(0, 200 - len(protected)):] if len(protected) < 200 else []
        self.redemptions[:] = sorted(protected + keep, key=lambda entry: entry["at"])


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

    def _adopt_other_processes(self) -> None:
        known = {_entry_key(entry) for entry in self.state.redemptions}
        for entry in AutoState.load(self.root).redemptions:
            if _entry_key(entry) not in known:
                self.state.redemptions.append(dict(entry))

    def reserve(self, entry: Dict[str, Any], *, cap: int, now: float) -> bool:
        """Claim one unit of the shared daily allowance, or report that it is gone."""
        if not self.persist:
            self.state.redemptions.append(entry)
            return True
        with self._lock():
            if AutoState.load(self.root).unreadable:
                # A history we cannot read is not an empty history. Spending against
                # it would be spending against a cap whose usage is unknown.
                _logger.warning("state.json is unreadable; refusing to redeem")
                return False
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
            # A dry run must leave no trace: a cached reading would let the next real
            # tick skip its own probe, which is exactly "changing what happens next".
            if not dry_run:
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
    unknown_alternatives = False
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
                # For the reset policy the failure means "unknown", not "exhausted":
                # spending a credit because a probe timed out is spending on a guess.
                unknown_alternatives = True
                continue
            if not dry_run:
                store.record_usage(account.slot, snapshot)
            state.unhealthy[account.slot] = 0
        candidates.append(strategy.Candidate(account, snapshot, settings.models))
    # CONTRACT: the reset policy asks whether another account still has headroom,
    # which is the plain threshold. Hysteresis is a switch-target rule, and applying it
    # here spent a credit while an account at 75% sat idle under a threshold of 80.
    # Unknown usage counts as headroom: never spend a credit on a maybe.
    alternatives_available = unknown_alternatives or any(
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
        account = self.store.get(slot)
        if account.identity.auth_mode == "apikey":
            return None
        snapshot = self.readings.get(slot) or self.store.cached_usage(slot, **kwargs)
        # A cached reading gets the same identity check a fresh probe gets; otherwise
        # the daemon decides about a slot using another account's numbers.
        if snapshot is not None and not snapshot.describes(account.identity):
            return None
        return snapshot

    def probe(self, account):
        from . import appserver, strategy

        if account.identity.auth_mode != "apikey":
            if account.slot in self.failures:
                raise errors.AppServerError("usage probe failed")
            if account.slot not in self.readings:
                try:
                    snapshot = appserver.probe_usage(
                        self.store.path_for(paths.slot_home(account.slot)),
                        timeout=self.settings.probe_timeout,
                    )
                except (errors.CodexSwapError, OSError):
                    self.failures.add(account.slot)
                    raise
                if not snapshot.describes(account.identity):
                    # The slot home holds someone else. The daemon can spend a reset
                    # credit, so an account it cannot identify is unusable, not idle.
                    self.failures.add(account.slot)
                    raise errors.AppServerError(
                        f"slot {account.slot} answered for a different account "
                        "than the registry records"
                    )
                self.readings[account.slot] = snapshot
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
                candidate = strategy.Candidate(other, snapshot, self.settings.models)
                if strategy.eligible(candidate,
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
        # The log is a record of what the daemon did. A dry run did nothing, and
        # writing to a shared file is still writing.
        if not dry_run:
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
                        store.path_for(paths.slot_home(account.slot)),
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
