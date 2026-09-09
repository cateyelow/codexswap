"""Argument parsing, command dispatch, and presentation for codexswap."""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import getpass
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import __version__, errors, paths, render
from .identity import health_of, identity_from_auth, load_auth
from .models import HEALTH_EXPIRED, UsageSnapshot
from .settings import Settings
from .store import AccountStore


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        if getattr(self, "redact_errors", False) or self.prog.endswith(" add-token"):
            message = "invalid add-token arguments; run codexswap add-token --help"
        super().error(message)


def _global_flags(parser, *, inherited=False):
    default = argparse.SUPPRESS if inherited else False
    parser.add_argument("--debug", action="store_true", default=default,
                        help="show a traceback for unexpected errors")
    parser.add_argument("--version", action="version", version="codexswap " + __version__)
    parser.add_argument("--no-color", action="store_true", default=default,
                        help="disable ANSI colors")
    parser.add_argument("--home", metavar="PATH", type=Path,
                        default=argparse.SUPPRESS if inherited else None,
                        help="override CODEXSWAP_HOME")


def _json_flag(parser, *, inherited=False):
    parser.add_argument("--json", action="store_true",
                        default=argparse.SUPPRESS if inherited else False,
                        help="print a single JSON object")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="codexswap", description="Switch between Codex CLI accounts and manage reset credits.",
        epilog="Global flags may precede the command. For run, place them before the account ref; "
               "arguments after -- are passed to Codex.",
        allow_abbrev=False,
    )
    _global_flags(parser)
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    def command(name, help_text, *, aliases=(), parent=commands, dest=None):
        child = parent.add_parser(name, aliases=list(aliases), help=help_text,
                                  description=help_text, allow_abbrev=False)
        _global_flags(child, inherited=True)
        if dest is None:
            child.set_defaults(command=name)
        else:
            child.set_defaults(**{dest: name})
        return child

    command("help", "Show all commands and global options")
    command("version", "Show codexswap and Codex versions")
    child = command("list", "List accounts and usage", aliases=("ls",))
    _json_flag(child)
    child.add_argument("--token-status", action="store_true", help="show token expiry and source")
    child.add_argument("--no-probe", action="store_true", help="use cached usage only")
    child = command("status", "Show the active account", aliases=("current", "st"))
    _json_flag(child)
    child = command("switch", "Activate an account, or rotate to the next account")
    child.add_argument("ref", nargs="?", help="slot, alias, email, or unique email prefix")
    child.add_argument("--strategy", choices=("best", "next-available"))
    child.add_argument("--model", metavar="NAMES",
                       help="also weigh these per-model limits (comma separated, or all)")
    child.add_argument("--force", action="store_true", help="allow switching while Codex is running")
    _json_flag(child)
    child = command("add", "Capture the current Codex login")
    child.add_argument("--slot", type=int, metavar="N")
    child.add_argument("--alias", metavar="NAME")
    child = command("add-token", "Register an API key (prefer stdin or the hidden prompt)")
    child.add_argument("token", nargs="?", metavar="TOKEN|-")
    child.add_argument("--slot", type=int, metavar="N")
    child.add_argument("--email", metavar="EMAIL")
    child.add_argument("--alias", metavar="NAME")
    child = command("sync-config", "Copy Codex config.toml into one or all slot homes")
    child.add_argument("ref", nargs="?")
    child.add_argument("--from", dest="source", type=Path, metavar="PATH")
    child.add_argument("--force", action="store_true", help="overwrite different slot configs")
    child = command("doctor", "Diagnose local setup without reading usage or redeeming credits")
    _json_flag(child)
    child = command("watch", "Continuously display accounts and usage")
    child.add_argument("--interval", type=int, default=30, metavar="N")
    child = command("upgrade", "Upgrade codexswap using its detected package manager")
    child.add_argument("--yes", action="store_true", help="confirm running the upgrade")
    _json_flag(child)
    for name, description, aliases in (
        ("remove", "Remove an account and its slot home", ("rm",)),
        ("disable", "Disable an account for automatic selection", ()),
        ("enable", "Enable an account for automatic selection", ()),
    ):
        child = command(name, description, aliases=aliases)
        child.add_argument("ref")
    child = command("alias", "List, set, or unset account aliases")
    child.add_argument("ref", nargs="?")
    child.add_argument("name", nargs="?")
    child.add_argument("--unset", action="store_true")
    child = command("swap", "Exchange two account slots")
    child.add_argument("a")
    child.add_argument("b")
    child = command("move", "Move an account to a free slot")
    child.add_argument("ref")
    child.add_argument("slot", type=int)
    child = command("run", "Run Codex using an account or the current directory mapping")
    child.add_argument("ref", nargs="?")
    child.add_argument("codex_args", nargs=argparse.REMAINDER, metavar="-- CODEX_ARGS")
    child = command("map", "List mappings or map a directory to an account")
    child.add_argument("ref", nargs="?")
    child.add_argument("path", nargs="?", type=Path)
    child = command("unmap", "Remove a directory mapping (defaults to the current directory)")
    child.add_argument("path", nargs="?", type=Path)
    child = command("probe", "Refresh usage for one account or all enabled accounts")
    child.add_argument("ref", nargs="?")
    child.add_argument("--backend", action="store_true",
                       help="on failure, fall back to unsupported chatgpt.com endpoints")
    _json_flag(child)
    child = command("reset", "List or redeem rate-limit reset credits for the active account")
    _json_flag(child)
    reset_commands = child.add_subparsers(dest="reset_command", metavar="ACTION")
    reset_list = command("list", "List reset credits", parent=reset_commands, dest="reset_command")
    _json_flag(reset_list, inherited=True)
    reset_use = command("use", "Redeem one credit (irreversible; resets both usage windows)",
                        parent=reset_commands, dest="reset_command")
    reset_use.add_argument("ref", nargs="?")
    reset_use.add_argument("--credit", metavar="ID")
    reset_use.add_argument("--yes", action="store_true", help="confirm redemption")
    reset_use.add_argument("--dry-run", action="store_true", help="show the credit without redeeming")
    child = command("auto", "Run the automatic account switching and reset policy")
    child.add_argument("--once", action="store_true")
    child.add_argument("--dry-run", action="store_true")
    child.add_argument("--interval", type=int, metavar="N")
    child.add_argument("--threshold", type=int, metavar="N")
    child.add_argument("--model", metavar="NAMES",
                       help="also weigh these per-model limits (comma separated, or all)")
    child = command("config", "Show or change configuration settings")
    _json_flag(child)
    config_commands = child.add_subparsers(dest="config_command", metavar="ACTION")
    config_set = command("set", "Set a configuration value", parent=config_commands,
                         dest="config_command")
    config_set.add_argument("key", metavar="KEY")
    config_set.add_argument("value", metavar="VALUE")
    _json_flag(config_set, inherited=True)
    config_unset = command("unset", "Restore a setting's default", parent=config_commands,
                           dest="config_command")
    config_unset.add_argument("key", metavar="KEY")
    _json_flag(config_unset, inherited=True)
    child = command("export", "Export accounts and credentials to a file")
    child.add_argument("path", type=Path)
    child.add_argument("--account", metavar="REF")
    child = command("import", "Import accounts and credentials from a file")
    child.add_argument("path", type=Path)
    child.add_argument("--force", action="store_true")
    child = command("purge", "Delete the entire codexswap home")
    child.add_argument("--yes", action="store_true", help="confirm deletion")
    return parser


