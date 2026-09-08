# Contributing

Read [CONTRACT.md](CONTRACT.md) before making changes. It is the specification for
codexswap; changes to behaviour must update the contract in the same commit.
Python 3.9+ is required, and Windows, macOS, and Linux are supported targets.

## Development setup

From the repository root, use uv:

```sh
uv venv
uv pip install -e ".[dev]"
```

Or create a standard-library virtual environment:

```sh
python -m venv .venv
```

Activate either environment on macOS, Linux, or Git Bash:

```sh
# macOS / Linux:
source .venv/bin/activate
# Windows Git Bash instead:
source .venv/Scripts/activate
```

On Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

If you chose the standard-library venv, install the development dependencies after
activation:

```sh
python -m pip install -e ".[dev]"
```

## Checks

With the environment active, run:

```sh
pytest -q
ruff check .
```

Tests must use temporary `CODEXSWAP_HOME` directories, with no network access or
real Codex binary. Keep runtime code **standard-library-only**; pytest and Ruff
are development dependencies. Preserve Python 3.9 compatibility and account for
Windows as well as POSIX behaviour.

## Adding a setting

Add the setting to `SPECS` in `src/codexswap/settings.py`, including its type,
default, validation, and help text. Update the settings table in
[README.md](README.md) and section 5 of [CONTRACT.md](CONTRACT.md) in the same
commit. Add a test covering the default and meaningful validation or behaviour.

Keep contributions focused, explain the behaviour change, and include the checks
you ran. Never commit real `auth.json` files, tokens, or account exports. Bug
reports should include reproduction steps and redact email addresses.
