"""The Remote Control start hold (board row X3768).

`claude remote-control` refuses to start ("requires a full-scope login
token") when the active login is a ``claude setup-token`` credential, whose
only scope is ``user:inference``. Once started, the server keeps working
whatever the rotation does afterwards, because the owner proxy answers its
traffic as the owner account. So the one moment that needs a full-scope
login is the start itself.

The owner proxy subcommand's ``--ensure --remote-control-start`` (run by the
Remote Control launcher right before it execs the server) makes the owner
account active when the active login lacks the Remote Control scope, and
writes a START HOLD beside the account store. While the hold is fresh the
auto-switch engine switches nothing, so the rotation cannot move the box off
the owner login before the server has read it. The owner proxy deletes the
hold when it forwards the server's environment registration (``POST
/v1/environments/bridge``), and from then on the rotation is free again. A
hold older than :data:`START_HOLD_MAX_S` means the server never registered;
the engine deletes it and reports an error.

The hold file is ``start_hold.json`` in the claude-swap backup directory::

    {"setAt": <epoch seconds>, "owner": "<account number>",
     "previous": "<account number>" | null}

A missing file is no hold. A file that is there but unreadable or not that
shape raises :class:`StartHoldError`: guessing "no hold" would release the
rotation under a server that has not started, and guessing "held" would
stall it with nothing to expire the guess.
"""

from __future__ import annotations

import importlib
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from claude_swap import oauth
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.locking import FileLock, StaleBuildWriteError
from claude_swap.settings import atomic_write_json

_logger = logging.getLogger("claude-swap")

START_HOLD_FILENAME = "start_hold.json"

#: How long a hold stops the rotation before the engine reads it as a server
#: that never registered. A Remote Control start registers within seconds.
START_HOLD_MAX_S = 300.0

#: The scope `claude remote-control` requires of the active login.
REMOTE_CONTROL_SCOPE = "user:sessions:claude_code"

#: How long the start waits for the auto-switch engine's state lock, which
#: the engine holds across one switch (a liveness probe included).
_ENGINE_LOCK_WAIT_S = 30.0

_LINE_PREFIX = "start hold: "


class StartHoldError(ClaudeSwitchError):
    """The start hold file exists but cannot be read as a hold."""


@dataclass(frozen=True)
class StartHold:
    """One start hold: when it was set, the owner account made active, and
    the account that was active before (None when none was)."""

    set_at: float
    owner: str
    previous: str | None

    def age_s(self, now: float) -> float:
        return now - self.set_at


def start_hold_path(backup_dir: Path) -> Path:
    return Path(backup_dir) / START_HOLD_FILENAME