def _cached(store, account, *, max_age, now) -> Optional[UsageSnapshot]:
    if account.identity.auth_mode == "apikey":
        return None
    # A cache entry written before this check existed, or edited by hand, can name
    # another account. Showing it would attribute one person's usage to another.
    try:
        snapshot = store.cached_usage(account.slot, max_age=max_age, now=now)
        if snapshot is not None and snapshot.describes(account.identity):
            return snapshot
    except Exception:
        pass
    snapshot = account.last_seen_usage
    if (snapshot is not None and now - snapshot.fetched_at <= max_age
            and snapshot.describes(account.identity)):
        return snapshot
    return None


def _cached_all(store, settings, accounts) -> Dict[int, Tuple[Optional[UsageSnapshot], bool]]:
    now = time.time()
    usages = {}
    for account in accounts:
        snapshot = _cached(store, account, max_age=float("inf"), now=now)
        stale = snapshot is not None and now - snapshot.fetched_at > settings.probe_stale_seconds
        usages[account.slot] = (snapshot, stale)
    return usages


def _backend_fallback(store, account, settings, results, auth_failed, mismatched=None):
    """Last resort for one account, reachable only through the explicit opt-in."""
    from . import backend

    try:
        snapshot = backend.probe_usage(store.path_for(paths.slot_home(account.slot)),
                                       timeout=settings.probe_timeout)
    except errors.AuthExpired:
        if auth_failed is not None:
            auth_failed.add(account.slot)
    except Exception:
        # Never surface backend diagnostics here; they can quote a response body.
        pass
    else:
        if not snapshot.describes(account.identity):
            # Same rule as the supported path: the reading is somebody else's, and
            # the commands that refuse on a mismatch have to be told about it.
            if mismatched is not None:
                mismatched.add(account.slot)
            return
        results[account.slot] = (snapshot, False)
        store.record_usage(account.slot, snapshot)


