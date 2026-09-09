# codexswap — implementation contract (single source of truth)

`codexswap` is a multi-account switcher for the **OpenAI Codex CLI**, modelled on
`claude-swap` (`cswap`) for Claude Code, plus a Codex-only feature: management and
policy-driven redemption of **banked rate-limit reset credits**.

Every module below is implemented against THIS document. Do not invent new shared
types, rename fields, or change signatures. If something here is ambiguous, choose
the simplest behaviour consistent with the rest of the document and add a short
`# CONTRACT:` comment explaining the choice.

---

## 0. Hard constraints

- **Python 3.9+**, **standard library only** at runtime (no requests, no rich, no psutil,
  no pydantic). `pytest` and `ruff` are dev-only.
- Must work on **Windows, macOS and Linux**. Windows is a first-class target: this is
  developed on Windows 11 with Python 3.11 and Git Bash.
- `from __future__ import annotations` at the top of every module (so `X | None`
  annotations work on 3.9). Prefer `typing.Optional` / `typing.Tuple` in runtime-evaluated
  positions such as dataclass fields on 3.9.
- No network calls in `appserver.py` (it shells out to the Codex CLI). `backend.py` is
  the only module that may use `urllib.request`.
- Never log, print, or serialise token material, **including prefixes**.
  `--token-status` prints *derived* facts (expiry, source), never any part of a
  token. Credential storage and the explicit credential export format are the only
  exceptions to serialisation. Text the program did not write, a subprocess stderr
  or an HTTP error body, goes through `redaction` before it is quoted: exact
  replacement of known secrets first, then the regex patterns each module keeps,
  then a windowed fragment scan that also sees whitespace-split, percent-encoded
  and JSON-escaped forms. Redact **before** truncating, never after; cutting first
  leaves half a token that nothing can then recognise. A token re-encoded whole
  (base64, say) is a documented gap, as is a fragment shorter than nine characters.
- `backend` follows no redirect that leaves the origin it sent the request to, and
  no https-to-http downgrade. urllib copies `Authorization` onto redirects.
- Files containing credentials are written with mode `0o600` on POSIX. On Windows,
  `os.chmod` is mostly a no-op; that is accepted, do not attempt ACL surgery.
- Every write to a shared *document* goes through `paths.atomic_write_*` (temp file in
  the same directory + `os.replace`) so a crash cannot truncate state: the registry,
  settings, the usage cache, auto state, slot `auth.json` and `config.toml`. Two files
  are deliberately written in place because they are not documents: the auto log is
  appended line by line (and rewritten atomically only when it is trimmed), and the
  lock file is opened and byte-locked, never rewritten.
- **Fail closed on anything irreversible.** Losing the live credential and spending a
  reset credit cannot be undone, so when the program cannot establish that an action is
  safe it refuses instead of guessing:
  - a credential is validated (`identity.validate_auth`) before it is written anywhere;
  - a settings value that fails validation is reported as its default, and
    `reset.policy` in `Settings.invalid` disables automatic redemption entirely;
  - failed process discovery returns `None`, not "nothing is running", and a
    command that succeeds while producing nothing parseable counts as failure;
  - sync-back keeps the newer of two credential copies rather than overwriting
    blindly, and refuses to write a live file that could not authenticate;
  - an unreadable `settings.json` or `state.json` is not an empty one: the first
    marks every key invalid, the second refuses to reserve a redemption;
  - a probe that failed says nothing about that account headroom, so it cannot be
    the evidence that every account is exhausted;
  - a reading whose `accountId` disagrees with the slot registered identity is
    discarded, not cached (section 3.4).

---

## 1. Verified facts about Codex (measured on codex-cli 0.153.4, 2026-09-09)

These were verified empirically. Implement against them.

### 1.1 `~/.codex/auth.json`

```json
{
  "auth_mode": "chatgpt",
  "OPENAI_API_KEY": null,
  "tokens": {
    "id_token": "<JWT>",
    "access_token": "<JWT>",
    "refresh_token": "rt.1.AAD...",
    "account_id": "109355fb-405e-4d1a-a9c0-670a91fb2c12"
  },
  "last_refresh": "2026-09-08T10:57:36.966926600Z"
}
```

`auth_mode` may also be `apikey`, in which case `OPENAI_API_KEY` is set and `tokens`
may be absent or null. Treat api-key accounts as valid accounts with no usage data.

### 1.2 `id_token` JWT payload (base64url, **no signature verification**)

```
email                                    "person@example.com"
name                                     "A Person"
exp, iat                                 unix seconds
https://api.openai.com/auth              {
                                           "chatgpt_account_id": "109355fb-...",
                                           "chatgpt_plan_type": "pro",
                                           "chatgpt_subscription_active_start": "2026-05-31T02:50:31+00:00",
                                           "chatgpt_subscription_active_until": "..."
                                         }
```

The `access_token` payload additionally has `https://api.openai.com/profile.email` and
its own `exp`. Use the id_token for identity, the access_token `exp` for health.

### 1.3 `CODEX_HOME`

The Codex CLI honours the `CODEX_HOME` environment variable (default `~/.codex`) for
**all** of its state, including `auth.json`. Verified: pointing `CODEX_HOME` at a
directory that contains only `auth.json` lets `codex app-server` authenticate as that
account and leaves the real `~/.codex` untouched. Codex will populate that directory
with its own caches (`models_cache.json`, several sqlite files, `installation_id`);
that is expected and harmless.

**This is the foundation of codexswap**: each slot owns a directory that is a complete
`CODEX_HOME`. Token refreshes performed by Codex land back in the slot automatically.

Each slot also owns its own `config.toml`. Account capture, API-key registration,
and import seed a missing config from the CURRENT `paths.codex_home()/config.toml`.
An absent source is harmless. An existing slot config (including on re-add or forced
import) is preserved. UTF-8 source bytes, including line endings, are preserved;
writes are atomic and use mode `0600` because MCP configuration can hold credentials.
Config is never copied back to the live home by switching or running a slot.
Slot moves/swaps carry the entire home, including config. Explicit removal/purge
still removes the home as before; config seeding and synchronisation never delete it.

### 1.4 `codex app-server` JSON-RPC (stdio, newline-delimited JSON)

Handshake:

```json
{"id":1,"method":"initialize","params":{"clientInfo":{"name":"codexswap","version":"0.1.0"}}}
{"method":"initialized","params":null}
```

Then request `{"id":2,"method":"account/rateLimits/read"}` (params must be omitted or
`null`). Verified response shape:

```json
{"id":2,"result":{
  "rateLimits":{
    "limitId":"codex","limitName":null,
    "primary":{"usedPercent":84,"windowDurationMins":10080,"resetsAt":1789435573},
    "secondary":null,
    "credits":{"hasCredits":false,"unlimited":false,"balance":"0"},
    "individualLimit":null,"spendControlReached":false,
    "planType":"pro","rateLimitReachedType":null},
  "rateLimitsByLimitId":{
    "codex":{"...same shape, plus limitId/limitName...":null},
    "codex_bengalfox":{"limitId":"codex_bengalfox","limitName":"GPT-5.3-Codex-Spark",
      "primary":{"usedPercent":0,"windowDurationMins":300,"resetsAt":1788912388},
      "secondary":{"usedPercent":0,"windowDurationMins":10080,"resetsAt":1789499188},
      "credits":null,"planType":"pro"}},
  "rateLimitResetCredits":{
    "availableCount":3,
    "credits":[{"id":"RateLimitResetCredit_<opaque>","resetType":"codexRateLimits",
                "status":"available","grantedAt":1787358028,"expiresAt":1789950028,
                "title":"Full reset",
                "description":"Thanks for using Codex! ..."}]},
  "accountId":"109355fb-...",
  "rateLimitUpsell":null}}
```

