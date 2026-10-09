"""Serialization helpers for ``--json`` structured output.

Centralizes the schema-v1 shapes so ``--list``/``--status``/``--switch`` agree on
field names (camelCase, matching the export envelope in transfer.py) and on how the
internal usage dict is projected to JSON. Callers build payloads here; the CLI does
the single ``json.dumps`` (see cli.py).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from claude_swap import oauth, pace

# Bump only on a breaking change to any payload shape. Scripts key off this.
SCHEMA_VERSION = 1

# Sentinel entries that ``_collect_usage`` / ``_fetch_active_usage`` yield in place
# of a usage dict. Kept here (the serialization hub) so the human renderer and the
# JSON projection agree instead of scattering raw strings.
USAGE_NO_CREDENTIALS = "no credentials"
USAGE_TOKEN_EXPIRED = "token expired"
# API-key (``/login`` managed key) accounts have no subscription quota; usage is
# reported as this sentinel instead of being fetched from the OAuth usage API.
USAGE_API_KEY = "api key"
# The active account's macOS Keychain was unreadable (locked / denied / timeout)
# with no plaintext fallback — distinct from a genuinely empty slot, so the user
# isn't misled into an unnecessary re-login.
USAGE_KEYCHAIN_UNAVAILABLE = "keychain unavailable"
# The stored refresh-token lineage is dead (repeated ``invalid_grant``). The
# account is quarantined from fetching until a re-login (``cswap login`` / ``add``)
# replaces the credential; distinct from "token expired" (which Claude Code can
# refresh on its own) because only the user can fix it.
USAGE_RELOGIN_REQUIRED = "re-login needed"
# The profile oracle proved the live credential belongs to a DIFFERENT account
# than the slot's identity (foreign credential under a stale config — partial
# cross-machine sync or a mid-``/login`` poll). Its quota is not this slot's, so
# recording it would poison history and autoswitch decisions; distinct from
# "token expired" because holding is wrong here — a switch repairs the drift
# (stash the foreign credential, restore the slot's backup), so autoswitch
# should treat the active as unknown-headroom and fail over.
USAGE_FOREIGN_CREDENTIAL = "foreign credential"


def _window_to_json(entry: dict) -> dict:
    """Project a 5h/7d usage window to JSON, preserving raw ``resetsAt``.

    ``countdown``/``clock`` are recomputed from ``resets_at`` at serialization
    time (the store may serve a measurement hours after its fetch); entries
    without ``resets_at`` fall back to the fetch-time strings.
    """
    out: dict = {"pct": entry["pct"]}
    if "resets_at" in entry:
        out["resetsAt"] = entry["resets_at"]
    cell = oauth.fresh_reset_strings(entry)
    if cell:
        out["countdown"], out["clock"] = cell
    return out


def _pace_fields(entry: dict, fetched_at: float | None) -> dict:
    """Weekly-window pace fields (issue #125): additive, JSON-only.

    Emitted only when pace is computable and not suppressed (see
    ``claude_swap.pace.compute_pace``). ``projectedExhaustionAt`` is a linear
    ETA — wide error bars against real, bursty usage — so it's kept out of
    every human-facing surface and only ever appears here.
    """
    if fetched_at is None:
        return {}
    result = pace.compute_pace(entry, fetched_at=fetched_at)
    if result is None:
        return {}
    out: dict = {
        "expectedPct": round(result.expected_pct, 1),
        "aheadOfPace": result.ahead,
    }
    eta = pace.projected_exhaustion_ts(result, fetched_at=fetched_at)
    if eta is not None:
        out["projectedExhaustionAt"] = (
            datetime.fromtimestamp(eta, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        )
    will_last = pace.will_last_to_reset(result)
    if will_last is not None:
        out["willLastToReset"] = will_last
    return out


def _weekly_window_to_json(entry: dict, fetched_at: float | None) -> dict:
    """A 7d/scoped window's JSON projection, with pace fields layered in."""
    out = _window_to_json(entry)
    out.update(_pace_fields(entry, fetched_at))
    return out


def _scoped_window_to_json(entry: dict, fetched_at: float | None) -> dict:
    """Project a per-model scoped weekly window, carrying its model name."""
    out = _weekly_window_to_json(entry, fetched_at)
    out["name"] = entry["name"]
    return out


def usage_to_json(usage: dict, fetched_at: float | None = None) -> dict:
    """Convert the internal usage dict to its camelCase JSON projection.

    Sub-keys are emitted only when present in the source (the API does not always
    return every window or pay-as-you-go spend). ``fetched_at`` is the
    measurement's fetch time; passing it adds pace fields to the weekly
    windows (``seven_day``, ``scoped``) only — never ``five_hour`` (issue #125).
    """
    out: dict = {}
    if "five_hour" in usage:
        out["fiveHour"] = _window_to_json(usage["five_hour"])
    if "seven_day" in usage:
        out["sevenDay"] = _weekly_window_to_json(usage["seven_day"], fetched_at)
    if "spend" in usage:
        spend = usage["spend"]
        spend_out: dict = {
            "reported": spend["reported"],
            "used": spend["used"],
            "limit": spend["limit"],
            "remaining": spend["remaining"],
            "pct": spend["pct"],
            "currency": spend["currency"],
            "limitReached": spend["limit_reached"],
        }
        if spend["reported"] == oauth.SPEND_REPORTED_FRACTION:
            spend_out["disabledReason"] = spend["disabled_reason"]
            # A fraction spend's remaining is null, so it measures nothing.
            spend_out["remainingBasis"] = None
        else:
            # What remaining measures (X3711): money left from an entered
            # balance, or room under the monthly limit.
            spend_out["remainingBasis"] = spend["remaining_basis"]
            if spend["remaining_basis"] == oauth.REMAINING_BASIS_BALANCE:
                spend_out["balanceEnteredAt"] = spend["balance_entered_at"]
        if "cap_source" in spend:
            # Dollars computed from the configured cap (X3696), not the API.
            spend_out["capSource"] = spend["cap_source"]
            spend_out["disabledReason"] = spend["disabled_reason"]
        if "resets_at" in spend:
            spend_out["resetsAt"] = spend["resets_at"]
        cell = oauth.fresh_reset_strings(spend)
        if cell:
            spend_out["countdown"], spend_out["clock"] = cell
        out["spend"] = spend_out
    if "scoped" in usage:
        out["scoped"] = [_scoped_window_to_json(w, fetched_at) for w in usage["scoped"]]
    return out


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _window_from_json(window: object, label: str) -> dict:
    """One JSON window back to the internal shape, fetch-time strings rebuilt."""
    if not isinstance(window, dict):
        raise ValueError(f"{label} must be an object")
    pct = window.get("pct")
    if not _is_number(pct) or pct < 0:
        raise ValueError(f"{label}.pct must be a non-negative number")
    out: dict = {"pct": float(pct)}
    resets_at = window.get("resetsAt")
    if resets_at is not None:
        if not isinstance(resets_at, str):
            raise ValueError(f"{label}.resetsAt must be an ISO-8601 string")
        try:
            out["countdown"], out["clock"] = oauth.format_reset(resets_at)
        except (ValueError, TypeError):
            raise ValueError(f"{label}.resetsAt is not an ISO-8601 time: {resets_at!r}")
        out["resets_at"] = resets_at
    return out


def _spend_from_json(spend: object) -> dict:
    """The ``spend`` JSON object back to the internal shape.

    ``limit`` and ``remaining`` are null together for an account with no
    monthly cap and numbers otherwise, and ``remaining`` must equal
    ``limit - used`` so an edited document cannot claim money the cap does
    not leave. That holds for ``remainingBasis: limit``; for ``balance``
    (money left from an entered balance) ``remaining`` is a non-negative
    number not above ``limit - used`` and ``balanceEnteredAt`` names the
    entry. ``pct`` is null when the API sent no utilization (always so
    for an uncapped account). ``reported`` names the measurement:
    ``dollars`` (the usage endpoint) or ``fraction`` (a setup-token
    account's reply headers, see :func:`_fraction_spend_from_json`).
    """
    if not isinstance(spend, dict):
        raise ValueError("spend must be an object")
    reported = spend.get("reported")
    if reported == oauth.SPEND_REPORTED_FRACTION:
        return _fraction_spend_from_json(spend)
    if reported != oauth.SPEND_REPORTED_DOLLARS:
        raise ValueError(
            f"spend.reported must be {oauth.SPEND_REPORTED_DOLLARS!r} or "
            f"{oauth.SPEND_REPORTED_FRACTION!r}, not {reported!r}"
        )
    used = spend.get("used")
    if not _is_number(used) or used < 0:
        raise ValueError("spend.used must be a non-negative number")
    for key in ("limit", "remaining", "pct"):
        if key not in spend:
            raise ValueError(f"spend.{key} is missing (null means no cap or no figure)")
    limit, remaining, pct = spend["limit"], spend["remaining"], spend["pct"]
    basis = _remaining_basis_from_json(spend)
    if limit is not None:
        if not _is_number(limit):
            raise ValueError("spend.limit must be a number or null")
        if limit < 0:
            raise ValueError("spend.limit must be non-negative")
    if basis == oauth.REMAINING_BASIS_LIMIT:
        if limit is None:
            if remaining is not None:
                raise ValueError("spend.remaining must be null when spend.limit is null")
        elif not _is_number(remaining):
            raise ValueError("spend.remaining must be a number when spend.limit is set")
        elif not math.isclose(remaining, limit - used, abs_tol=0.005):
            raise ValueError(
                f"spend.remaining {remaining!r} is not spend.limit - spend.used"
            )
    else:
        # Money left from an entered balance: never negative, never above
        # the room the limit leaves.
        if not _is_number(remaining) or remaining < 0:
            raise ValueError(
                "spend.remaining must be a non-negative number for a balance basis"
            )
        if limit is not None and remaining > max(0.0, limit - used) + 0.005:
            raise ValueError(
                f"spend.remaining {remaining!r} is above spend.limit - spend.used"
            )
        entered_at = spend.get("balanceEnteredAt")
        if not isinstance(entered_at, str):
            raise ValueError(
                "spend.balanceEnteredAt must be a string for a balance basis"
            )
    if pct is not None and (not _is_number(pct) or pct < 0):
        raise ValueError("spend.pct must be a non-negative number or null")
    if not isinstance(spend.get("currency"), str):
        raise ValueError("spend.currency must be a string")
    if not isinstance(spend.get("limitReached"), bool):
        raise ValueError("spend.limitReached must be a boolean")
    out: dict = {
        "reported": reported,
        "used": float(used),
        "limit": float(limit) if limit is not None else None,
        "remaining": float(remaining) if remaining is not None else None,
        "remaining_basis": basis,
        "pct": float(pct) if pct is not None else None,
        "currency": spend["currency"],
        "limit_reached": spend["limitReached"],
    }
    if basis == oauth.REMAINING_BASIS_BALANCE:
        out["balance_entered_at"] = spend["balanceEnteredAt"]
    if "capSource" in spend:
        if spend["capSource"] != oauth.CAP_SOURCE_CONFIG:
            raise ValueError(
                f"spend.capSource must be {oauth.CAP_SOURCE_CONFIG!r} when "
                f"present, not {spend['capSource']!r}"
            )
        if limit is None:
            raise ValueError("spend.limit must be set when spend.capSource is set")
        out["cap_source"] = oauth.CAP_SOURCE_CONFIG
        out["disabled_reason"] = _disabled_reason_from_json(spend)
    _spend_reset_from_json(spend, out)
    return out


def _remaining_basis_from_json(spend: dict) -> str:
    """A dollars spend's ``remainingBasis``: ``limit`` or ``balance``."""
    if "remainingBasis" not in spend:
        raise ValueError("spend.remainingBasis is missing")
    basis = spend["remainingBasis"]
    if basis not in (oauth.REMAINING_BASIS_LIMIT, oauth.REMAINING_BASIS_BALANCE):
        raise ValueError(
            f"spend.remainingBasis must be {oauth.REMAINING_BASIS_LIMIT!r} or "
            f"{oauth.REMAINING_BASIS_BALANCE!r} for a dollars spend, not {basis!r}"
        )
    return basis


def _disabled_reason_from_json(spend: dict) -> str | None:
    if "disabledReason" not in spend:
        raise ValueError("spend.disabledReason is missing (null means none)")
    reason = spend["disabledReason"]
    if reason is not None and not isinstance(reason, str):
        raise ValueError("spend.disabledReason must be a string or null")
    return reason


def _fraction_spend_from_json(spend: dict) -> dict:
    """A ``reported: fraction`` spend back to the internal shape.

    The reply headers carry only a share of the monthly cap, so ``used``,
    ``limit``, ``remaining`` and ``currency`` must all be present and null;
    ``pct`` is the share used (null when the reply sent none) and
    ``disabledReason`` the reply's reason credits are off, or null.
    """
    for key in ("used", "limit", "remaining", "remainingBasis", "currency"):
        if key not in spend or spend[key] is not None:
            raise ValueError(f"spend.{key} must be null for a fraction spend")
    if "pct" not in spend:
        raise ValueError("spend.pct is missing (null means no figure)")
    pct = spend["pct"]
    if pct is not None and (not _is_number(pct) or pct < 0):
        raise ValueError("spend.pct must be a non-negative number or null")
    if not isinstance(spend.get("limitReached"), bool):
        raise ValueError("spend.limitReached must be a boolean")
    reason = _disabled_reason_from_json(spend)
    out: dict = {
        "reported": oauth.SPEND_REPORTED_FRACTION,
        "used": None,
        "limit": None,
        "remaining": None,
        "pct": float(pct) if pct is not None else None,
        "currency": None,
        "limit_reached": spend["limitReached"],
        "disabled_reason": reason,
    }
    _spend_reset_from_json(spend, out)
    return out


def _spend_reset_from_json(spend: dict, out: dict) -> None:
    """Copy a spend's ``resetsAt`` into ``out`` with its fetch-time strings
    rebuilt; nothing when it is null or absent."""
    resets_at = spend.get("resetsAt")
    if resets_at is not None:
        if not isinstance(resets_at, str):
            raise ValueError("spend.resetsAt must be an ISO-8601 string")
        try:
            out["countdown"], out["clock"] = oauth.format_reset(resets_at)
        except (ValueError, TypeError):
            raise ValueError(f"spend.resetsAt is not an ISO-8601 time: {resets_at!r}")
        out["resets_at"] = resets_at


def usage_from_json(usage: object) -> dict:
    """Read a ``usage`` object from ``list --json`` back into the internal dict.

    The inverse of :func:`usage_to_json` for what the API measured: ``pct``,
    ``resetsAt``, the spend amounts and the scoped model names. Everything
    derived at serialization (countdown, clock, pace fields) is dropped, and
    the fetch-time strings are rebuilt from ``resets_at``, which gives the
    shape :func:`oauth.build_usage_result` stores. Raises ``ValueError`` on
    anything malformed, so an importer can refuse a document before writing
    any of it.
    """
    if not isinstance(usage, dict):
        raise ValueError("usage must be an object")
    out: dict = {}
    if "fiveHour" in usage:
        out["five_hour"] = _window_from_json(usage["fiveHour"], "fiveHour")
    if "sevenDay" in usage:
        out["seven_day"] = _window_from_json(usage["sevenDay"], "sevenDay")
    if "spend" in usage:
        out["spend"] = _spend_from_json(usage["spend"])
    if "scoped" in usage:
        if not isinstance(usage["scoped"], list):
            raise ValueError("scoped must be a list")
        scoped = []
        for i, window in enumerate(usage["scoped"]):
            label = f"scoped[{i}]"
            entry = _window_from_json(window, label)
            name = window.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError(f"{label}.name must be a non-empty string")
            scoped.append({"name": name, **entry})
        out["scoped"] = scoped
    if not out:
        raise ValueError("usage carries no windows")
    return out


def usage_fields(
    entry: dict | str | None, fetched_at: float | None = None
) -> tuple[str, dict | None]:
    """Map a collected usage entry to ``(usageStatus, usage|None)``.

    A collected entry is one of: a usage dict, the ``USAGE_TOKEN_EXPIRED`` sentinel
    (active token expired and the refresh was deferred this pass — lock
    contention, unattributable lineage, or a failed persist; retried
    automatically — or a live session's credential refused, which only that
    session may renew), the ``USAGE_API_KEY`` sentinel
    (managed API-key account, no subscription quota), the
    ``USAGE_KEYCHAIN_UNAVAILABLE`` sentinel (active Keychain unreadable), the
    ``USAGE_FOREIGN_CREDENTIAL`` sentinel (live credential proven to belong to
    another account; usage suppressed, a switch repairs the drift), the
    ``USAGE_NO_CREDENTIALS`` sentinel, or ``None`` (fetch failed). ``fetched_at``
    is forwarded to ``usage_to_json`` for the weekly pace fields (issue #125).
    """
    if isinstance(entry, dict):
        return "ok", usage_to_json(entry, fetched_at)
    if entry == USAGE_TOKEN_EXPIRED:
        return "token_expired", None
    if entry == USAGE_API_KEY:
        return "api_key", None
    if entry == USAGE_KEYCHAIN_UNAVAILABLE:
        return "keychain_unavailable", None
    if entry == USAGE_RELOGIN_REQUIRED:
        return "relogin_required", None
    if entry == USAGE_FOREIGN_CREDENTIAL:
        return "foreign_credential", None
    if isinstance(entry, str):
        return "no_credentials", None
    return "unavailable", None


def account_ref(number: int | None, email: str) -> dict:
    """A minimal account reference, used for switch ``from``/``to``."""
    return {"number": number, "email": email}


def usage_freshness_fields(
    fetched_at: float | None, age_s: float | None
) -> dict:
    """Additive ``usageFetchedAt``/``usageAgeSeconds`` fields describing how
    old the served ``usage`` measurement is (the store may serve last-good
    data on fetch failure). Emitted under these names only alongside a
    non-null ``usage``; ``last_good_usage_fields`` reuses them renamed to
    ``lastGoodFetchedAt``/``lastGoodAgeSeconds`` for null-``usage`` rows."""
    if fetched_at is None:
        return {}
    fields: dict = {"usageFetchedAt": _timestamp(fetched_at)}
    if age_s is not None:
        fields["usageAgeSeconds"] = round(age_s, 1)
    return fields


def _timestamp(epoch_s: float) -> str:
    return (
        datetime.fromtimestamp(epoch_s, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def usage_failure_fields(
    status: str, last_error: str | None, backoff_until: float | None
) -> dict:
    """Additive ``usageError``/``usageRetryAt`` fields for a row that is
    ``unavailable`` with nothing else to say for itself: the last fetch
    failure by kind (``http-429``, ``timeout``, ...) and, while the store is
    backing off from it, when the next attempt is due. Every other status
    already explains the null ``usage``, so nothing is added to it."""
    if status != "unavailable" or not last_error:
        return {}
    out = {"usageError": last_error}
    if backoff_until is not None:
        out["usageRetryAt"] = _timestamp(backoff_until)
    return out


def last_good_usage_fields(
    usage: dict | None, fetched_at: float | None, age_s: float | None
) -> dict:
    """Display-grade last-good usage, separate from decision-grade ``usage``."""
    if not isinstance(usage, dict) or fetched_at is None:
        return {}
    freshness = usage_freshness_fields(fetched_at, age_s)
    out = {
        "lastGoodUsage": usage_to_json(usage, fetched_at),
        "lastGoodFetchedAt": freshness["usageFetchedAt"],
    }
    if "usageAgeSeconds" in freshness:
        out["lastGoodAgeSeconds"] = freshness["usageAgeSeconds"]
    return out


def account_row(
    number: int,
    email: str,
    org_name: str,
    org_uuid: str,
    active: bool,
    usage_entry: dict | str | None,
    *,
    usage_fetched_at: float | None = None,
    usage_age_s: float | None = None,
    last_good_usage: dict | None = None,
    last_error: str | None = None,
    backoff_until: float | None = None,
    alias: str = "",
    disabled: bool = False,
    login_expires_at: str | None = None,
    login_kind: str,
    switch_threshold: float,
) -> dict:
    """A full account row for ``--list``. ``backoff_until`` is the live
    backoff only; a lapsed one is the caller's to withhold.
    ``switch_threshold`` is the account's own switch point in percent
    (``settings.account_switch_point``)."""
    status, usage = usage_fields(usage_entry, usage_fetched_at)
    row = {
        "number": number,
        "email": email,
        "organizationName": org_name,
        "organizationUuid": org_uuid,
        "isOrganization": bool(org_uuid),
        "active": active,
        "usageStatus": status,
        "usage": usage,
    }
    if alias:
        row["alias"] = alias
    # Additive field: present only when the slot is held out of rotation, so
    # existing consumers keying on the base schema are unaffected.
    if disabled:
        row["disabled"] = True
    # Additive field: when the stored login records the expiry of its refresh
    # token (see ``oauth.login_expires_at_iso``), scripts can warn ahead of the
    # ``relogin_required`` that follows; absent when the login carries none.
    if login_expires_at:
        row["loginExpiresAt"] = login_expires_at
    # How the slot logs in, so a reader knows which renewal a near expiry
    # needs: "oauth" (a browser login, renewed by /login), "setup-token" (a
    # one-year `claude setup-token`, renewed by a new token through
    # `cswap add-token`; its expiry is the recorded add time plus a year) or
    # "api-key" (no login to expire).
    row["loginKind"] = login_kind
    # The utilization pct the auto engine switches this account away at:
    # `autoswitch.creditThreshold` while its stored reading has usage-credit
    # room, else `autoswitch.threshold`, read from settings.json.
    row["switchThreshold"] = switch_threshold
    if usage is not None:
        row.update(usage_freshness_fields(usage_fetched_at, usage_age_s))
    else:
        row.update(
            last_good_usage_fields(
                last_good_usage, usage_fetched_at, usage_age_s
            )
        )
        row.update(usage_failure_fields(status, last_error, backoff_until))
    return row


def error_envelope(exc: Exception) -> dict:
    """The structured error payload emitted on a handled ClaudeSwitchError."""
    return {
        "schemaVersion": SCHEMA_VERSION,
        "error": {"type": type(exc).__name__, "message": str(exc)},
    }