def _probe_all(store, settings, *, accounts, force=False, auth_failed=None,
               allow_backend=False, mismatched=None
               ) -> Dict[int, Tuple[Optional[UsageSnapshot], bool]]:
    accounts = list(accounts)
    results: Dict[int, Tuple[Optional[UsageSnapshot], bool]] = {}
    if mismatched is None:
        mismatched = set()
    pending = []
    now = time.time()
    for account in accounts:
        if account.identity.auth_mode == "apikey":
            results[account.slot] = (None, False)
            continue
        cached = _cached(store, account, max_age=float("inf"), now=now)
        results[account.slot] = (cached, cached is not None)
        if not force:
            fresh = _cached(store, account, max_age=settings.probe_stale_seconds, now=now)
            if fresh is not None:
                results[account.slot] = (fresh, False)
                continue
        pending.append(account)
    if not pending:
        return results
    try:
        # Keep Codex process discovery and app-server imports off the help path.
        from . import appserver

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(appserver.probe_usage, paths.slot_home(account.slot),
                            timeout=settings.probe_timeout): account
                for account in pending
            }
            for future in concurrent.futures.as_completed(futures):
                account = futures[future]
                try:
                    snapshot = future.result()
                    if snapshot is None:
                        continue
                    if not snapshot.describes(account.identity):
                        # The slot home holds a different account than the registry
                        # says. Caching this would attribute usage to the wrong
                        # person, and the cached reading it would fall back to is now
                        # known to describe a slot whose contents have changed.
                        mismatched.add(account.slot)
                        results[account.slot] = (None, False)
                        continue
                    results[account.slot] = (snapshot, False)
                    # Serialize registry/cache writes on the calling thread.
                    store.record_usage(account.slot, snapshot)
                except errors.AuthExpired:
                    # A rejected credential is the one probe failure worth telling
                    # the user about; everything else degrades to a stale reading.
                    if auth_failed is not None:
                        auth_failed.add(account.slot)
                    continue
                except Exception:
                    # Do not surface subprocess diagnostics that could contain tokens.
                    if allow_backend or settings.allow_backend_fallback:
                        _backend_fallback(store, account, settings, results, auth_failed,
                                          mismatched)
                    continue
    except Exception:
        # Missing binaries, executor failures, and unreadable caches degrade output.
        pass
    return results


def _json_account(account, usage, stale, *, active_slot, auth_failed=()) -> Dict[str, Any]:
    def window(value):
        if value is None:
            return None
        return {"usedPercent": value.used_percent, "windowMinutes": value.window_minutes,
                "resetsAt": value.resets_at}

    # CONTRACT: status uses the same full account entry as list. activeSlot lives
    # in the enclosing object; absent usage is JSON null.
    return {
        "slot": account.slot, "email": account.identity.email, "alias": account.alias,
        "disabled": account.disabled,
        "planType": "api key" if account.identity.auth_mode == "apikey" else account.identity.plan_type,
        "health": (HEALTH_EXPIRED if account.slot in auth_failed
                   else health_of(account.identity, now=time.time())),
        "usage": None if usage is None else {
            "bindingPercent": usage.binding_percent,
            "primary": window(usage.primary), "secondary": window(usage.secondary),
            "resetCreditsAvailable": usage.available_reset_count,
            "fetchedAt": usage.fetched_at, "stale": bool(stale),
        },
    }


