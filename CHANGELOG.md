# Changelog

Notable behaviour changes, newest first. This tool holds OAuth credentials and can
spend banked reset credits, so anything that changes when it writes a credential or
redeems a credit is listed here even when it is small.

## 0.1.0 — unreleased

First public release.

- Per-account slots under `$CODEXSWAP_HOME`, each an isolated `CODEX_HOME`, with
  `switch`, `run`, directory mappings, `export`/`import`, and `doctor`.
- Usage read through the Codex app-server (`account/rateLimits/read`), including the
  per-model breakdown; `--model` and `autoswitch.model` weigh named model limits
  alongside the account totals.
- `auto` monitors usage, switches by `best` or `next-available`, and applies a reset
  policy. **Redeeming a reset credit cannot be undone.** The default policy spends a
  credit that is close to expiring; `reset.policy never` keeps redemption manual.
- Fail-closed rules for everything irreversible: credentials are validated before any
  write, unreadable settings or state refuse to authorise a redemption, failed process
  discovery blocks a switch, a slot whose credential belongs to another account is
  refused rather than read, a live login no account owns is not overwritten without
  `--force`, and the slot's account is re-checked in the moment before a credit is
  spent.
- Runs on Windows, macOS and Linux, Python 3.9+, standard library only at runtime.
  Codex itself is installed separately.
