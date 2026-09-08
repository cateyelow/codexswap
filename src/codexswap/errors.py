"""Public errors and their command-line exit codes."""

from __future__ import annotations


class CodexSwapError(Exception):
    """Base error for an expected codexswap failure."""

    exit_code = 1


class UserError(CodexSwapError):
    """An invalid request or user-correctable condition."""

    exit_code = 2


class AccountNotFound(UserError):
    """No configured account matches the requested reference."""


class NoAccountsConfigured(UserError):
    """An operation requires at least one configured account."""


class SlotInUse(UserError):
    """The requested account slot is already occupied."""


class CodexRunning(UserError):
    """A running Codex process prevents switching accounts."""


class AuthFileMissing(CodexSwapError):
    """The requested authentication file does not exist."""


class AuthFileInvalid(CodexSwapError):
    """Authentication data cannot be read or decoded."""


class AppServerError(CodexSwapError):
    """The Codex app server failed to complete a request."""

    exit_code = 3


class AppServerTimeout(AppServerError):
    """The Codex app server did not respond before the deadline."""


class CodexBinaryNotFound(CodexSwapError):
    """The Codex executable could not be located."""

    exit_code = 3


class AuthExpired(CodexSwapError):
    """Authentication can no longer be refreshed."""

    exit_code = 4


class BackendError(CodexSwapError):
    """A request to the optional backend failed."""

    exit_code = 5


class LockBusy(CodexSwapError):
    """A shared-state lock remained busy past its deadline."""

    exit_code = 6