def _print_json(value):
    print(json.dumps(value, indent=2))


def _active_account(store):
    if not store.accounts:
        raise errors.NoAccountsConfigured("no accounts configured; run: codexswap add")
    if store.active_slot is None:
        raise errors.UserError("no active account; run: codexswap switch <ref>")
    return store.get(store.active_slot)


def _show_accounts(args, store, settings, color):
    single = args.command == "status" or (args.command == "probe" and args.ref is not None)
    if single:
        account = (store.resolve(args.ref) if args.command == "probe" else _active_account(store))
        accounts = [account]
    else:
        accounts = store.enabled_accounts() if args.command == "probe" else store.ordered()
    usages = _cached_all(store, settings, accounts)
    auth_failed = set()
    mismatched = set()
    if not getattr(args, "no_probe", False):
        to_probe = accounts if single else [account for account in accounts if not account.disabled]
        usages.update(_probe_all(store, settings, accounts=to_probe,
                                 force=args.command == "probe", auth_failed=auth_failed,
                                 allow_backend=getattr(args, "backend", False),
                                 mismatched=mismatched))
    if mismatched:
        # Reading is harmless, so say it and carry on; --json keeps stdout clean.
        print("warning: slot {} answered for a different account than the registry records; "
              "run: codexswap doctor".format(", ".join(str(slot) for slot in sorted(mismatched))),
              file=sys.stderr)
    for account in accounts:
        # A missing or unreadable slot file must not prevent listing the stored account.
        with contextlib.suppress(errors.CodexSwapError, OSError):
            derived = identity_from_auth(
                load_auth(paths.slot_home(account.slot) / "auth.json")
            )
            if derived.auth_mode == "apikey":
                derived = replace(derived, email=account.identity.email, plan_type="api key")
                usages[account.slot] = (None, False)
            account.identity = derived
    if args.json:
        entries = [_json_account(account, *usages[account.slot], active_slot=store.active_slot,
                                 auth_failed=auth_failed)
                   for account in accounts]
        _print_json({"activeSlot": store.active_slot,
                     "account" if single else "accounts": entries[0] if single else entries})
    elif single:
        print(render.render_status(accounts[0], usages[accounts[0].slot],
                                   active_slot=store.active_slot, color=color,
                                   auth_failed=auth_failed))
    else:
        print(render.render_accounts(accounts, usages, active_slot=store.active_slot, color=color,
                                     token_status=getattr(args, "token_status", False),
                                     auth_failed=auth_failed))
    return 0


def _selected_models(args, settings) -> Tuple[str, ...]:
    """--model for this run, otherwise autoswitch.model."""
    raw = getattr(args, "model", None)
    if raw is None:
        return settings.models
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _switch(args, store, settings):
    from . import strategy, switcher

    # Weighing a model only means something while choosing by usage, so asking for
    # one selects rather than rotates. The saved strategy decides how.
    selection = args.strategy or (settings.strategy if args.model is not None else None)
    if args.ref is not None:
        # CONTRACT: an explicit ref selects the requested account directly.
        target = store.resolve(args.ref)
    elif selection is None:
        target = strategy.rotate_next(store.ordered(), store.active_slot)
    else:
        accounts = store.enabled_accounts()
        usages = _probe_all(store, settings, accounts=accounts)
        models = _selected_models(args, settings)
        candidates = [strategy.Candidate(account, usages[account.slot][0], models)
                      for account in accounts]
        target = switcher.pick_target(candidates, current_slot=store.active_slot,
                                      strategy=selection, threshold=settings.threshold,
                                      hysteresis=settings.hysteresis_pct)
    if target is None:
        reason = "no other enabled account is available"
        if selection is not None:
            reason += f" at or below {settings.threshold - settings.hysteresis_pct}% usage (threshold minus hysteresis)"
        if not store.accounts:
            reason = "no accounts configured; run: codexswap add"
        raise errors.UserError("no eligible target: " + reason)
    previous = store.active_slot
    switcher.activate(store, target, force=args.force, sync_back=True)
    if args.json:
        _print_json({"from": previous, "to": target.slot,
                     "account": {"slot": target.slot, "email": target.identity.email}})
    else:
        print(f"switched {previous} -> {target.slot} ({target.identity.label()})")
    return 0