Any of `rateLimits`, `secondary`, `credits`, `rateLimitResetCredits`,
`rateLimitsByLimitId` may be `null` or missing. Parse defensively.

Redemption request:

```json
{"id":3,"method":"account/rateLimitResetCredit/consume",
 "params":{"idempotencyKey":"<uuid4>","creditId":"<optional, omit to let backend pick>"}}
```

Outcome strings (from the binary enum): `reset`, `nothingToReset`, `noCredit`,
`alreadyRedeemed`. The result object contains an `outcome` field; accept both
`result.outcome` and a bare string result. **Redeeming a credit resets both the 5-hour
and the weekly window to 0% and is irreversible.**

Locating the binary: `codex` on PATH (`codex.exe` on Windows). Honour `$CODEX_BIN` as
an override. On Windows the npm shim is a `.cmd` script, so subprocess calls must not
set `shell=True`; resolve with `shutil.which("codex")` and fall back to
`shutil.which("codex.exe")`, then to the npm-installed native binary if discoverable.

### 1.5 Undocumented backend endpoints (fallback only)

Base `https://chatgpt.com/backend-api`, headers
`Authorization: Bearer <access_token>` and `ChatGPT-Account-Id: <account_id>`.

- `GET  /wham/rate-limit-reset-credits`
- `POST /wham/rate-limit-reset-credits/consume`  body `{"credit_id":..., "redeem_request_id":...}`
- `GET  /wham/usage`

These are reverse-engineered and unsupported by OpenAI. They are used **only** when
the user passes `--backend`, or when the app-server path fails and
`probe.allowBackendFallback` is true (default **false**). Every code path that uses
them must be reachable only through those two switches.

`backend.probe_usage(codex_home, *, timeout: float = 20.0)` is the fallback that `cli._probe_all`
calls after an app-server probe fails, and only under one of those two switches. It
reads the slot's own `auth.json` for the access token and account id. `/wham/usage`
is undocumented and its shape unverified, so an unrecognised payload yields **unknown
usage** rather than invented numbers. Reset credits are kept when the usage response
already carried a list this code recognises; only when it did not does a second
request go to `/wham/rate-limit-reset-credits`, whose shape this section does record.
`backend.consume_reset_credit` accepts only the four documented outcomes, as
`appserver` does.

---

## 2. Storage layout

Root is `$CODEXSWAP_HOME` if set, else `~/.codexswap`.

```
<root>/
  settings.json          # user settings (section 5)
  accounts.json          # account registry (section 2.1)
  mappings.json          # directory -> slot map
  state.json             # auto-daemon state
  usage-cache.json       # last usage snapshot per slot
  codexswap.log          # append log, truncated when it exceeds 2 MB
  homes/<slot>/          # a complete CODEX_HOME per slot
  homes/<slot>/auth.json # authoritative credential for that slot
  homes/<slot>/config.toml # optional account-specific Codex configuration
  unclaimed/<id>.json    # a credential a switch rescued (section 3.5)
  .lock                  # advisory lock file
```

### 2.1 `accounts.json`

```json
{
  "version": 1,
  "activeSlot": 1,
  "accounts": [
    {
      "slot": 1,
      "email": "a@example.com",
      "name": "A Person",
      "accountId": "109355fb-...",
      "planType": "pro",
      "authMode": "chatgpt",
      "subscriptionActiveUntil": "2027-05-31T02:50:31+00:00",
      "alias": "main",
      "disabled": false,
      "addedAt": "2026-09-09T03:00:00Z",
      "lastSwitchedAt": null,
      "lastSeenAt": 1789435573.0,
      "lastSeenUsage": {}
    }
  ]
}
```

A missing file means "no accounts yet" and must not raise. A corrupt file is renamed
to `accounts.json.corrupt-<timestamp>` and treated as empty, with a warning on stderr.

---

## 3. Shared types (`src/codexswap/models.py`)

All dataclasses. Every type gets `to_dict()` and a `from_dict()` classmethod using the
exact camelCase keys shown in section 2.1 and the app-server payload. `from_dict` must
tolerate missing and `None` fields.

```python
@dataclass(frozen=True)
class AccountIdentity:
    email: Optional[str]
    name: Optional[str]
    account_id: Optional[str]
    plan_type: Optional[str]
    auth_mode: str                      # "chatgpt" | "apikey" | "unknown"
    subscription_active_until: Optional[str]
    access_token_exp: Optional[int]     # unix seconds
    id_token_exp: Optional[int]
    def label(self) -> str              # email, else account_id[:8], else "unknown"

@dataclass(frozen=True)
class RateLimitWindow:
    used_percent: float
    window_minutes: int
    resets_at: Optional[int]            # unix seconds
    def seconds_until_reset(self, now: float) -> Optional[float]

@dataclass(frozen=True)
class ResetCredit:
    id: str
    reset_type: Optional[str]
    status: str                         # "available" | "redeeming" | "redeemed"
    granted_at: Optional[int]
    expires_at: Optional[int]
    title: Optional[str]
    description: Optional[str]
    @property
    def is_available(self) -> bool      # status == "available"
    def days_until_expiry(self, now: float) -> Optional[float]

@dataclass(frozen=True)
class PerLimitUsage:
    limit_id: str
    limit_name: Optional[str]
    primary: Optional[RateLimitWindow]
    secondary: Optional[RateLimitWindow]
    plan_type: Optional[str]

@dataclass(frozen=True)
class UsageSnapshot:
    fetched_at: float
    account_id: Optional[str]
    plan_type: Optional[str]
    primary: Optional[RateLimitWindow]
    secondary: Optional[RateLimitWindow]
    has_credits: bool
    credits_balance: Optional[str]
    reset_credits: Tuple[ResetCredit, ...]
    per_limit: Tuple[PerLimitUsage, ...]
    @property
    def binding_percent(self) -> Optional[float]
        # max(used_percent) over primary and secondary; None if both absent
    @property
    def available_reset_credits(self) -> Tuple[ResetCredit, ...]
    @property
    def available_reset_count(self) -> int
    def soonest_expiring_credit(self) -> Optional[ResetCredit]

@dataclass
class Account:
    slot: int
    identity: AccountIdentity
    alias: Optional[str] = None
    disabled: bool = False
    added_at: str = ""                  # ISO8601 Z
    last_switched_at: Optional[str] = None
    last_seen_at: Optional[float] = None
    last_seen_usage: Optional[UsageSnapshot] = None
    def display(self) -> str            # "main (a@example.com)" or "a@example.com"
    def matches(self, ref: str) -> bool # slot number as str, email (case-insensitive), or alias
```

Module constants: `HEALTH_OK = "ok"`, `HEALTH_EXPIRED = "expired"`,
`HEALTH_UNKNOWN = "unknown"`.