def read_start_hold(backup_dir: Path) -> StartHold | None:
    """The hold in ``backup_dir``, or None when there is none.

    Raises :class:`StartHoldError` naming the file when it is there but is
    not a readable hold.
    """
    path = start_hold_path(backup_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise StartHoldError(f"cannot read the start hold {path}: {exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StartHoldError(f"the start hold {path} is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise StartHoldError(f"the start hold {path} is not a JSON object")
    set_at = raw.get("setAt")
    owner = raw.get("owner")
    previous = raw.get("previous")
    if (
        not isinstance(set_at, (int, float))
        or isinstance(set_at, bool)
        or not isinstance(owner, str)
        or not owner
        or not (previous is None or isinstance(previous, str))
    ):
        raise StartHoldError(
            f"the start hold {path} lacks a numeric setAt, an owner account "
            "or a string-or-null previous account"
        )
    return StartHold(set_at=float(set_at), owner=owner, previous=previous)


def write_start_hold(backup_dir: Path, hold: StartHold) -> None:
    atomic_write_json(
        start_hold_path(backup_dir),
        {"setAt": hold.set_at, "owner": hold.owner, "previous": hold.previous},
    )


def remove_start_hold(backup_dir: Path) -> bool:
    """Delete the hold. Returns whether there was one to delete."""
    try:
        start_hold_path(backup_dir).unlink()
    except FileNotFoundError:
        return False
    return True


def credential_scopes(credentials: str | None) -> list[str] | None:
    """The OAuth scopes a stored credential carries, or None when it is not
    an OAuth credential carrying a scope list (absent, an API key, or a
    wrapper with no ``scopes``)."""
    data = oauth.extract_oauth_data(credentials) if credentials else None
    if data is None:
        return None
    scopes = data.get("scopes")
    if not isinstance(scopes, list):
        return None
    return [s for s in scopes if isinstance(s, str)]


def _can_start_remote_control(switcher) -> bool:
    scopes = credential_scopes(switcher._read_credentials())
    return scopes is not None and REMOTE_CONTROL_SCOPE in scopes


def _say(message: str, level: int = logging.INFO) -> None:
    """One decision line, to claude-swap.log and to stderr, which the
    Remote Control launcher sends to the journal."""
    _logger.log(level, "%s", _LINE_PREFIX + message)
    print(_LINE_PREFIX + message, file=sys.stderr, flush=True)


def _engine_state_lock(switcher) -> FileLock:
    # Imported here: autoswitch imports this module for the hold check.
    from claude_swap.autoswitch import STATE_FILENAME, engine_state_lock

    return engine_state_lock(
        switcher.backup_dir / STATE_FILENAME, timeout=_ENGINE_LOCK_WAIT_S
    )


def hold_for_remote_control_start(switcher, *, clock=time.time) -> StartHold | None:
    """Make the owner account active for a Remote Control start, when needed.

    The start-hold half of the owner proxy subcommand's ``--ensure
    --remote-control-start``. Every outcome is one stderr line, so the
    journal shows the decision on every start. Returns the hold written, or
    None when none was.

    Never raises: the launch path exits 0 on every path, so a failure is an
    ERROR line here, and the launcher's own check of the active login (board
    row X3769) is the alarm that the server could not start.
    """
    try:
        return _hold_for_remote_control_start(switcher, clock)
    except StaleBuildWriteError as exc:
        _say(f"failed: this claude-swap build is not the installed one ({exc})",
             logging.ERROR)
    # Every exception: the launch exits 0, so this ERROR line is the report.
    except Exception as exc:
        _logger.exception("start hold raised")
        _say(f"failed: {type(exc).__name__}: {exc}", logging.ERROR)
    return None


def _hold_for_remote_control_start(switcher, clock) -> StartHold | None:
    # Resolved at call time, so a test that replaces the module's readers
    # is the one this call reaches.
    owner_proxy_module = importlib.import_module("claude_swap.pin")

    if owner_proxy_module._pinned_email_now(switcher) is None:
        _say("not needed, no owner account is set")
        return None
    owner = owner_proxy_module.pinned_slot(switcher)
    if owner is None:
        _say("failed: the owner account is not in the account roster",
             logging.ERROR)
        return None
    if _can_start_remote_control(switcher):
        _say("not needed, active login is full-scope")
        return None
    previous = switcher.current_account_number()
    if previous == owner:
        _say(
            f"failed: owner account {owner} is active but its login lacks "
            f"{REMOTE_CONTROL_SCOPE}; Remote Control cannot start",
            logging.ERROR,
        )
        return None

    hold = StartHold(set_at=clock(), owner=owner, previous=previous)
    backup_dir = switcher.backup_dir
    # THE HOLD BEFORE THE SWITCH, BOTH UNDER THE ENGINE'S STATE LOCK. The
    # engine holds that lock across its own recheck and switch and re-reads
    # the hold there, so it cannot move the box off the owner between this
    # switch and the server's start. The switch path never takes the state
    # lock, so holding it here cannot deadlock.
    with _engine_state_lock(switcher).held_for("a Remote Control start hold"):
        write_start_hold(backup_dir, hold)
        try:
            result = switcher.switch_to(owner, json_output=True)
        except BaseException:
            remove_start_hold(backup_dir)
            raise
        if not result or not result.get("switched"):
            remove_start_hold(backup_dir)
            reason = (result or {}).get("reason", "no result")
            _say(
                f"failed: could not make owner account {owner} active "
                f"(was {previous}): {reason}",
                logging.ERROR,
            )
            return None
        if not _can_start_remote_control(switcher):
            remove_start_hold(backup_dir)
            _say(
                f"failed: owner account {owner} is now active but its login "
                f"lacks {REMOTE_CONTROL_SCOPE}; Remote Control cannot start",
                logging.ERROR,
            )
            return None
    _say(f"owner account {owner} made active for a Remote Control start "
         f"(was {previous})")
    return hold