def _reset(args, store, settings, color):
    from . import resets

    ref = getattr(args, "ref", None)
    account = store.resolve(ref) if ref is not None else _active_account(store)
    use = args.reset_command == "use"
    mismatched = set()
    snapshot, stale = _probe_all(store, settings, accounts=[account], force=use,
                                 mismatched=mismatched)[account.slot]
    if account.slot in mismatched:
        # Redemption is irreversible and spends the credit of whoever is actually in
        # that slot home, not the account the user named. Refuse rather than guess.
        raise errors.UserError(
            f"slot {account.slot} answered for a different account than the registry records; "
            "re-add it or run: codexswap doctor"
        )
    if snapshot is None:
        raise errors.UserError(f"usage unavailable for slot {account.slot}; cannot read reset credits")
    now = time.time()
    if not use:
        if args.json:
            _print_json({"slot": account.slot, "availableCount": snapshot.available_reset_count,
                         "credits": [{"id": credit.id, "status": credit.status,
                                      "expiresAt": credit.expires_at,
                                      "daysUntilExpiry": credit.days_until_expiry(now),
                                      "title": credit.title} for credit in snapshot.reset_credits]})
        else:
            if stale:
                print(f"Reset credits for slot {account.slot} (stale):")
            print(render.render_reset_list(account.slot, snapshot.reset_credits, color=color, now=now))
        return 0
    credit = snapshot.soonest_expiring_credit()
    if args.credit is not None:
        credit = next((item for item in snapshot.available_reset_credits if item.id == args.credit),
                      None)
    if credit is None:
        raise errors.UserError(f"no available reset credit matches the request for slot {account.slot}")
    # CONTRACT: the reset parent's --json also controls `reset --json use`.
    output = sys.stderr if args.json else sys.stdout
    print("{}redeem credit for slot {}:".format("would " if args.dry_run else "", account.slot),
          file=output)
    print(render.render_reset_list(account.slot, [credit], color=color, now=now,
                                   summary=False), file=output)
    if args.dry_run:
        if args.json:
            _print_json({"slot": account.slot, "creditId": credit.id,
                         "outcome": None, "dryRun": True})
        return 0
    if not args.yes:
        prompt = ("Irreversible: resets both the 5-hour and weekly windows. "
                  "redeem this reset credit? [y/N] ")
        print(prompt, end="", file=output, flush=True)
        try:
            accepted = input().strip().casefold() in ("y", "yes")
        except EOFError:
            accepted = False
        if not accepted:
            print("reset cancelled", file=output)
            if args.json:
                _print_json({"slot": account.slot, "creditId": credit.id,
                             "outcome": None, "dryRun": False})
            return 0
    outcome = resets.redeem(paths.slot_home(account.slot), credit_id=credit.id,
                            timeout=settings.probe_timeout)
    if resets.outcome_is_success(outcome):
        # Both windows are now zero and one credit is gone. Every cached number for
        # this slot is wrong, not merely stale, so the next read must probe again.
        store.forget_usage(account.slot)
    if args.json:
        _print_json({"slot": account.slot, "creditId": credit.id,
                     "outcome": outcome, "dryRun": False})
    else:
        print(f"reset slot {account.slot}: {outcome}")
    return 0 if resets.outcome_is_success(outcome) else 1


def _config(args, settings):
    if args.config_command == "set":
        value = settings.set(args.key, args.value)
        settings.save()
        if not args.json:
            print(f"set {args.key} = {json.dumps(value)}")
    elif args.config_command == "unset":
        settings.unset(args.key)
        settings.save()
        if not args.json:
            print(f"unset {args.key} (default restored)")
    if args.json:
        # CONTRACT: config has no prescribed JSON schema; use dotted keys and
        # typed effective values, in the same order as the text listing.
        _print_json({key: value for key, value, _ in settings.items()})
    elif args.config_command is None:
        print(render.render_config(settings.items()))
    return 0