### 3.1 What health means

Health has exactly one offline question behind it: does the stored credential parse?

- `identity.health_of(identity, *, now)` returns **only** `HEALTH_OK` or `HEALTH_UNKNOWN`.
  `HEALTH_UNKNOWN` means the token set is missing or unreadable. An elapsed
  `access_token_exp` is still `HEALTH_OK`, because Codex refreshes that token on demand.
- `HEALTH_EXPIRED` means a live call actually rejected the credential. That cannot be
  judged from a file, so it is only ever attached by a caller that saw `errors.AuthExpired`
  from a probe. `cli._probe_all` collects those slots into an `auth_failed` set and passes
  it to `render.render_accounts` / `render.render_status` and into the `--json` `health`
  field.
- `identity.subscription_lapsed(identity, *, now)` exists but is **informational only**.
  The `chatgpt_subscription_active_until` claim records the billing period that was current
  when the token was issued, and the token is not reissued each period, so an active
  subscriber routinely carries a timestamp in the past. Deriving health from it produced a
  false "re-login needed" banner on a working Pro account, which is why it is excluded.

The re-login banner is therefore shown only for a slot in `auth_failed` (rejected) or for
`HEALTH_UNKNOWN` (unreadable), never for a healthy account with a stale billing period.

`appserver` classifies a JSON-RPC `error` whose `code` is 401 or 403, or whose message
matches the auth markers, as `errors.AuthExpired` rather than `AppServerError`, so a
credential the server actually rejected reaches `auth_failed` instead of looking like a
generic fault. Generic JSON-RPC codes (-32000 and below) are deliberately excluded.

### 3.2 Credential safety

Overwriting `auth.json` with something unusable logs the user out of Codex, and the
original is gone. Three rules keep that from happening:

- `identity.validate_auth(auth, *, source="authentication data")` raises
  `errors.AuthFileInvalid` unless the
  object carries something Codex can actually authenticate with: `tokens.refresh_token`
  or `tokens.access_token` as a non-empty string, or a non-empty `OPENAI_API_KEY`.
  `identity.load_auth` only proves the file holds a JSON object, which `{}` and
  `{"hello": 1}` also satisfy. **Every** path that writes a credential validates
  first: `AccountStore.add_from_auth` (so `add`, `add-token` and `capture_current`
  are covered at one place), `switcher.capture_current` before it takes the lock,
  `switcher.sync_live_to_slot` before it writes a slot, `switcher.activate` before
  touching the live home, and `transfer.import_accounts` during its validation pass.
  Two of these matter most: re-adding an account matches an existing slot by identity
  and overwrites it, and sync-back writes the live file into a slot, so an unusable
  live file could destroy the last working copy of that account's credential.
- `switcher.detect_running_codex() -> Optional[List[ProcessInfo]]` returns `None` when
  discovery itself failed (missing tool, timeout, malformed output). `activate` treats
  `None` as a refusal requiring `--force`; reporting an empty list would let a switch
  overwrite the credential a live Codex still holds. A command that *succeeds* but
  produces nothing parseable counts as failure: `ps` and `tasklist` always list at
  least themselves, so zero readable rows means the output was not a process listing.
  Zero *Codex* rows in a readable listing is still an empty list.
- `switcher.sync_live_to_slot` compares Codex's `last_refresh` stamp on both copies and
  keeps the newer one. A probe refreshes a slot's own credential, and refresh tokens
  rotate, so blindly copying an older live file over a newer slot file can void the
  account. When either stamp is missing or unreadable the live copy wins, as before.

### 3.5 Nothing is overwritten that nothing else holds

`activate` writes the target's credential over `~/.codex/auth.json`. If the file it
replaces belongs to no slot, that switch is the end of that login: an OAuth
authorisation code is single-use, so getting it back means authorising the account
again from scratch. Refusing the switch would protect the credential and block the
user; `switcher.rescue_unregistered_live` does neither, and copies it aside instead.

- Called from `activate` immediately before the overwrite, holding the lock, so the
  window in which Codex could finish writing a login this check did not see is as
  small as it can be made. It is not skipped by `--force`, which is about a running
  Codex, and not by `auto`, which switches unattended.
- `switcher.slot_owning(store, auth)` decides whether a copy already exists, and
  confirms every match against the slot's own `auth.json` rather than the registry's
  recorded identity: the registry is a cache that drifts, the file is what would
  survive. In order: identical bytes; the same `account_id` when both sides have one;
  the same `OPENAI_API_KEY`; and finally the same email, but only when *neither* side
  has an account id, so a slot's label can never make one account's login pass as
  another's.
- A live file that will not parse is kept as raw bytes (`unclaimed.stash_raw`). A
  half-written file and one from a Codex release this version does not understand are
  indistinguishable here, and only one of them is junk. A file that parses but carries
  no credential is not kept: there is nothing in it to lose.
- If the copy cannot be written, `activate` raises and writes nothing. Failing to
  preserve is the one condition that must stop the switch.
- Entries are `<root>/unclaimed/<id>.json`, mode `0600`, in a `0700` directory. `<id>`
  leads with a compacted UTC timestamp so a plain sort is chronological. Each holds
  the credential under `auth` (or the bytes under `raw`) plus `stashedAt`, `reason`,
  `email`, `accountId`, `planType`, `authMode` and a SHA-256 `fingerprint`. The
  fingerprint deduplicates: a live file something outside this tool keeps rewriting
  produces one entry, not one per switch.
- `codexswap unclaimed` lists them and never reads `auth`. `--claim ID` registers one
  through `AccountStore.add_from_auth` and drops the copy only after that succeeds;
  `--purge ID` deletes one. A `raw` entry cannot be claimed. An id that is not a bare
  filename is refused, so `--purge` cannot name a file outside the directory.
- `doctor` reports a `unclaimed` check: `warn` while any are waiting, naming the ids.
  A rescue is otherwise one line of output on a command the user ran for another
  reason.

### 3.3 Registry consistency

`AccountStore.load()` runs outside the lock, so an in-memory registry is a snapshot
that another process can invalidate. It also renames a corrupt registry aside and
carries on with an empty one -- except for `load(quarantine=False)`, which reports the
corruption and leaves the file alone. `auto --dry-run` uses that: renaming a file is a
write, and a dry run promises none. `AccountStore.reload()` re-reads it inside the
lock, refreshing existing `Account` objects in place so a caller holding one keeps a
live view. Every mutating method calls it first, as do `switcher.activate`,
`switcher.sync_live_to_slot` and `transfer.import_accounts`, which hold the lock
themselves. Without it, two `codexswap add` runs both allocate slot 1 and the second
save erases the first account and its credential.

Two references must never silently select the wrong account:

- `AccountStore._check_alias` refuses an alias another slot already uses
  (case-insensitively) and every writer calls it: `set_alias`, `add_from_auth` for
  both the new-slot and the re-add branch, and `add_token`. Re-adding with an alias
  renames the slot; re-adding without one keeps the alias the slot has.
- `resolve` raises `UserError` listing the candidates when a reference matches more
  than one account, for an alias, for an exact email, or for an email prefix.
  `import` deliberately preserves every entry rather than merging by identity, so one
  email really can name two slots, and choosing the first would act on an account the
  user did not name.
