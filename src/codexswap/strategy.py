"""Target selection when switching accounts."""

from __future__ import annotations

# CONTRACT: Optional annotations must remain compatible with Python 3.9.
# ruff: noqa: UP007
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Optional

from .errors import UserError
from .models import Account, UsageSnapshot


@dataclass(frozen=True)
class Candidate:
    account: Account
    snapshot: Optional[UsageSnapshot]

    @property
    def percent(self) -> Optional[float]:
        return self.snapshot.binding_percent if self.snapshot is not None else None


def eligible(
    candidate: Candidate, *, current_slot: Optional[int], threshold: int, hysteresis: int,
) -> bool:
    if candidate.account.disabled or candidate.account.slot == current_slot:
        return False
    percent = candidate.percent
    return percent is None or percent <= threshold - hysteresis


def pick_target(
    candidates: Iterable[Candidate], *, current_slot: Optional[int], strategy: str,
    threshold: int, hysteresis: int,
) -> Optional[Account]:
    if strategy not in ("best", "next-available"):
        raise UserError(f"unknown strategy '{strategy}'; choose best or next-available")
    available = [
        candidate for candidate in candidates
        if eligible(candidate, current_slot=current_slot, threshold=threshold, hysteresis=hysteresis)
    ]
    if not available:
        return None
    if strategy == "best":
        available.sort(key=lambda candidate: (
            candidate.percent if candidate.percent is not None else float("inf"),
            candidate.account.slot,
        ))
        return available[0].account
    return rotate_next([candidate.account for candidate in available], current_slot)


def rotate_next(accounts: Sequence[Account], current_slot: Optional[int]) -> Optional[Account]:
    available = sorted(
        (account for account in accounts if not account.disabled and account.slot != current_slot),
        key=lambda account: account.slot,
    )
    if not available:
        return None
    if current_slot is not None:
        for account in available:
            if account.slot > current_slot:
                return account
    return available[0]