def _purge(args):
    configured = paths.codexswap_home().expanduser()
    root = configured.resolve()
    # Deletion belongs here because the contract exposes no purge service. Check
    # resolved paths before recursively deleting the explicitly configured root.
    protected = (paths.codex_home().resolve(), (Path.home() / ".codex").resolve())
    if configured.is_symlink() or root == Path(root.anchor) or root == Path.home().resolve():
        raise errors.UserError(f"refusing to purge unsafe home: {root}")
    for live in protected:
        if root == live or root in live.parents or live in root.parents:
            raise errors.UserError(f"refusing to purge a path overlapping the Codex home: {root}")
    print(f"will delete {root} and all of its contents")
    if not args.yes:
        if not sys.stdin.isatty():
            raise errors.UserError("purge requires --yes or an interactive yes confirmation")
        try:
            confirmed = input("type yes to delete: ").strip().casefold() == "yes"
        except EOFError:
            confirmed = False
        if not confirmed:
            print("purge cancelled")
            return 0
    if root.exists():
        shutil.rmtree(root)
    print(f"purged {root}")
    return 0


def _version():
    print("codexswap " + __version__)
    binary = os.environ.get("CODEX_BIN") or shutil.which("codex") or shutil.which("codex.exe")
    if binary:
        try:
            result = subprocess.run([binary, "--version"], capture_output=True, text=True,
                                    timeout=5, check=True)
            version = result.stdout.strip().splitlines()[0]
            for prefix in ("codex-cli ", "codex "):
                if version.startswith(prefix):
                    version = version[len(prefix):]
                    break
            print("codex " + version)
            return 0
        except (OSError, subprocess.SubprocessError, IndexError):
            pass
    print("codex not found")
    return 0


def _add_token(args, store):
    token = args.token
    if token == "-":
        token = sys.stdin.readline()
    elif token is None:
        if not sys.stdin.isatty():
            raise errors.UserError("use add-token - to read an API key from stdin")
        try:
            # getpass warns before falling back to an echoed input; refuse that fallback.
            import warnings

            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                token = getpass.getpass("OpenAI API key: ")
        except (EOFError, getpass.GetPassWarning):
            raise errors.UserError("cannot read a hidden API key; use add-token -") from None
    try:
        account = store.add_token(token, slot=args.slot, email=args.email, alias=args.alias)
    except OSError:
        raise errors.UserError("Cannot save API-key account") from None
    print(f"saved slot {account.slot} ({account.display()})")
    return 0


def _watch(args):
    if not 5 <= args.interval <= 3600:
        raise errors.UserError("watch interval must be in the range 5..3600 seconds")
    # Standard-library ANSI/plain output: curses is unavailable on Windows.
    frame_args = argparse.Namespace(command="list", json=False, no_probe=False, token_status=False)
    try:
        while True:
            settings = Settings.load()
            color = render.supports_color(sys.stdout, "never" if args.no_color else settings.ui_color)
            if sys.stdout.isatty() and color:
                print("\033[2J\033[H", end="", flush=True)
            else:
                print("--- " + datetime.now().astimezone().isoformat(timespec="seconds") + " ---")
            try:
                _show_accounts(frame_args, AccountStore.load(), settings, color)
            except Exception:
                # Never echo a probe exception: subprocess output can contain secrets.
                print("Accounts: refresh failed; retrying next frame")
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()
        return 0


def _upgrade_method() -> Optional[str]:
    import codexswap

    locations = [sys.prefix, sys.argv[0], codexswap.__file__ or ""]
    normalised = ["/" + value.replace("\\", "/").lower().strip("/") + "/"
                  for value in locations]
    uv = any("/uv/tools/" in value or "/uv/tool/" in value for value in normalised)
    pipx = any("/pipx/venvs/" in value for value in normalised)
    if uv and pipx:
        return None
    if uv:
        return "uv"
    if pipx:
        return "pipx"
    # CONTRACT: a checkout/editable install is ambiguous. Only a package installed
    # under this interpreter's prefix in site/dist-packages establishes plain pip.
    package = Path(codexswap.__file__ or "").resolve()
    prefix = Path(sys.prefix).resolve()
    if prefix in package.parents and any(
        part in ("site-packages", "dist-packages") for part in package.parts
    ):
        return "pip"
    return None