- A directory mapping is a promise that `run` in that directory uses *that account*,
  so `remove`, `move_slot` and `swap_slots` move or drop its mappings. Left behind,
  the mapping would hand the directory to whichever account reuses the slot.

### 3.4 The slot home must hold the account the registry names

A probe reads whatever credential is in the slot home, so the registry entry is a
label that can be wrong: a hand-copied `auth.json`, a restored backup, or a login
performed directly into a slot home all leave the label pointing at someone else.

`UsageSnapshot.describes(identity)` compares the probed `accountId` with the
identity the **registry** records for that slot. The probe reads the slot's
`auth.json`, so a disagreement means the registry label and the stored credential
have come apart, which is the thing worth catching. It is false only when both ids
are known and differ, so API-key accounts and caches predating the field still match.
`doctor`'s `slot.N.identity` check compares the same two things directly, without a
probe, and is what the refusals below tell the user to run.

Every place a reading is bound to a slot enforces it:

- `cli._probe_all` and `cli._backend_fallback` discard a mismatched reading instead
  of caching it, and record the slot in `mismatched`.
- `cli._cached` refuses a mismatched cache entry, so a file written by an older
  build cannot show one account's usage under another's name.
- `list`, `status` and `probe` print one warning to stderr naming the slots and
  continue; reading is harmless and `--json` keeps stdout parseable.
- `reset list` and `reset use` raise `UserError`. Redemption is irreversible and
  would spend the credit of whoever is actually in that slot home.
- `auto._AutoAccounts.probe` raises `AppServerError`, so the daemon counts the slot
  as an unhealthy probe rather than deciding anything about it.

---

## 4. Errors (`src/codexswap/errors.py`)

```python
class CodexSwapError(Exception):           exit_code = 1
class UserError(CodexSwapError):           exit_code = 2
class AccountNotFound(UserError)
class NoAccountsConfigured(UserError)
class SlotInUse(UserError)
class CodexRunning(UserError)
class AuthFileMissing(CodexSwapError)
class AuthFileInvalid(CodexSwapError)
class AppServerError(CodexSwapError):      exit_code = 3
class AppServerTimeout(AppServerError)
class CodexBinaryNotFound(CodexSwapError): exit_code = 3
class AuthExpired(CodexSwapError):         exit_code = 4
class BackendError(CodexSwapError):        exit_code = 5
class LockBusy(CodexSwapError):            exit_code = 6
```

`cli.main` catches `CodexSwapError`, prints `error: {msg}` to stderr, returns
`exc.exit_code`. Unexpected exceptions print a short message plus
`run with --debug for a traceback`.

---

## 5. Settings (`src/codexswap/settings.py`)

Dotted keys, stored in `settings.json` as a nested dict. Only keys present in the file
are non-default. `config` prints `key  value  (default)` aligned.

| key | type | default | validation |
|---|---|---|---|
| `autoswitch.enabled` | bool | `true` | |
| `autoswitch.threshold` | int | `80` | 1..100 |
| `autoswitch.intervalSeconds` | int | `60` | 10..3600 |
| `autoswitch.cooldownSeconds` | int | `300` | 0..86400 |
| `autoswitch.hysteresisPct` | int | `10` | 0..50 |
| `autoswitch.strategy` | str | `best` | `best` or `next-available` |
| `autoswitch.unhealthyTicks` | int | `3` | 1..20 |
| `autoswitch.model` | str | `""` | any comma separated list |
| `reset.policy` | str | `expiring` | `never`, `expiring`, `exhausted`, `always` |
| `reset.expiryDays` | int | `3` | 0..30 |
| `reset.minUsagePercent` | int | `50` | 0..100 |
| `reset.maxPerDay` | int | `1` | 0..10 |
| `probe.timeoutSeconds` | int | `45` | 5..300 |
| `probe.staleSeconds` | int | `120` | 0..86400 |
| `probe.allowBackendFallback` | bool | `false` | |
| `ui.color` | str | `auto` | `auto`, `always`, `never` |

API:

```python
SPECS: Dict[str, SettingSpec]           # ordered as in the table above

@dataclass(frozen=True)
class SettingSpec:
    key: str
    type: str                           # "bool" | "int" | "str"
    default: Any
    choices: Optional[Tuple[str, ...]]
    minimum: Optional[int]
    maximum: Optional[int]
    help: str

def validate(spec: SettingSpec, value: Any) -> bool   # is a stored JSON value usable?

class Settings:
    invalid: Dict[str, Any]                        # stored values that failed validate()
    @classmethod
    def load(cls, root: Optional[Path] = None) -> "Settings"
    def get(self, key: str) -> Any                 # UserError on unknown key
    def is_default(self, key: str) -> bool
    def set(self, key: str, raw: str) -> Any       # parse+validate, UserError on bad value
    def unset(self, key: str) -> None
    def items(self) -> List[Tuple[str, Any, bool]] # (key, value, is_default)
    def save(self) -> None                         # under the root FileLock
```

Bools parse from `true/false/1/0/yes/no/on/off` case-insensitively.

`load()` validates every stored value against its spec, because `settings.json` can be
hand-edited or written by an older release and these values reach decisions that spend
reset credits. A value that fails validation:

- is **not** an override: `get()` returns the documented default and `is_default()` is
  `True`, so `"false"` is not truthy and `maxPerDay: -1` does not remove the cap;
- is recorded in `Settings.invalid` (key to rejected value) and named in one stderr
  warning: `warning: ignoring invalid settings, using defaults: <keys>`;
- is left in the file, so `save()` round-trips it until the user corrects that key,
  and `unset(key)` removes it from the file as well as from the overrides. Without
  that removal `config unset` reports the default and then writes the bad value
  straight back, so the key could never be cleared.

A document that cannot be read at all is treated the same way, only for every key:
unparseable JSON, a top-level value that is not an object, and a section written as
something other than an object all set `invalid` for the keys they cover. An
unreadable file is not an unset file, and reporting bare defaults would let it
authorise a redemption under a policy the user never wrote. A **missing** file is
genuinely unset and leaves `invalid` empty.

`Settings.invalid` exists so that an irreversible action can distinguish "unset" from
"set to nonsense": see rule 0 in section 6.

---

## 6. Reset-credit policy (`src/codexswap/resets.py`)

This is the feature that distinguishes codexswap. A banked reset restores **both** the
5h and weekly windows to 0%, expires 30 days after it is granted, and cannot be
undone. Wasting one on a lightly used account is the main failure mode to avoid.

```python
@dataclass(frozen=True)
class ResetDecision:
    should_redeem: bool
    credit: Optional[ResetCredit]
    reason: str          # machine-ish reason, e.g. "expiring-soon", "policy-never"
```

```python
def decide(
    snapshot: UsageSnapshot,
    settings: Settings,
    *,
    now: float,
    redeemed_last_24h: int,
    alternatives_available: bool,   # True if some OTHER enabled account is below threshold
) -> ResetDecision
```

Rules, evaluated in order; the first that fires wins:

0. `reset.policy` is not one of the four documented values, or appears in
   `Settings.invalid`, gives `(False, None, "policy-invalid")`. It does **not** fall back
   to the `expiring` default: redemption is irreversible, and a value this build does not
   understand is not evidence that the user wanted the default rule applied to their
   credits. `resets.POLICIES` is the tuple of accepted values.
