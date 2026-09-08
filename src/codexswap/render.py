"""Small, credential-free text renderers for the command line."""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Optional, Tuple

from .identity import health_of
from .models import HEALTH_EXPIRED, HEALTH_OK

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"


def supports_color(stream, setting: str) -> bool:
    if setting == "never":
        return False
    if setting == "always":
        return True
    if setting != "auto" or "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
        return False
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def colorize(text: str, color: str, *, enabled: bool) -> str:
    return color + text + RESET if enabled and color else text


def percent_color(pct: Optional[float]) -> str:
    if pct is None:
        return DIM
    if pct < 50:
        return GREEN
    return YELLOW if pct < 80 else RED


def human_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    if seconds < 0:
        return "now"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h" + (f" {minutes}m" if minutes else "")
    days, hours = divmod(hours, 24)
    return f"{days}d" + (f" {hours}h" if hours else "")


def format_ts(ts: Optional[int]) -> str:
    if ts is None:
        return "-"
    try:
        return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
    except (ValueError, OverflowError, OSError):
        return "-"


def _ascii(text: str) -> str:
    return text.encode("ascii", errors="backslashreplace").decode("ascii")


def _relative(ts: Optional[int], now: float, *, compact_days: bool = False) -> str:
    if ts is None:
        return "-"
    remaining = ts - now
    if remaining < 0:
        return "now"
    if compact_days and remaining >= 86400:
        return f"in {int(remaining // 86400)}d"
    return "in " + human_duration(remaining)


def _usage_pair(value) -> Tuple[object, bool]:
    # CONTRACT: CLI usage entries carry (snapshot, stale); direct snapshots are
    # also accepted so the status renderer remains useful on its own.
    return value if isinstance(value, tuple) else (value, False)


def _window_label(minutes: int) -> str:
    if minutes == 10080:
        return "7d"
    if minutes and minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def _account_lines(account, usage, *, active_slot, color, token_status, now, auth_failed=()):
    snapshot, stale = _usage_pair(usage)
    identity = account.identity
    heading = "  {}: {}  [{}]".format(
        account.slot, _ascii(identity.label()), _ascii(identity.plan_type or "unknown")
    )
    if account.slot == active_slot:
        heading += "  " + colorize("* active", GREEN, enabled=color)
    if account.disabled:
        heading += "  disabled"
    lines = [heading]
    rows = []
    health = health_of(identity, now=now)
    if snapshot is not None:
        for window in (snapshot.primary, snapshot.secondary):
            if window is None:
                continue
            percent = colorize(
                f"{window.used_percent:4g}%",
                percent_color(window.used_percent), enabled=color,
            )
            rows.append(f"{_window_label(window.window_minutes)}: {percent}   resets {format_ts(window.resets_at)}   {_relative(window.resets_at, now)}")
        if snapshot.primary is None and snapshot.secondary is None:
            rows.append("usage unavailable")
        reset_row = f"resets: {snapshot.available_reset_count} available"
        credit = snapshot.soonest_expiring_credit()
        if credit is not None and credit.expires_at is not None:
            reset_row += f" (soonest expires {_relative(credit.expires_at, now, compact_days=True)})"
        if stale:
            reset_row += "  " + colorize("stale", YELLOW, enabled=color)
        rows.append(reset_row)
    elif health == HEALTH_OK:
        rows.append("usage unavailable")
    if account.slot in auth_failed:
        health = HEALTH_EXPIRED
    if health != HEALTH_OK:
        # Only a rejected credential earns the re-login banner. A stale billing
        # period in the token is normal for an active subscriber and says nothing.
        reason = (
            "authentication was rejected" if health == HEALTH_EXPIRED
            else "stored credential could not be read"
        )
        rows.append(colorize(
            f"re-login needed - {reason}; run: codexswap add",
            YELLOW, enabled=color,
        ))
    if token_status:
        rows.append(f"token: access exp {format_ts(identity.access_token_exp)} ({_relative(identity.access_token_exp, now)}) - source slot")
    for index, row in enumerate(rows):
        lines.append("     " + ("+- " if index == len(rows) - 1 else "|- ") + row)
    return lines


def render_accounts(accounts, usages, *, active_slot, color, token_status=False, now=None,
                    auth_failed=()) -> str:
    now = time.time() if now is None else now
    blocks = ["Accounts:"]
    for account in accounts:
        if len(blocks) > 1:
            blocks.append("")
        blocks.append("\n".join(_account_lines(
            account, usages.get(account.slot), active_slot=active_slot, color=color,
            token_status=token_status, now=now, auth_failed=auth_failed,
        )))
    if len(blocks) == 1:
        blocks.append("  no accounts")
    return "\n".join(blocks)


def render_status(account, usage, *, active_slot, color, now=None) -> str:
    now = time.time() if now is None else now
    return "\n".join(["Account:"] + _account_lines(
        account, usage, active_slot=active_slot, color=color, token_status=False, now=now,
    ))


def render_reset_list(slot, credits, *, color, now=None) -> str:
    now = time.time() if now is None else now
    credits = list(credits)
    if not credits:
        return "  no reset credits available"
    rows = []
    for index, credit in enumerate(credits, 1):
        status = colorize(credit.status, GREEN if credit.is_available else DIM, enabled=color)
        rows.append("  {}. {}  {}  expires {} ({})  {}".format(
            index, _ascii(credit.title or "Reset credit"), status,
            format_ts(credit.expires_at), _relative(credit.expires_at, now, compact_days=True),
            _ascii(credit.id[:20]),
        ))
    rows.append(f"  {sum(credit.is_available for credit in credits)} available")
    return "\n".join(rows)


def _value_text(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return _ascii(str(value))


def render_config(items) -> str:
    items = list(items)
    key_width = max((len(key) for key, _, _ in items), default=0)
    value_width = max((len(_value_text(value)) for _, value, _ in items), default=0)
    return "\n".join(
        "{}  {}{}".format(
            key.ljust(key_width), _value_text(value).ljust(value_width),
            "  (default)" if is_default else "",
        ).rstrip()
        for key, value, is_default in items
    )


def render_mappings(pairs) -> str:
    pairs = list(pairs.items() if hasattr(pairs, "items") else pairs)
    if not pairs:
        return "  no directory mappings"
    width = max(len(_ascii(str(path))) for path, _ in pairs)
    return "\n".join(
        f"  {_ascii(str(path)).ljust(width)}  -> {slot}" for path, slot in pairs
    )
