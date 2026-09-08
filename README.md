# codexswap

Automatic account switching and banked rate-limit reset management for the OpenAI Codex CLI.

Using several Codex accounts means keeping track of each account's usage, saved
credentials, and expiring reset credits. `codexswap` keeps accounts in separate local
slots, shows their usage, and switches as limits fill up. Its reset policy can spend
a banked credit when it is useful, while guarding against wasting one on a lightly
used account. It works on Windows, macOS, and Linux.

[![Python 3.9–3.13](https://img.shields.io/badge/python-3.9%20%7C%203.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue)](https://github.com/cateyelow/codexswap/blob/main/pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/cateyelow/codexswap/actions/workflows/ci.yml/badge.svg)](https://github.com/cateyelow/codexswap/actions/workflows/ci.yml)

## Install

Requires **Python 3.9+** and an installed Codex CLI. `codexswap` has **zero runtime
dependencies**; it uses Python's standard library. The Codex integration described
here was verified on **codex-cli 0.153.4**.

Install with uv:

```sh
uv tool install codexswap
```

Alternatively, choose pipx or pip:

```sh
pipx install codexswap
# Or:
pip install codexswap
```

From source:

```sh
git clone https://github.com/cateyelow/codexswap.git
cd codexswap
uv tool install .          # Or: pip install -e ".[dev]" for a development checkout.
```

## Quick start

Use the normal Codex home (`~/.codex`) for these logins. During the second login,
choose the other account in the browser.

```sh
codex login                      # Sign in to your first Codex account.
codexswap add --alias personal    # Save its credentials in a local slot.
codex login                      # Sign in to your second Codex account.
codexswap add --alias work        # Save the second account in another slot.
codexswap list                    # Show saved accounts, usage, and reset credits.
codexswap auto                    # Monitor usage and apply switching/reset policy.
```

`auto` runs in the foreground until you press Ctrl+C. The default policy may redeem
an expiring credit; read [Reset credits](#reset-credits) before leaving it running.

## How it works

Each slot owns a complete `CODEX_HOME` at `~/.codexswap/homes/<slot>/`. Codex stores
its authentication, caches, and other local state there. Probes and
`codexswap run <ref>` point Codex at that slot, so token refreshes performed by
Codex land back in the correct slot automatically.

Switching the normal, live account also updates `~/.codex/auth.json`. **Before a
switch, the live `auth.json` is copied back into the slot it belongs to**, preserving
tokens refreshed by the live session. This avoids losing refreshed credentials
when restoring an older saved file. Switching refuses while Codex is running
unless you explicitly use `--force`.

Usage and reset credits come from the supported `codex app-server` JSON-RPC method
`account/rateLimits/read`; redemption uses
`account/rateLimitResetCredit/consume`. This uses Codex's protocol, without scraping
its UI. `codexswap run` leaves the live auth file alone, so a slot can run alongside
a different active account.

## Commands

Square brackets indicate optional arguments. `<ref>` resolves by slot number,
case-insensitive alias, case-insensitive email, then unique email prefix. An
ambiguous reference reports the matching candidates.

| Command | Description |
| --- | --- |
| `codexswap help` | Show command help. |
| `codexswap version` | Print the installed version. |
| `codexswap list [--json] [--token-status] [--no-probe]` | List accounts and usage; optionally show derived token health or use cached data only (alias: `ls`). |
| `codexswap status [--json]` | Show the active account (aliases: `current`, `st`). |
| `codexswap switch [<ref>] [--strategy best\|next-available] [--force] [--json]` | Activate an account, select by usage strategy, or rotate to the next slot when neither is given. |
| `codexswap add [--slot N] [--alias NAME]` | Save the live login in a slot; re-adding an existing email updates its slot. |
| `codexswap remove <ref>` | Remove a saved account (alias: `rm`). |
| `codexswap disable <ref>` | Exclude an account from automatic selection while keeping it stored. |
| `codexswap enable <ref>` | Return a disabled account to automatic selection. |
| `codexswap alias [<ref> <name> \| <ref> --unset]` | List aliases, assign one, or remove an account's alias. |
| `codexswap swap <a> <b>` | Exchange two accounts' slots. |
| `codexswap move <ref> <slot>` | Move an account to another slot. |
| `codexswap run [<ref>] [-- <codex args>...]` | Run Codex in a slot's home, using the current directory's mapping if no reference is given. |
| `codexswap map [<ref> [path]]` | List directory mappings or map a directory to an account. |
| `codexswap unmap [path]` | Remove a directory mapping, defaulting to the current directory. |
| `codexswap probe [<ref>] [--json]` | Probe usage for the selected account. |
| `codexswap reset [list] [--json]` | List the active account's banked reset credits. |
| `codexswap reset use [<ref>] [--credit ID] [--yes] [--dry-run]` | Redeem a reset credit with confirmation, or preview the redemption. |
| `codexswap auto [--once] [--dry-run] [--interval N] [--threshold N]` | Run automatic monitoring, optionally for one tick or without switching or redeeming. |
| `codexswap config [set <KEY> <VALUE> \| unset <KEY>] [--json]` | Show settings, set a validated value, or restore a default. |
| `codexswap export <path> [--account <ref>]` | Export all accounts or one account, including credentials. |
| `codexswap import <path> [--force]` | Import accounts, remapping occupied slots unless overwrite is explicitly forced. |
| `codexswap purge [--yes]` | Delete the entire codexswap data root after confirmation, leaving `~/.codex` untouched. |

Global flags: `--debug`, `--version`, `--no-color`, and `--home PATH`. The last
overrides the `CODEXSWAP_HOME` storage root, which otherwise defaults to
`~/.codexswap`.

## Reset credits

A banked reset is a credit granted to an account that restores **both the 5-hour
and weekly usage windows to 0%**. It expires **30 days after it is granted**.
**Redeeming a credit is irreversible.** Wasting a reset on a lightly used account
is the main failure mode the policy guards against.

The default `reset.policy=expiring` only spends automatically when a credit is
about to expire **and the account is genuinely used up relative to the configured
threshold**. With the defaults, auto mode reaches the policy at 80% usage or above,
the credit must expire within 3 days, and the policy's minimum-use guard requires
at least 50% usage. This does not require reaching 100%: usage means the higher
percentage of the two windows, not that both windows must be exhausted.

| `reset.policy` | Behaviour and when to choose it |
| --- | --- |
| `never` | Never redeem automatically; choose this to keep every redemption manual. |
| `expiring` | Redeem only when the soonest-expiring credit is within `reset.expiryDays`; choose this to use credits before they expire while preserving newer ones. |
| `exhausted` | Redeem only when no other enabled account is below the switching threshold; choose this to use other accounts before spending credits. |
| `always` | Redeem whenever the policy is reached and its guards pass; choose this to prefer a reset over switching, regardless of expiry or alternatives. |

Every policy that can redeem requires an available credit, known usage, and usage
at or above `reset.minUsagePercent`. The default cap is one automatic redemption
across accounts in a rolling 24 hours; `reset.maxPerDay=0` removes that cap. The
policy chooses the soonest-expiring available credit, with dated credits ahead of
those without an expiry. Auto mode considers redemption before selecting another
account.

Inspect and preview without spending a credit:

```sh
codexswap reset                    # List the active account's available credits.
codexswap reset use --dry-run      # Show what would be redeemed without redeeming.
```

For an explicit manual redemption:

```sh
codexswap reset use                # Confirm interactively before spending.
```

`reset use` is the manual path; `reset.policy` controls automatic decisions.
`--yes` skips manual confirmation, and `--credit ID` selects a specific credit.

## Auto mode

`codexswap auto` checks accounts every 60 seconds by default. It compares the
active account's **binding usage** (the higher of its two usage percentages) with
`autoswitch.threshold`, default 80. Below the threshold it stays idle. At or above
it, it checks other enabled accounts, considers the reset policy, and then selects
a switching target if no reset is chosen.

Hysteresis gives a replacement account room to work: its usage must be at most
`threshold - hysteresisPct`, or 70% with the defaults. The `best` strategy chooses
the lowest usage, breaking ties by lowest slot number. `next-available` walks slot
order from the current account and wraps around. Unknown usage is eligible but
ranks last under `best`.

After either a switch or a redemption, the default 300-second cooldown pauses
decisions. If the active account's probe fails, the daemon waits for 3 consecutive
failed ticks before treating it as unusable and trying selection. A successful
probe clears that failure count. A single timeout therefore does not cause an
immediate switch. Disabling `autoswitch.enabled` disables these decisions.

For example, with defaults, account 1 has 30% 5-hour usage and 84% weekly usage;
account 2 has 40% binding usage. If account 1's earliest credit expires in 10 days,
the default policy preserves it, and account 2 qualifies because 40% is at most
70%. Auto mode switches to account 2 and waits 5 minutes. If that credit instead
expires in 2 days and the daily cap is still available, auto mode redeems it on
account 1 first and then waits 5 minutes. If account 2 were at 75%, it would not
qualify as a switching target despite being below 80%.

Preview a single tick:

```sh
codexswap auto --once --dry-run
```

Dry runs perform reads and decisions, but neither switch accounts nor redeem
credits. An automatic switch changes the live credentials; it does not promise
to migrate an already-running Codex session. The running-process guard still
applies to switching.

**Linux: systemd user service.** Run this in Bash after installing `codexswap` and
Codex. It writes a user unit with the executable paths and PATH from your current
shell, then starts it and enables it for future user sessions:

```bash
(
  set -eu
  codexswap_bin=$(command -v codexswap)
  codex_bin=${CODEX_BIN:-$(command -v codex)}
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/codexswap.service" <<EOF
[Unit]
Description=Codex account switching and reset-credit monitor

[Service]
Type=simple
ExecStart="$codexswap_bin" auto
Environment="CODEX_BIN=$codex_bin"
Environment="PATH=$PATH"
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now codexswap.service
)
```

This uses the default `~/.codexswap` data root. If you use `CODEXSWAP_HOME`, add its
absolute path as an `Environment=` entry in the unit. The service normally follows
your user session; running after logout requires systemd user lingering to be
enabled. Inspect output with `journalctl --user -u codexswap.service`; stop it with
`systemctl --user disable --now codexswap.service`.

**Windows: Task Scheduler.** Open an elevated **Command Prompt** as the same user
who added the accounts, with `codexswap.exe` on PATH. This one-liner resolves the
installed executable's full path and registers a task at login, running with that
user's limited privileges. It is Command Prompt syntax, not PowerShell syntax.
See Microsoft's [schtasks reference](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-create).

```bat
for %I in (codexswap.exe) do schtasks /Create /TN "codexswap" /SC ONLOGON /TR "\"%~$PATH:I\" auto" /RL LIMITED /IT /F
```

Ensure Codex is on your Windows user PATH, or set `CODEX_BIN` as a persistent user
environment variable. Persist `CODEXSWAP_HOME` there too if you use a custom root;
the task does not inherit temporary variables from a terminal. In Task Scheduler,
open the task's **Settings** and clear **Stop the task if it runs longer than** so
the daemon can keep running; review the battery conditions if using a laptop.
Start it immediately with `schtasks /Run /TN codexswap`, or wait until your next
login. Stop it with `schtasks /End /TN codexswap` and disable future runs with
`schtasks /Change /TN codexswap /Disable`.

## Configuration

Settings live in `settings.json` under the data root, stored as nested objects.
Only overrides are stored; `config` shows effective values and marks defaults.

```sh
codexswap config
codexswap config set KEY VALUE
```

Replace `KEY` and `VALUE` with a setting and value below, for example:

```sh
codexswap config set autoswitch.threshold 85
codexswap config unset autoswitch.threshold
```

| Key | Type | Default | Valid values | Meaning |
| --- | --- | --- | --- | --- |
| `autoswitch.enabled` | bool | `true` | `true`, `false` | Enable automatic switching and reset decisions. |
| `autoswitch.threshold` | int | `80` | 1..100 | Binding usage percentage at which auto mode considers action. |
| `autoswitch.intervalSeconds` | int | `60` | 10..3600 | Seconds between monitoring ticks. |
| `autoswitch.cooldownSeconds` | int | `300` | 0..86400 | Pause after a switch or redemption. |
| `autoswitch.hysteresisPct` | int | `10` | 0..50 | Percentage points below threshold required for a target with known usage. |
| `autoswitch.strategy` | str | `best` | `best`, `next-available` | Choose the least-used eligible account or the next eligible slot. |
| `autoswitch.unhealthyTicks` | int | `3` | 1..20 | Consecutive active-account probe failures before trying another account. |
| `reset.policy` | str | `expiring` | `never`, `expiring`, `exhausted`, `always` | Decide when auto mode may spend a banked reset. |
| `reset.expiryDays` | int | `3` | 0..30 | Expiry horizon in days for the `expiring` policy. |
| `reset.minUsagePercent` | int | `50` | 0..100 | Minimum binding usage for any policy-driven redemption. |
| `reset.maxPerDay` | int | `1` | 0..10 | Automatic redemption cap across accounts per rolling 24 hours; 0 means uncapped. |
| `probe.timeoutSeconds` | int | `45` | 5..300 | Time allowed for a Codex app-server probe. |
| `probe.staleSeconds` | int | `120` | 0..86400 | Maximum age of cached usage reused without a fresh probe. |
| `probe.allowBackendFallback` | bool | `false` | `true`, `false` | Opt in to unsupported backend calls when the app-server path fails. |
| `ui.color` | str | `auto` | `auto`, `always`, `never` | Select terminal colour behaviour; `NO_COLOR` is respected. |

Boolean values also accept `1`/`0`, `yes`/`no`, and `on`/`off`, case-insensitively.
Unknown keys and values outside the allowed choices or bounds are rejected.

## JSON output for scripting

```sh
codexswap list --json
```

Example with one saved account:

```json
{
  "activeSlot": 1,
  "accounts": [
    {
      "slot": 1,
      "email": "person@example.com",
      "alias": "personal",
      "disabled": false,
      "planType": "pro",
      "health": "ok",
      "usage": {
        "bindingPercent": 84.0,
        "primary": {
          "usedPercent": 12,
          "windowMinutes": 300,
          "resetsAt": 1788912388
        },
        "secondary": {
          "usedPercent": 84,
          "windowMinutes": 10080,
          "resetsAt": 1789435573
        },
        "resetCreditsAvailable": 3,
        "fetchedAt": 1788908400.0,
        "stale": false
      }
    }
  ]
}
```

`--json` emits **one JSON object and nothing else to stdout**; diagnostic messages
go to stderr. Timestamps are Unix seconds, and window lengths are minutes. Use
`windowMinutes` to identify a window: `primary` is not necessarily the 5-hour
window. Missing windows may be `null`.

`list` probes enabled accounts with up to four workers and reuses fresh cached
snapshots. A failed probe falls back to cached usage marked `stale` instead of
aborting the list. `--no-probe` skips fresh probes. API-key accounts are valid
accounts but have no usage data.

## Comparison with existing tools

The table reflects the linked projects' documentation reviewed on 2026-09-09.
Automatic switching means unattended switching in response to usage, rather than
manual account selection or automatic warm-up. Where an OS support matrix is not
documented, the table says so.

| Tool | Automatic switching | Usage display | Reset-credit management | Platform |
| --- | --- | --- | --- | --- |
| **codexswap** | Yes, threshold and health monitoring | Yes | Listing, manual redemption, and automatic policies | Windows, macOS, Linux; Python CLI |
| [codex-switch (PyPI)](https://pypi.org/project/codex-switch/) | No; manual switching | Yes | Not documented | Python CLI; OS matrix unspecified |
| [codex-switcher (Lampese)](https://github.com/Lampese/codex-switcher) | No usage-driven switching documented | Yes | Credit count and expiry display; no automatic redemption documented | Windows, macOS, Linux; desktop app |
| [codex-auth-snap (enerai)](https://github.com/enerai/codex-auth-snap) | No; manual snapshots | No | No | Windows (PowerShell), macOS and Linux (Bash) |
| [codex-auth (Loongphy)](https://github.com/Loongphy/codex-auth) | Manual with the standard Codex CLI; separate `codext` integration offers automatic switching | Yes | Not documented | Windows, macOS, Linux; CLI |
| [codex-profiles (midhunmonachan)](https://github.com/midhunmonachan/codex-profiles) | No; manual profiles | Not documented | Not documented | Rust CLI with npm/Bun installers; OS matrix unspecified |
| [codex-reset (aaamosh)](https://github.com/aaamosh/codex-reset) | No account switching | Yes | Manual listing and redemption through undocumented endpoints | Python CLI; Unix-style installation documented |

The projects cover different needs: local snapshots, desktop controls, named
profiles, or explicit reset redemption. `codexswap` combines account switching
with policy-driven use of banked reset credits.

[claude-swap (`cswap`)](https://github.com/realiti4/claude-swap), the multi-account
switcher for Claude Code, inspired this project.
[codex-reset](https://github.com/aaamosh/codex-reset) is prior art for redeeming
Codex reset credits from the command line.

## Security

Credentials remain on your machine for storage and account switching; they are
never uploaded to a codexswap service. Normal authentication with OpenAI is handled
by the Codex CLI. `codexswap` makes no network calls of its own by default: it
communicates with the local `codex app-server` process, which contacts OpenAI.

The exception is the explicitly opt-in `--backend` fallback, or enabling
`probe.allowBackendFallback` to permit fallback after an app-server failure.
Those paths make authenticated requests directly to OpenAI's undocumented
`chatgpt.com/backend-api` endpoints. The fallback setting defaults to `false`;
these endpoints are unsupported and may change.

Stored credential files contain **OAuth refresh tokens** and are written with
mode **`0600` on POSIX**. Windows relies on your **user-profile ACLs**; codexswap
does not modify those ACLs. Shared state is written atomically to avoid truncated
files. Token-status output contains derived facts, not raw token values.

**Exports contain refresh tokens and must be treated as secrets.** Keep exported
files private, and never attach them or `auth.json` to an issue. Redact email
addresses from diagnostic output before sharing it.

## Troubleshooting

- **`codex CLI not found on PATH`:** Install Codex and make it available in the
  environment running codexswap, or set `CODEX_BIN` to its full executable path.
  For example, `export CODEX_BIN="/absolute/path/to/codex"` in Bash, or
  `$env:CODEX_BIN = 'C:\path\to\codex.exe'` in PowerShell. Services need their own
  persistent environment configuration.
- **A switch is refused because Codex is running:** The error lists the process
  IDs. Close those sessions and retry. To override the guard deliberately, use
  `codexswap switch 2 --force`. This does not guarantee that a running session
  reloads its credentials.
- **A probe times out on a cold `CODEX_HOME`:** Codex may be downloading its model
  cache and creating initial state. Raise the timeout, for example with
  `codexswap config set probe.timeoutSeconds 120`, and retry
  `codexswap probe 1`. The allowed maximum is 300 seconds.
- **An account shows `re-login needed`:** Sign in to that account again with
  `codex login`, then run `codexswap add`. Re-adding the same email refreshes its
  existing slot instead of creating a duplicate.
- **The same account behaves differently on two machines:** Both machines share
  that account's quota. Concurrent token refreshes can invalidate a refresh token
  stored on the other machine. Re-login and re-add the account on the affected
  machine; copying an older auth file cannot restore an invalidated token.
- **Usage is marked `stale`, or auto mode finds no target:** Stale usage means a
  probe failed and cached data is being shown. Check `CODEX_BIN`, connectivity,
  and login health. For selection, check enabled accounts and hysteresis: with
  defaults, a known-usage target must be at or below 70%, not merely below 80%.
- **No usage appears for an API-key account:** API-key accounts are supported,
  but usage data is unavailable for them. Missing usage does not make the account
  registration invalid.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and checks.
[CONTRACT.md](CONTRACT.md) is the specification; behaviour changes must update it
in the same commit. Report reproducible problems through the
[issue tracker](https://github.com/cateyelow/codexswap/issues), without credentials
or unredacted email addresses.

## License (MIT)

Released under the [MIT License](LICENSE).