1. `reset.policy == "never"` gives `(False, None, "policy-never")`.
2. No available credits gives `(False, None, "no-credits")`.
3. `redeemed_last_24h >= reset.maxPerDay` when `maxPerDay > 0` gives
   `(False, None, "daily-cap")`. `maxPerDay == 0` means no cap.
4. `binding_percent` is None gives `(False, None, "no-usage-data")`.
5. `binding_percent < reset.minUsagePercent` gives `(False, credit, "usage-too-low")`.
   Never burn a reset that would mostly be thrown away.
6. `policy == "expiring"`: redeem only if the soonest-expiring available credit expires
   within `reset.expiryDays` days, giving `(True, credit, "expiring-soon")`, otherwise
   `(False, credit, "not-expiring")`.
7. `policy == "exhausted"`: redeem only when `alternatives_available` is False, giving
   `(True, credit, "all-accounts-exhausted")`, otherwise
   `(False, credit, "alternatives-available")`.
8. `policy == "always"` gives `(True, credit, "policy-always")`.

The chosen credit is always `snapshot.soonest_expiring_credit()` (use-it-or-lose-it
ordering: credits with an `expires_at` sort before those without).

```python
def redeem(
    codex_home: Path, *, credit_id: Optional[str] = None,
    idempotency_key: Optional[str] = None, timeout: float = 45.0,
    expect_account_id: Optional[str] = None,
    client_factory=None,   # for tests; defaults to appserver.AppServerClient
) -> str                   # returns the outcome string
```

`expect_account_id` is the account the decision was made about. The usage read and
this call open separate app-server sessions against a directory anything can write in
between, so the slot's `auth.json` is re-read here and the redemption refused when it
names a different account -- or no account at all, which given that the caller only
supplies this when its reading named one, means the same thing.

`idempotency_key` defaults to `str(uuid.uuid4())`. Callers that retry the *same logical
attempt* must pass the same key back in.

---

## 7. Auto daemon (`src/codexswap/auto.py`)

```python
@dataclass
class AutoState:
    last_switch_at: Optional[float] = None
    cooldown_until: Optional[float] = None
    unhealthy: Dict[int, int] = field(default_factory=dict)          # slot -> consecutive failures
    redemptions: List[Dict[str, Any]] = field(default_factory=list)  # {"at":ts,"slot":n,"creditId":...}
    unreadable: bool = False                  # state.json exists but did not parse
    def to_dict(self) -> Dict[str, Any]       # `unreadable` is not serialised
    @classmethod
    def from_dict(cls, d) -> "AutoState"
    @classmethod
    def load(cls, root: Optional[Path] = None) -> "AutoState"
    def save(self, root: Optional[Path] = None) -> None   # merges `redemptions`
    def redeemed_last_24h(self, now: float) -> int
    def prune(self, now: float) -> None       # 30 days, then a 200-entry bound

@dataclass(frozen=True)
class TickResult:
    action: str        # "idle" | "switched" | "redeemed" | "cooldown" | "no-target"
                       # | "probe-failed" | "disabled" | "no-accounts"
                       # run() also emits "error" and "stopped" for its own loop
    detail: str
    slot: Optional[int] = None
```

```python
def tick(store, settings, state, *, now: float, probe, redeemer, activator,
         dry_run: bool = False, journal: Optional[RedemptionJournal] = None) -> TickResult
def run(*, once: bool = False, dry_run: bool = False, interval: Optional[int] = None,
        threshold: Optional[int] = None, models: Optional[str] = None, log=print) -> int
```

The redemption history is a shared ledger of irreversible acts, so three rules
protect it beyond the reservation lock:

- `save()` merges `redemptions` with whatever is on disk instead of overwriting it,
  preferring whichever copy knows an entry's outcome. A daemon that loaded state
  before another daemon spent a credit would otherwise erase that spend on its next
  idle tick and hand back the allowance.
- `prune()` keeps the 30-day window and a 200-entry bound, but never drops an entry
  the daily cap still counts. A burst of `noCredit` attempts would otherwise push a
  real spend out of the file; those attempts are themselves droppable, so the bound
  still holds where it matters.
- `RedemptionJournal.reserve` refuses when the history is unknown: `state.json` exists
  and did not parse, or is not a JSON object, or carries a `historyUnknownSince` stamp
  less than 24 hours old. An unreadable history is not an empty one, and spending
  against it is spending against a cap whose usage is unknown.
- `save()` writes `historyUnknownSince` into the document that replaces an unreadable
  one, keeping the later of its own stamp and the stored one. Without it the refusal
  would last exactly one tick: the daemon saves after every pass, and that save turns
  an unreadable document into a valid empty ledger. The stamp is dropped once it is
  more than 24 hours old, because by then no lost entry could still count against the
  daily cap, and a permanent refusal would be its own failure.

`probe(account) -> UsageSnapshot`, `redeemer(account, credit_id) -> str` and
`activator(account) -> None` are injected so `tick` is unit-testable with no
subprocesses. `run` wires the real implementations, handles `KeyboardInterrupt`
cleanly (exit 0), and sleeps `autoswitch.intervalSeconds` between ticks.

Tick algorithm:

1. `autoswitch.enabled` false gives `("disabled", ...)`.
2. No enabled accounts gives `("no-accounts", ...)`.
3. `now < state.cooldown_until` gives `("cooldown", ...)`.
4. Probe the active account. On failure increment `state.unhealthy[slot]`; once it
   reaches `autoswitch.unhealthyTicks`, treat the account as unusable and fall through
   to selection. Reset the counter to 0 on any success. A probe failure that has not
   yet reached the threshold returns `("probe-failed", ...)`.
5. `percent_for(autoswitch.model)` below the threshold gives `("idle", ...)`. With
   no model named that is `binding_percent`; with one it is the worse of the totals
   and the selected per-model windows. See section 8.
6. Compute `alternatives_available` by probing other enabled accounts, respecting
   `probe.staleSeconds` for cached snapshots. An account counts as headroom when its
   usage is below the threshold, when its usage is unknown, **and when its probe
   failed**: a failure says nothing about that account, and the exhausted policy must
   not spend a credit because a probe timed out. Hysteresis is not applied here; it
   is a switch-target rule.
7. Ask `resets.decide(...)`. The decision reads `binding_percent`, not the
   model-aware percent: a credit clears the account-wide windows, so an exhausted
   model must not by itself authorise spending one. If it says redeem, write a
   `"pending"` entry through `RedemptionJournal.reserve` **before** the request
   leaves, so a crash between the two counts against the cap; a reservation refused
   by another process falls through to selection. Then redeem, settle the entry with
   its outcome, and on `reset` only: drop the slot's cached usage with
   `store.forget_usage(slot)` (a redemption zeroes both windows and consumes a
   credit, so every cached number is wrong, not merely stale), set cooldown, return
   `("redeemed", ...)`. Any other outcome is logged and falls through to selection.
8. Otherwise pick a target with `strategy.pick_target(...)`. None gives
   `("no-target", ...)`.
9. Activate, set `state.last_switch_at` and `cooldown_until = now + cooldownSeconds`,
   return `("switched", ...)`.