def _upgrade(args):
    commands = {"uv": ["uv", "tool", "upgrade", "codexswap"],
                "pipx": ["pipx", "upgrade", "codexswap"],
                "pip": [sys.executable, "-m", "pip", "install", "--upgrade", "codexswap"]}

    def display(command):
        return subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)

    method = _upgrade_method()
    output = sys.stderr if args.json else sys.stdout
    if method is None:
        candidates = [display(command) for command in commands.values()]
        print("Cannot determine installation method. Candidate commands:", file=output)
        for candidate in candidates:
            print("  " + candidate, file=output)
        if args.json:
            _print_json({"method": None, "command": None, "candidates": candidates,
                         "executed": False, "returnCode": 2})
        return 2
    command = commands[method]
    print("will run: " + display(command), file=output, flush=True)
    accepted = args.yes
    if not accepted:
        print("Upgrade codexswap? [y/N] ", end="", file=output, flush=True)
        try:
            accepted = input().strip().casefold() in ("y", "yes")
        except EOFError:
            accepted = False
    code = 0
    executed = False
    if accepted:
        try:
            # In JSON mode send the package manager's entire output to stderr.
            result = subprocess.run(command, stdout=sys.stderr if args.json else None,
                                    stderr=sys.stderr if args.json else None, check=False)
            executed = True
            code = result.returncode
        except OSError:
            print("error: could not start the upgrade command", file=output)
            code = 1
    else:
        print("upgrade cancelled", file=output)
    if args.json:
        _print_json({"method": method, "command": command, "executed": executed, "returnCode": code})
    return code


def _dispatch(args, parser):
    if args.command in (None, "help"):
        parser.print_help()
        return 0
    if args.command == "version":
        return _version()
    if args.command == "purge":
        return _purge(args)
    if args.command == "doctor":
        from .doctor import collect_checks

        report = collect_checks()
        if args.json:
            _print_json(report)
        else:
            for check in report["checks"]:
                print("{status}: {name}: {detail}".format(**check))
        return int(any(check["status"] == "fail" for check in report["checks"]))
    if args.command == "upgrade":
        return _upgrade(args)
    if args.command == "watch":
        return _watch(args)
    settings = Settings.load()
    if args.command == "config":
        return _config(args, settings)
    if args.command == "auto":
        from . import auto

        return auto.run(once=args.once, dry_run=args.dry_run, interval=args.interval,
                        threshold=args.threshold, models=getattr(args, "model", None),
                        log=print)
    color = render.supports_color(sys.stdout, "never" if args.no_color else settings.ui_color)
    store = AccountStore.load()
    if args.command == "add-token":
        return _add_token(args, store)
    if args.command == "sync-config":
        lines = store.sync_config(args.ref, source=args.source, force=args.force)
        for line in lines:
            print(line)
        # CONTRACT: conflicts are reported partial success (1); no slots is success.
        return int(any(": skipped" in line or ": failed" in line for line in lines))
    if args.command in ("list", "status", "probe"):
        return _show_accounts(args, store, settings, color)
    if args.command == "switch":
        return _switch(args, store, settings)
    if args.command == "reset":
        return _reset(args, store, settings, color)
    if args.command == "add":
        from . import switcher

        account = switcher.capture_current(store, slot=args.slot, alias=args.alias)
        print(f"saved slot {account.slot} ({account.display()})")
    elif args.command == "remove":
        account = store.resolve(args.ref)
        store.remove(account.slot)
        print(f"removed slot {account.slot} ({account.display()})")
    elif args.command in ("disable", "enable"):
        account = store.resolve(args.ref)
        store.set_disabled(account.slot, args.command == "disable")
        print(f"{args.command}d slot {account.slot} ({account.display()})")
    elif args.command == "alias":
        if args.ref is None and not args.unset and args.name is None:
            aliases = [f"  {account.slot}: {account.alias}  ({account.identity.label()})"
                       for account in store.ordered() if account.alias]
            print("\n".join(aliases) if aliases else "  no aliases")
        else:
            if args.ref is None or (args.unset and args.name is not None):
                raise errors.UserError("use alias <ref> <name> or alias <ref> --unset")
            if not args.unset and args.name is None:
                raise errors.UserError("alias requires a name or --unset")
            account = store.resolve(args.ref)
            store.set_alias(account.slot, None if args.unset else args.name)
            print("{} alias for slot {}{}".format(
                "unset" if args.unset else "set", account.slot,
                "" if args.unset else " to " + args.name,
            ))
    elif args.command == "swap":
        a, b = store.resolve(args.a).slot, store.resolve(args.b).slot
        store.swap_slots(a, b)
        print(f"swapped slots {a} and {b}")
    elif args.command == "move":
        previous = store.resolve(args.ref).slot
        store.move_slot(previous, args.slot)
        print(f"moved slot {previous} -> {args.slot}")
    elif args.command == "run":
        from . import mappings, switcher

        if args.ref is not None:
            account = store.resolve(args.ref)
        else:
            slot = mappings.lookup(os.getcwd())
            if slot is None:
                raise errors.UserError("no account mapped to this directory; run: codexswap map <ref>")
            account = store.get(slot)
        return switcher.run_as(store, account, list(args.codex_args))
    elif args.command in ("map", "unmap"):
        from . import mappings

        directory = args.path if args.path is not None else Path.cwd()
        if args.command == "map" and args.ref is None:
            print(render.render_mappings(mappings.all_mappings()))
        elif args.command == "map":
            account = store.resolve(args.ref)
            mappings.set_mapping(directory, account.slot)
            print(f"mapped {mappings.normalise_path(directory)} -> {account.slot}")
        else:
            removed = mappings.remove_mapping(directory)
            print("{} {}".format("unmapped" if removed else "no mapping for",
                                  mappings.normalise_path(directory)))
    elif args.command in ("export", "import"):
        from . import transfer

        if args.command == "export":
            transfer.export_accounts(store, args.path, account_ref=args.account)
            print("warning: export contains refresh tokens; keep this file private", file=sys.stderr)
            print(f"exported accounts to {args.path}")
        else:
            result = transfer.import_accounts(store, args.path, force=args.force)
            print(f"imported accounts from {args.path}{_import_summary(result)}")
    return 0