`dry_run` performs every read and decision but calls neither `redeemer` nor
`activator`, and prefixes `detail` with `[dry-run] `. It writes nothing whatsoever:
`run` skips `state.save()`, `tick` skips `store.record_usage`, and `emit` skips the
log file, so `state.json`, `accounts.json`, `usage-cache.json` and `codexswap.log`
are all left exactly as they were. "Show me what would happen" must not itself
change what happens next, and a cached reading would let the next real tick skip
its own probe. The cost is that consecutive dry ticks each probe afresh.

`run` reloads `Settings` **and** the registry at the top of every tick, so that
`config set` and switches made elsewhere take effect in a daemon that is already
running. `--interval` / `--threshold` are reapplied after each reload, so an explicit
command-line override always outranks the file.

Hysteresis: a candidate is only a valid target when the figure selection is using is
at or below `threshold - hysteresisPct`. That figure is `binding_percent` by default,
and the highest of the named per-model limits when `autoswitch.model` or `--model` is
set. This prevents ping-ponging between two accounts that both hover at the threshold.

---

## 8. Selection strategy (`src/codexswap/strategy.py`)

```python
@dataclass(frozen=True)
class Candidate:
    account: Account
    snapshot: Optional[UsageSnapshot]
    models: Tuple[str, ...] = ()   # per-model limits to weigh; see percent_for
    @property
    def percent(self) -> Optional[float]

def pick_target(candidates, *, current_slot, strategy, threshold, hysteresis) -> Optional[Account]
def rotate_next(accounts: Sequence[Account], current_slot: Optional[int]) -> Optional[Account]
```

- `best`: among candidates that are enabled, not the current slot, and whose `percent`
  is `<= threshold - hysteresis` (unknown percent counts as eligible but ranks last),
  return the one with the lowest `percent`. Ties break by lowest slot number.
- `next-available`: rotate slot order starting after `current_slot`, wrapping, and
  return the first candidate satisfying the same eligibility test.
- `rotate_next` ignores usage entirely and is what a bare `codexswap switch` uses when
  no strategy is given.

A candidate's `percent` is `snapshot.percent_for(candidate.models)`: the aggregate
windows when no model is named, otherwise the worse of the aggregate and every
selected entry of `rateLimitsByLimitId`. Names match `limitId` or `limitName`
case-insensitively and `all` selects every reported entry; a name the account does
not report contributes nothing, so an account that never ran the model is judged on
its totals alone. Models come from `--model` when given and `autoswitch.model`
otherwise, and `--model ""` restores the totals for one run.

The reset policy deliberately keeps using `binding_percent`. A credit clears the
account-wide windows, so an exhausted model must not on its own authorise spending
one while the totals are low.

The CLI's strategy switch uses `switcher.pick_target`, which applies the above
strategy to eligible non-API accounts first, then API-key accounts if none qualify.
This includes ordinary accounts with unknown usage. Bare rotation and explicit
references retain their existing behaviour.

The `auto` command uses `auto.run`, the single daemon loop, which wraps the store
in `auto._AutoAccounts` before calling `auto.tick`. There is deliberately only one
loop: a second copy would drift, and every daemon fix would have to be made twice. API-key probes are synthetic successful snapshots with no
usage and no reset credits; no app-server/backend request is made for them and
no usage cache is retained. An API-key target is excluded while any enabled,
non-current ordinary account passes the same eligibility test. Failed ordinary
probes are ineligible, as in `auto.tick`; successful unknown usage remains eligible.
Both strategies use this priority. An active API-key account has no probe failure
counter increment and may switch to a qualifying ordinary account; if no target
exists, the result is `no-target`. Reset policy, dry runs, cooldowns, logging and
backoff retain the existing tick/daemon semantics.

---

## 9. CLI surface (`src/codexswap/cli.py`)

```
codexswap help
codexswap version
codexswap list [--json] [--token-status] [--no-probe]      (alias: ls)
codexswap status [--json]                                  (alias: current, st)
codexswap switch [<ref>] [--strategy best|next-available] [--model NAMES] [--force] [--json]
codexswap add [--slot N] [--alias NAME]
codexswap unclaimed [--claim ID [--slot N] [--alias NAME]] [--purge ID] [--json]
codexswap add-token [TOKEN|-] [--slot N] [--email EMAIL] [--alias NAME]
codexswap sync-config [<ref>] [--from PATH] [--force]
codexswap doctor [--json]
codexswap watch [--interval N]
codexswap upgrade [--yes] [--json]
codexswap remove <ref>                                     (alias: rm)
codexswap disable <ref>
codexswap enable <ref>
codexswap alias [<ref> <name> | <ref> --unset]
codexswap swap <a> <b>
codexswap move <ref> <slot>
codexswap run [<ref>] [-- <codex args>...]
codexswap map [<ref> [path]]
codexswap unmap [path]
codexswap probe [<ref>] [--backend] [--json]
codexswap reset [list] [--json]
codexswap reset use [<ref>] [--credit ID] [--yes] [--dry-run]
codexswap reset --json use ...            (--json belongs to the reset parser)
codexswap auto [--once] [--dry-run] [--interval N] [--threshold N] [--model NAMES]
codexswap config [set <KEY> <VALUE> | unset <KEY>] [--json]
codexswap export <path> [--account <ref>]
codexswap import <path> [--force]
codexswap purge [--yes]
```

Global flags: `--debug`, `--version`, `--no-color`, `--home PATH` (override
`$CODEXSWAP_HOME`). argparse also installs `-h`/`--help` on the root parser and on
every subcommand.

`<ref>` resolves in this order: exact slot number, alias (case-insensitive), email
(case-insensitive), unique email prefix. Ambiguity raises `UserError` listing the
candidates.

Behaviour notes:

- `list` probes usage for every enabled account **in parallel** (thread pool, max 4
  workers) unless `--no-probe`; cached snapshots newer than `probe.staleSeconds` are
  reused. Probe failures degrade to the cached value with a `stale` marker and never
  abort the command.
- `add` reads the live `~/.codex/auth.json`, derives identity, and copies it into the
  next free slot (or `--slot`). Re-adding an email that already exists updates that slot
  in place instead of creating a duplicate.
- `switch` with no `<ref>`, no `--strategy`, and no `--model` rotates to the next
  slot. With either flag it uses `strategy.pick_target`; `--model` alone selects with
  the saved `autoswitch.strategy`, because weighing a model only means something
  while choosing by usage. An explicit `<ref>` still wins over both. Before overwriting the live auth it
  copies the live `auth.json` back into the slot it belongs to, so token refreshes done
  by the live session are not lost.
- `switch` refuses when a Codex process is running unless `--force`, printing the pids.
- `run <ref> -- ...` runs `codex` with `CODEX_HOME` pointed at the slot home. It does
  not touch the live auth, so it is safe alongside a different active account. With no
  `<ref>` it uses the directory mapping for `os.getcwd()`.
- `reset` with no subcommand lists credits for the active account.
- `reset use` prompts for confirmation unless `--yes`; `--dry-run` shows what would be
  redeemed and exits 0 without calling the backend.
- `purge` requires `--yes` or an interactive `yes` answer; it deletes `<root>` entirely
  and never touches `~/.codex`.
- `unclaimed` with no flags lists the rescued credentials of section 3.5, oldest
  first, and reads no `auth` member. `--claim ID` registers one and drops the copy
  only after the registry holds it; `--purge ID` drops one. The two flags are mutually
  exclusive, `--slot`/`--alias` apply to `--claim` only, and each rejection is a
  `UserError` (2). An id naming a `raw` entry, or one that is not a bare filename, is
  refused.

Exit codes come from the exception table in section 4; success is 0. Six commands
depart from "nonzero means the command failed", and a script must know which:

- `run` exits with Codex's own status, whatever that is.
- `reset use` returns 1 for any outcome other than `reset`; the redemption reached the
  server and the server declined it.
- `sync-config` returns 1 when any slot was skipped or failed, having processed all of
  them.
- `doctor` returns 1 when any check failed, and `upgrade` returns 2 when it cannot
  name a manager. Both still print their `--json` object first.
- `auto --once` returns 0 after an expected error: the tick is logged as `error` and
  the daemon's contract is to keep running, not to fail.
- `Ctrl-C` outside `watch` and `auto` returns 130; inside them it is the ordinary way
  to stop and returns 0.

### Additional command behaviour

- `add-token` accepts an inline key, `-` for exactly one stdin line, or a hidden
  `getpass.getpass` prompt on a TTY. Without a token on non-TTY input it raises
  `UserError` instructing the caller to use `-`. An unavailable hidden prompt does
  not fall back to echoed input. Leading/trailing whitespace is stripped; an empty
  key raises `UserError`. There is no network validation. Argument errors and
  failures must not expose the key, including with `--debug`.
  It writes exactly `{"auth_mode":"apikey","OPENAI_API_KEY":"<key>","tokens":null}`
  to the slot auth file with mode `0600`. Next-free/positive slot rules apply;
  occupied slots and duplicate emails are rejected. Email defaults to
  `api-key-{slot}@token.local`; labels may not contain the key. Registration does
  not change the live auth or active slot; use `switch <ref>` to activate it.
  Display refreshes preserve the assigned email, and `planType`/text plan is
  `api key`. Usage is `null`/`usage unavailable`, including with cached usage or
  `--no-probe`; it is not an authentication or probe failure.
- `sync-config` copies from the live Codex home, or `--from` (a directory containing
  `config.toml`, or the config file itself). Missing/unreadable/non-UTF-8 source is
  `UserError` (2). With a ref it processes that slot; otherwise all slots, including
  disabled ones, in numeric order. It reports one line per slot: `copied`,
  `unchanged (already identical)`, `skipped (different config.toml; use --force to
  overwrite)`, `overwritten`, `failed (cannot read or write config.toml)`, or
  `failed (slot home is not inside the homes directory)`.
  Different configs require `--force`; identical configs are not rewritten even
  with force. Other slots still run after a destination error. Any skip/failure
  returns 1; complete success (including no slots with a readable source) returns 0.