def _import_summary(result) -> str:
    # CONTRACT: report slot remaps returned by the transfer service, without
    # serializing accounts or auth dictionaries into terminal output.
    if isinstance(result, (list, tuple, dict)):
        pairs = result.items() if isinstance(result, dict) else result
        remaps = [f"{old} -> {new}" for old, new in pairs
                  if isinstance(old, int) and isinstance(new, int) and old != new]
        if remaps:
            return " (remapped " + ", ".join(remaps) + ")"
    return ""


def _tolerate_legacy_console() -> None:
    """Never fail a command because the console cannot encode its own output.

    Windows consoles still default to a legacy code page (CP949 on this developer's
    machine), where printing an emoji alias raises UnicodeEncodeError *after* the
    alias has already been set, so a successful mutation reports failure and exits 1.
    Unencodable characters become backslash escapes; a UTF-8 terminal is unaffected.
    """
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(errors="backslashreplace")


def main(argv: Optional[Sequence[str]] = None) -> int:
    _tolerate_legacy_console()
    arguments: List[str] = list(sys.argv[1:] if argv is None else argv)
    before_separator = arguments[:arguments.index("--")] if "--" in arguments else arguments
    debug = "--debug" in before_separator
    overridden = False
    try:
        parser = build_parser()
        parser.redact_errors = "add-token" in arguments
        # Parse the run prefix separately so `run -- --resume` uses a directory
        # mapping, rather than consuming --resume as the optional account ref.
        args = parser.parse_args(arguments)
        if "--" in arguments and args.command == "run":
            separator = arguments.index("--")
            args = parser.parse_args(arguments[:separator])
            args.codex_args.extend(arguments[separator + 1:])
        debug = args.debug
        if args.home is not None:
            paths.set_home_override(args.home)
            overridden = True
        return _dispatch(args, parser)
    except SystemExit as exc:
        return int(exc.code or 0)
    except errors.CodexSwapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        if "add-token" in before_separator:
            print("error: could not register API-key account", file=sys.stderr)
            return 1
        if debug:
            raise
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("run with --debug for a traceback", file=sys.stderr)
        return 1
    finally:
        if overridden:
            paths.clear_home_override()