- `doctor` delegates diagnostics to `doctor.collect_checks()`, returning
  `{"checks":[{"name":"...","status":"ok|warn|fail","detail":"one line"}]}`.
  Text prints `status: name: detail`. It checks version, Python/platform,
  resolved homes and writability (nearest existing parent for a missing home),
  Codex path and `--version`, app-server `initialize`, registry/account count,
  per-slot auth presence/parse/auth mode/offline health and POSIX permissions,
  per-slot identity (`slot.N.identity`: the stored credential's account ID and
  email against the registry entry, failing when either is known on both sides and
  differs, so section 3.4's refusals have a command that explains them),
  live auth matching (account ID/email or exact private API-key comparison),
  settings shape/types/ranges and unknown keys, lock, processes, and free disk.
  Each subprocess version/initialize timeout is 5 seconds; both run in a throwaway
  `CODEX_HOME`. The only app-server exchange is `initialize`/`initialized`;
  no usage read, backend call, or credit consumption is permitted.
  Diagnostics do not quarantine corrupt files or create missing homes/locks.
  A missing live auth file, no accounts, unknown setting keys, non-private POSIX
  modes, a busy lock, running sessions, a slot credential with no identity to compare,
  a missing home, or less than 100 MiB free are warnings. A **missing or unreadable
  slot** auth file is a failure, not a warning: the slot cannot be switched to.
  Invalid registry/settings/auth, a slot holding another account, unwritable homes,
  unavailable Codex/handshake, or disk inspection failure are also failures. Missing settings use defaults. Process discovery
  is best effort and reports PIDs only, never command lines. Known credentials and
  recognised token patterns are redacted from output. Exit 1 if any check fails,
  otherwise 0. JSON emits exactly one object, even for failed checks.
- `watch` refreshes the same list view immediately and then sleeps the specified
  integer interval (default 30, valid 5..3600 seconds; invalid values return 2).
  Registry and settings reload each frame; normal list caching/probe behaviour
  applies. With a TTY and enabled colour, frames start with `ESC[2J ESC[H` (without
  the separating space). Otherwise each frame has a separator and local ISO timestamp.
  Failed refreshes are retried next frame without exposing exception text.
  Ctrl+C prints a final newline and exits 0. No curses dependency.
- `upgrade` detects uv tool/pipx venv paths using `sys.prefix`, `sys.argv[0]`, and
  package location. That evidence decides on its own, including for an editable
  install inside such a venv, because the manager that owns the venv is still the one
  that can upgrade it. Only when there is no uv or pipx evidence does an installed
  package under this interpreter's site/dist-packages identify plain pip. A plain
  source checkout, and uv and pipx evidence together, are ambiguous.
  Commands are `uv tool upgrade codexswap`, `pipx upgrade codexswap`,
  or `<sys.executable> -m pip install --upgrade codexswap`. The exact quoted command
  prints before confirmation. Only `--yes`, `y`, or `yes` permits execution;
  EOF/other answers cancel with exit 0. Unknown method lists all three candidates,
  exits 2 and never runs a manager, even with `--yes`. Start failures exit 1;
  otherwise propagate the manager exit code. Use subprocess argv without a shell.
  JSON is one object with `method`, argv `command`, `executed`, and `returnCode`;
  unknown method also has a `candidates` array of printable commands. Prompts,
  explanatory messages, and manager stdout/stderr go to stderr in JSON mode.

### JSON output

On success, `--json` emits a single JSON object to stdout and nothing else. There is
no JSON error envelope: an expected failure prints `error: ...` to stderr, leaves
stdout empty, and returns the exit code from section 4. Warnings also go to stderr,
so stdout stays parseable. Shapes:

```json
{"activeSlot":1,"accounts":[{"slot":1,"email":"a@example.com","alias":null,
  "disabled":false,"planType":"pro","health":"ok",
  "usage":{"bindingPercent":84.0,
    "primary":{"usedPercent":84,"windowMinutes":10080,"resetsAt":1789435573},
    "secondary":null,"resetCreditsAvailable":3,"fetchedAt":1789.0,"stale":false}}]}
```

`status --json`, and `probe <ref> --json`, wrap one **full** account entry, the same
object `list` puts in its array, under `account`. `probe` with no `<ref>` uses the
`accounts` array shape instead, because it probes every enabled account:

```json
{"activeSlot":1,"account":{"slot":1,"email":"a@example.com","alias":null,
  "disabled":false,"planType":"pro","health":"ok","usage":null}}
```

```json
{"from":1,"to":2,"account":{"slot":2,"email":"b@example.com"}}
```

```json
{"slot":1,"availableCount":3,"credits":[{"id":"RateLimitResetCredit_example",
  "status":"available","expiresAt":1789950028,"daysUntilExpiry":11.9,
  "title":"Full reset"}]}
```

`credits` lists every credit the account holds, redeemed and expired ones included, so
a client can show history. `availableCount` counts the ones the server marked
`available`; the server is authoritative for that, and no expiry is re-derived here.
Choosing which credit to spend does apply the expiry in the same reading
(`soonest_expiring_credit(now)`), so a cached reading that has outlived a credit does
not send a request that can only fail. Credit identifiers in this
document are synthetic; a real one is an opaque handle to a specific account's credit.

```json
{"slot":1,"creditId":"RateLimitResetCredit_example","outcome":"reset","dryRun":false}
```

```json
{"unclaimed":[{"id":"20260909T011324Z-f160d6","stashedAt":"2026-09-09T01:13:24Z",
  "reason":"live login belonged to no registered account","email":"a@example.com",
  "accountId":"acct-0000","planType":"pro","authMode":"chatgpt"}]}
```

`unclaimed --claim` emits `{"claimed":<id>,"slot":N,"email":...}` and `--purge` emits
`{"purged":<id>,"email":...}`. No shape here carries the credential.

`outcome` is `null` for a preview (`"dryRun":true`) and for a redemption the user
declined at the prompt (`"dryRun":false`), so the two are told apart by `dryRun`.

`config --json` is a flat object of all 16 dotted keys mapped to their effective
typed values, after any `set`/`unset` in the same invocation. It carries no nesting
and no default/override marker; `config` without `--json` prints that marker.

```json
{"autoswitch.enabled":true,"autoswitch.threshold":80,"reset.policy":"expiring"}
```

---

## 10. Rendering (`src/codexswap/render.py`)

Plain ANSI, no dependencies. `list` output mirrors the cswap tree style:

```
Accounts:
  1: a@example.com  [pro]  * active
     |- 5h:   12%   resets 09-09 08:20   in 2h 7m
     |- 7d:   84%   resets 09-15 10:26   in 6d 7h
     +- resets: 3 available (soonest expires in 11d)

  2: b@example.com  [plus]  disabled
     re-login needed - authentication was rejected; run: codexswap add
```

The banner's reason is `authentication was rejected` when the credential was refused
and `stored credential could not be read` when the slot's `auth.json` is unusable. An
expired billing period inside a still-valid token earns no banner at all.

Use ASCII only (`|-`, `+-`) so Windows consoles in code page 949 do not mangle output.
Colour is applied only when `supports_color()` is true: green under 50%, yellow 50-79%,
red 80 and above. `ui.color` is checked first and settles the question on its own:
`never` and `always` both win outright, so `always` emits colour even under `NO_COLOR`
or a redirected stdout. Only `auto` consults `NO_COLOR`, `TERM=dumb` and `isatty()`.

```python
def supports_color(stream, setting: str) -> bool   # ui.color first, then NO_COLOR/tty
def human_duration(seconds: Optional[float]) -> str  # "6d 7h", "45s"; None is "-"
def format_ts(ts: Optional[int]) -> str            # local time "09-15 10:26", "-" if None
def render_accounts(...) -> str
def render_status(...) -> str
def render_reset_list(...) -> str
def render_config(items) -> str
```

---

## 11. Transfer format (`src/codexswap/transfer.py`)

```json
{
  "format": "codexswap-export",
  "version": 1,
  "exportedAt": "2026-09-09T03:00:00Z",
  "activeSlot": 1,
  "accounts": [
    {"slot":1,"email":"a@example.com","alias":"main","disabled":false,
     "auth":{"auth_mode":"chatgpt","tokens":{}}}
  ]
}
```

`transfer.export_accounts(store, path, *, account_ref=None) -> int` and
`transfer.import_accounts(store, path, *, force=False) -> List[Tuple[int, int]]`.
Export skips an account whose slot `auth.json` is missing or unreadable, warning on
stderr, so selecting exactly one such account writes an archive with no accounts.
Import refuses to overwrite an occupied slot unless `force`; on conflict without
`force` it allocates the next free slot and reports the remap. A `version` other than
1 raises `UserError`. The file is written `0o600` because it contains refresh tokens,
and the CLI prints a warning saying so.

API-key emails and `api key` plan labels survive export/import. Import seeds missing
slot configs from the destination machine's live Codex home; configs are not included
in the credential export. Forced import preserves an existing destination config.

---

## 12. Testing requirements

`pytest`, no network, no real Codex binary. Every test sets `CODEXSWAP_HOME` to a
`tmp_path` via a fixture.

CI runners have no Codex installed and a developer machine does, so a test must never
assume one is on `PATH`. `doctor` is the trap: it exits 1 when it cannot find the
binary, so asserting `doctor` returns 0 passes locally and fails in CI. Fake the
binary where the test is about it, and ignore the exit code where it is not. Running
the suite with Codex removed from `PATH` reproduces the CI environment.

Required coverage:

- `identity`: JWT decode including padding edge cases, missing claims, apikey mode,
  malformed base64 raising `AuthFileInvalid`.
- `store`: add/remove/alias/enable/disable/swap/move, slot allocation, ref resolution
  including ambiguity, round-trip through `accounts.json`, corrupt file recovery.
- `settings`: defaults, validation bounds, bool parsing, unknown key errors, save/load.
- `strategy`: both strategies, hysteresis, ties, unknown percent, all-ineligible.
- `resets.decide`: one test per numbered rule in section 6.
- `auto.tick`: every `TickResult.action` value, cooldown, unhealthy escalation, and a
  dry-run test asserting that no mutating fake was called.
- `transfer`: round-trip, slot conflict with and without `force`, version rejection.
- `appserver`: framing and handshake against a fake subprocess (a small Python script
  that speaks the protocol), timeout handling, malformed line skipping, error responses.
- `render`: no ANSI when colour is disabled, duration and timestamp formatting.
- `cli`: argument parsing for every subcommand, exit codes for each error class, and
  `--json` shapes matching section 9.
- `unclaimed`: what a rescue keeps and what it refuses, that a listing never carries
  the credential, that `--claim` drops the copy only after the registry holds it, and
  that an id cannot name a file outside the stash.

Coverage is judged by whether a test would fail if the behaviour were removed, not by
whether the line executed. Every rule that guards something irreversible needs a test
that reaches it through the real call path -- a test that constructs the guard
directly still passes when a caller stops using it. The suite carries paired tests for
these, each with the control case that proves the guard rejects the wrong thing rather
than everything:

- a confirmed `purge` refuses each way its root can overlap the Codex home, and
  deletes a root that overlaps neither;
- sync-back keeps a slot credential newer than the live one, and adopts a newer live
  one; and refuses a live file that identifies the slot but cannot authenticate;
- a forced import validates every entry before writing any;
- two stale registries each register their own account;
- an unreadable redemption ledger stays unspendable across a daemon save;
- a running daemon adopts `reset.policy never` set between ticks;
- a backend fallback reading for another account cannot authorise a redemption;
- a real backend request goes through the redirect guard, not just the guard class;
- `add-token` never echoes the key, including under `--debug`.

Target: the suite runs in under 30 seconds on Windows.
