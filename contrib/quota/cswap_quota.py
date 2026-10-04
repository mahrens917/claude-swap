#!/usr/bin/env python3
"""cswap-quota: every claude-swap account's usage, the pool's average rate, and where that rate
runs out.

It reads ``cswap list --json`` and prints one of two forms of that read.

The default is markdown, meant for a chat transcript: a title line first (the Claude mobile app
prefixes a command row's first line with the command's name, which would break a table placed on
that line), then a table of the accounts in the order the switcher will use them, then each usage
window's verdict, then any condition that needs action.

``--lines`` is a short plain-text form, one line per account, then the pace per window, then one
line per condition:

    #2* 5h 14.0% (4h 27m) · 7d 93.0% (5d 4h) RUNS OUT
    #3 5h 15.0% (3h 47m) · 7d 26.0% (5d 21h)
    #5 unknown: last fetch http-503
    7d 88.0%/day vs plan 42.0%/day (+107.0%): out from Sun 7:52 PM CET for 1d 7h

Every time shown is in this machine's local time zone.

A figure older than MAX_FIGURE_AGE_SECONDS, or one cswap no longer trusts (``lastGoodUsage`` on an
unavailable row), prints with the time it was read, never as a current value, and no condition or
pace is worked out from it. If cswap is missing or fails, the error goes to stderr and the exit
status is non-zero: nothing stale or made up is printed in its place.

THE RATE needs a few readings over time, so each run appends one sample per account with a current
figure to a small history file (see ``history_path``) and drops samples older than HISTORY_DAYS. A
window's figure only grows until its reset, so the rise between two samples of one window is what
was spent between them. The rate is measured since each account's own last reset: each account's
current window is one measurement of what the WHOLE pool spent over that span, and the measurements
are combined weighted by their hours. With no history yet, each account's first sample of a window
counts its whole figure as spent since the window opened, so the first run already has a rate.

THE PLAN is what the pool renews at: each account's weekly window up to the switcher's threshold
(the switcher moves off an account there) per seven days, and each account's full five-hour window
per five hours. Every account counts the same, because cswap reports each window as a percent of
that account's own plan and gives no plan size.

THE PROJECTION runs the rate forward over each account's remaining room, refilling an account at
its reset, and reports when the pool first runs dry and for how long, or the room that renews
unspent when it never does.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

CSWAP_LIST = ("cswap", "list", "--json")
CSWAP_THRESHOLD = ("cswap", "config", "get", "autoswitch.threshold", "--json")
CSWAP_STRATEGY = ("cswap", "config", "get", "autoswitch.strategy", "--json")
CSWAP_TIMEOUT_SECONDS = 60
# The oldest figure, in seconds, still treated as current. cswap keeps an account at usageStatus ok
# while it backs off from a failed refresh, so an ok row can carry a figure hours old.
MAX_FIGURE_AGE_SECONDS = 900.0
# Samples older than this are dropped from the history: the rate looks back at most one week (the
# longest window) from now, plus the one reading just before that week starts.
HISTORY_DAYS = 8

# A weekly window at the switcher's threshold that resets within this many hours expires unused,
# because the switcher only moves to accounts under its threshold.
THRESHOLD_EXPIRY_HOURS = 48
# Extra-usage spend at this share of its monthly cap or more is money billed beyond the plan.
SPEND_WARN_FRACTION = 0.8
# A login expiring within this many days needs a /login before it lapses.
LOGIN_WARN_DAYS = 3

# Each window: cswap's key and its length in hours.
WINDOW_HOURS = {"sevenDay": 168.0, "fiveHour": 5.0}
# Each window the --lines form covers: cswap's key, its label, the rate's unit, and the unit's hours.
PACE_WINDOWS = (("sevenDay", "7d", "day", 24.0), ("fiveHour", "5h", "hour", 1.0))
# The same for the markdown form, with the window's name in a sentence.
SENTENCE_WINDOWS = (("sevenDay", "Week", "day", 24.0), ("fiveHour", "5-hour", "hour", 1.0))

# cswap's autoswitch strategies: the first two pick the account whose weekly window resets soonest,
# "best" picks the one with the most headroom (100 minus its highest window).
SOONEST_RESET_STRATEGIES = ("consume-first", "dynamic")
MOST_HEADROOM_STRATEGY = "best"

# The projection's step: fine enough that a reset lands within a quarter hour of its time.
STEP_HOURS = 0.25
# Points a step may leave undrawn and still count as served: float subtraction's residue.
UNMET_TOLERANCE = 1e-9
# cswap stamps one window's reset a second apart between reads (…59 beside …00), so two samples
# whose resets lie within this many seconds belong to the same window.
RESET_JITTER_SECONDS = 60


class QuotaError(Exception):
    """cswap could not be read, or the history file is not what this program writes."""


# ---------------------------------------------------------------------------------------------
# Reading cswap


def run_cswap(argv: tuple[str, ...]) -> dict:
    """``argv``'s JSON payload, raising QuotaError carrying cswap's own error."""
    command = " ".join(argv)
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=CSWAP_TIMEOUT_SECONDS)
    except FileNotFoundError as exc:
        raise QuotaError(f"cswap is not on PATH, so `{command}` cannot run") from exc
    except subprocess.TimeoutExpired as exc:
        raise QuotaError(f"`{command}` did not answer within {CSWAP_TIMEOUT_SECONDS} s") from exc
    if proc.returncode != 0:
        raise QuotaError(f"`{command}` exited {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}")
    try:
        payload = json.loads(proc.stdout)
    except ValueError as exc:
        raise QuotaError(f"`{command}` printed no JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise QuotaError(f"`{command}` printed JSON that is not an object: {proc.stdout.strip()!r}")
    return payload


def read_threshold() -> float:
    """The switcher's weekly threshold in percent; no default stands in for an unreadable one."""
    value = run_cswap(CSWAP_THRESHOLD).get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 < value <= 100.0:
        raise QuotaError(f"`{' '.join(CSWAP_THRESHOLD)}` gave no percent above 0 and at most 100: value={value!r}")
    return float(value)


def read_strategy() -> str:
    """The switcher's strategy, one this program knows how to order accounts by."""
    value = run_cswap(CSWAP_STRATEGY).get("value")
    if value not in (*SOONEST_RESET_STRATEGIES, MOST_HEADROOM_STRATEGY):
        raise QuotaError(f"`{' '.join(CSWAP_STRATEGY)}` gave a strategy this program does not know: value={value!r}")
    return value


# ---------------------------------------------------------------------------------------------
# Formatting


def _instant(iso: str) -> datetime:
    # Python 3.10's fromisoformat does not accept a trailing Z
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _local(moment: datetime) -> datetime:
    """``moment`` in the local zone, to the nearest minute: cswap stamps a reset a second before the
    hour as often as on it (23:59:59 beside 00:00:00), and both should read as the hour."""
    return (moment + timedelta(seconds=30)).astimezone().replace(second=0, microsecond=0)


def _hour_12(local: datetime) -> str:
    return str(local.hour % 12 or 12)


def clock(moment: datetime) -> str:
    """A time of day in the local zone, 12-hour: ``10:14 PM CET``."""
    local = _local(moment)
    return f"{_hour_12(local)}:{local:%M %p} {local:%Z}"


def day_clock(moment: datetime, zone_named: bool = True) -> str:
    """A weekday and time in the local zone, minutes left out on the hour: ``Thu 3 AM CET``. A table
    cell under a header that already names the zone leaves it out."""
    local = _local(moment)
    time = f"{_hour_12(local)} {local:%p}" if local.minute == 0 else f"{_hour_12(local)}:{local:%M %p}"
    return f"{local:%a} {time}" + (f" {local:%Z}" if zone_named else "")


def zone_name(moment: datetime) -> str:
    return f"{moment.astimezone():%Z}"


def pct(value: float) -> str:
    """A percentage to one decimal place, which also hides cswap's float residue (14.000000000000002)."""
    return f"{value:.1f}%"


def duration(hours: float) -> str:
    whole = int(round(hours))
    days, rest = divmod(whole, 24)
    return f"{days}d {rest}h" if days else f"{rest}h"


def _is_out_of_date(account: dict) -> bool:
    return account["usageAgeSeconds"] > MAX_FIGURE_AGE_SECONDS


def is_current(account: dict) -> bool:
    return account["usageStatus"] == "ok" and not _is_out_of_date(account)


def _as_of(fetched_at: str, age_seconds: float) -> str:
    """When a shown-but-not-current figure was read, and how long ago."""
    hours, minutes = divmod(int(age_seconds // 60), 60)
    age = f"{hours}h {minutes}m" if hours else f"{minutes}m"
    return f"as of {clock(_instant(fetched_at))} ({age} old)"


def _unread_reason(account: dict) -> str:
    """Why cswap could not read the account, from its usageError/usageRetryAt fields."""
    # cswap writes usageError only when the last fetch failed with a recorded error
    if "usageError" not in account:
        return "no fetch error recorded"
    error = account["usageError"]
    # cswap writes usageRetryAt only while it is backing off from that failure
    if "usageRetryAt" not in account:
        return f"last fetch {error}"
    retry = clock(_instant(account["usageRetryAt"]))
    if error == "http-429":
        return f"rate-limited until {retry}"
    return f"last fetch {error}, retry {retry}"


def windows_text(usage: dict) -> str:
    """The five-hour and weekly windows with their countdowns, and RUNS OUT when cswap says the
    week is on pace to end before its reset."""
    five = usage["fiveHour"]
    seven = usage["sevenDay"]
    line = f"5h {pct(five['pct'])}"
    # cswap writes no countdown for a window not opened since its last reset
    if "countdown" in five:
        line += f" ({five['countdown']})"
    line += f" · 7d {pct(seven['pct'])}"
    if "countdown" in seven:
        line += f" ({seven['countdown']})"
    # cswap adds its weekly pace verdict only once the week is about a day old
    if "willLastToReset" in seven and seven["willLastToReset"] is False:
        line += " RUNS OUT"
    return line


def account_text(account: dict) -> str:
    """The account's usage, or its status when it has none to show."""
    status = account["usageStatus"]
    if status == "unavailable":
        reason = _unread_reason(account)
        # cswap adds lastGoodUsage only when it holds an earlier good reading of the account
        if "lastGoodUsage" not in account:
            return f"unknown: {reason}"
        windows = windows_text(account["lastGoodUsage"])
        return f"{windows}, {_as_of(account['lastGoodFetchedAt'], account['lastGoodAgeSeconds'])}, {reason}"
    if status != "ok":
        return status
    windows = windows_text(account["usage"])
    if _is_out_of_date(account):
        return f"{windows}, {_as_of(account['usageFetchedAt'], account['usageAgeSeconds'])}"
    return windows


def account_line(account: dict) -> str:
    """The account's one short line, ``*`` marking the active account."""
    tag = f"#{account['number']}" + ("*" if account["active"] else "")
    return f"{tag} {account_text(account)}"


def _current_conditions(account: dict, threshold: float, now: datetime) -> list[str]:
    number = account["number"]
    usage = account["usage"]
    lines = []
    seven = usage["sevenDay"]
    hours_to_reset = (_instant(seven["resetsAt"]) - now).total_seconds() / 3600
    if seven["pct"] >= threshold and hours_to_reset <= THRESHOLD_EXPIRY_HOURS:
        lines.append(
            f"#{number}: 7d at the switcher's {threshold:g}% threshold, resets in {seven['countdown']};"
            f" its remaining {pct(100 - seven['pct'])} expires unused unless `! cswap switch {number}`"
        )
    # cswap writes spend as null, or leaves it out, for an account with extra usage switched off
    spend = usage["spend"] if "spend" in usage else None
    if spend is not None and spend["used"] >= SPEND_WARN_FRACTION * spend["limit"]:
        lines.append(f"#{number}: extra usage ${spend['used']:.2f} of ${spend['limit']:.2f} this month, billed beyond the plan")
    return lines


def condition_lines(accounts: list[dict], threshold: float, now: datetime) -> list[str]:
    """One line per condition that needs action, in account order."""
    lines = []
    for account in accounts:
        number = account["number"]
        status = account["usageStatus"]
        if status == "unavailable" or (status == "ok" and _is_out_of_date(account)):
            if account["active"]:
                lines.append(f"#{number}: the active account, so the switcher is deciding on figures that are not current")
        elif status != "ok":
            lines.append(f"#{number}: {status}, needs `/login` as that account, then `! cswap add --slot {number}`")
        else:
            lines.extend(_current_conditions(account, threshold, now))
        # cswap writes loginExpiresAt only when the stored login records its expiry
        if "loginExpiresAt" not in account:
            continue
        expires = _instant(account["loginExpiresAt"])
        if (expires - now).total_seconds() <= LOGIN_WARN_DAYS * 86_400:
            lines.append(f"#{number}: login expires {_local(expires):%a %b} {_local(expires).day}, {clock(expires)}, `/login` as that account before it lapses")
    return lines


# ---------------------------------------------------------------------------------------------
# Rotation order


def _usable_from(account: dict, threshold: float) -> datetime | None:
    """When the account can next take work: None when it can now (every window under the threshold),
    else the latest reset among the windows holding it at the threshold."""
    usage = account["usage"]
    blocked = [usage[key] for key in ("sevenDay", "fiveHour") if usage[key]["pct"] >= threshold]
    if not blocked:
        return None
    return max(_instant(window["resetsAt"]) for window in blocked)


def rotation_bucket(account: dict, threshold: float) -> str:
    if account["active"]:
        return "active"
    if not is_current(account):
        return "unread"
    return "usable" if _usable_from(account, threshold) is None else "held"


def _weekly_reset(account: dict) -> datetime:
    return _instant(account["usage"]["sevenDay"]["resetsAt"])


def _headroom(account: dict) -> float:
    usage = account["usage"]
    return 100.0 - max(usage["fiveHour"]["pct"], usage["sevenDay"]["pct"])


def rotation_order(accounts: list[dict], threshold: float, strategy: str) -> list[dict]:
    """The accounts in the order the switcher will use them: the active one; then each that can take
    work now, soonest weekly reset first (consume-first, dynamic) or most headroom first (best); then
    each held at the threshold by when it can take work again; then each with no current figure."""
    buckets: dict[str, list[dict]] = {"active": [], "usable": [], "held": [], "unread": []}
    for account in accounts:
        buckets[rotation_bucket(account, threshold)].append(account)
    if strategy == MOST_HEADROOM_STRATEGY:
        buckets["usable"].sort(key=_headroom, reverse=True)
    else:
        buckets["usable"].sort(key=_weekly_reset)
    buckets["held"].sort(key=lambda a: _usable_from(a, threshold))
    return buckets["active"] + buckets["usable"] + buckets["held"] + buckets["unread"]


# ---------------------------------------------------------------------------------------------
# History


def history_path() -> Path:
    """$CSWAP_QUOTA_HISTORY, else $XDG_STATE_HOME/cswap-quota/history.jsonl, else
    ~/.local/state/cswap-quota/history.jsonl."""
    explicit = os.environ.get("CSWAP_QUOTA_HISTORY")
    if explicit:
        return Path(explicit)
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state) if state else Path.home() / ".local" / "state"
    return base / "cswap-quota" / "history.jsonl"


@dataclass(frozen=True)
class Sample:
    """One account's cumulative figure in one window, read at ``at`` (epoch seconds); ``resets_at``
    is None for a window not opened since its last reset."""

    at: float
    resets_at: float | None
    used: float

    def same_window(self, other: Sample) -> bool:
        if self.resets_at is None or other.resets_at is None:
            return self.resets_at is None and other.resets_at is None
        return abs(self.resets_at - other.resets_at) < RESET_JITTER_SECONDS


def _read_history_rows(path: Path) -> list[dict]:
    """Every row of the history file; a missing file is a first run with no history."""
    if not path.exists():
        return []
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise QuotaError(f"{path} line {number} is not JSON: {exc}") from exc
        if not isinstance(row, dict) or not {"at", "account", "fiveHour", "sevenDay"} <= row.keys():
            raise QuotaError(f"{path} line {number} is not a cswap-quota sample: {line!r}")
        rows.append(row)
    return rows


def history_samples(rows: list[dict], key: str) -> dict[str, list[Sample]]:
    """Every account's samples of one window, keyed by account email, oldest first."""
    samples: dict[str, list[Sample]] = {}
    for row in rows:
        window = row[key]
        samples.setdefault(row["account"], []).append(Sample(row["at"], window["resetsAt"], window["pct"]))
    for rows_of_account in samples.values():
        rows_of_account.sort(key=lambda sample: sample.at)
    return samples


def _resets_epoch(window: dict) -> float | None:
    # cswap writes resetsAt only for a window opened since its last reset
    return _instant(window["resetsAt"]).timestamp() if "resetsAt" in window else None


def sample_row(account: dict, now: datetime) -> dict:
    """The history line for one account's current figure."""
    usage = account["usage"]
    return {
        "at": now.timestamp(),
        "account": account["email"],
        **{key: {"resetsAt": _resets_epoch(usage[key]), "pct": usage[key]["pct"]} for key in WINDOW_HOURS},
    }


def write_history(path: Path, rows: list[dict], accounts: list[dict], now: datetime) -> None:
    """Rewrite the history: the rows still inside HISTORY_DAYS, plus one sample per account whose
    figure is current. The file is replaced whole, so a crash mid-write leaves the previous one."""
    cutoff = now.timestamp() - HISTORY_DAYS * 86_400
    kept = [row for row in rows if row["at"] >= cutoff]
    kept.extend(sample_row(account, now) for account in accounts if is_current(account))
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(path.name + ".tmp")
    scratch.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in kept), encoding="utf-8")
    scratch.replace(path)


# ---------------------------------------------------------------------------------------------
# Rate and projection


@dataclass(frozen=True)
class Room:
    """One account's room in one window now: points left before its limit, and hours to its reset."""

    left: float
    resets_in_hours: float | None


@dataclass(frozen=True)
class Pace:
    """One window kind's rate against the plan, and the projection of that rate."""

    rate_per_hour: float
    capacity_per_hour: float
    horizon_hours: float
    # Each stretch with no account left: (start, end) in hours from now, the end being the reset
    # that refills an account (or the horizon)
    dry_spells: tuple[tuple[float, float], ...]
    unspent_at_resets: float

    @property
    def share_of_capacity(self) -> float:
        return self.rate_per_hour / self.capacity_per_hour

    @property
    def first_dry_in_hours(self) -> float | None:
        return self.dry_spells[0][0] if self.dry_spells else None


def _share_after(start: float, end: float, since: float) -> float:
    """The share of [start, end] after ``since``: spend between two readings is taken as steady, so a
    rise that straddles the span's start counts only its part inside the span."""
    if end <= start:
        return 1.0
    return min(1.0, max(0.0, (end - max(start, since)) / (end - start)))


def spent_since(samples: list[Sample], since: float, length_hours: float) -> float:
    """The points one account spent in one window kind after ``since``, from its samples."""
    spent = 0.0
    previous: Sample | None = None
    for sample in samples:
        if sample.at > since:
            if previous is not None and previous.same_window(sample):
                # A fall between two samples (cswap's rounding) adds nothing
                spent += max(0.0, sample.used - previous.used) * _share_after(previous.at, sample.at, since)
            elif sample.resets_at is not None:
                # The account's first sample of this window: its whole figure was spent between the
                # window's opening and this read
                opened = sample.resets_at - length_hours * 3600
                spent += sample.used * _share_after(opened, sample.at, since)
        previous = sample
    return spent


def fleet_rate(histories: list[list[Sample]], resets: list[float | None], now: float, length_hours: float) -> float:
    """Points per hour across the pool. Each account's current window, from its opening to now, is
    one measurement of what EVERY account spent over that span; the measurements combine weighted by
    their length. One account's own percent over its own hours would overstate the pool, because
    the accounts are spent one at a time."""
    spent = 0.0
    hours = 0.0
    for resets_at in resets:
        # An unopened window has no span since a reset
        if resets_at is None:
            continue
        opened = resets_at - length_hours * 3600
        spent += sum(spent_since(history, opened, length_hours) for history in histories)
        hours += (now - opened) / 3600
    # Every window unopened: no account spent anything in the last window length
    if hours == 0.0:
        return 0.0
    return spent / hours


def _refill(left: list[float], resets: list[float | None], clock_hours: float, ceiling: float, length_hours: float) -> tuple[float, float | None]:
    """Reset every window due by ``clock_hours`` to ``ceiling``; returns the points those resets let
    expire and the earliest of their reset times (None when none was due)."""
    expired = 0.0
    earliest: float | None = None
    for index, reset in enumerate(resets):
        if reset is not None and reset <= clock_hours:
            expired += left[index]
            left[index] = ceiling
            resets[index] = reset + length_hours
            earliest = reset if earliest is None else min(earliest, reset)
    return expired, earliest


def _draw(left: list[float], resets: list[float | None], clock_hours: float, need: float, length_hours: float) -> float:
    """Draw ``need`` points from the accounts in order; returns the points no account had."""
    for index, room in enumerate(left):
        draw = min(need, room)
        if draw > 0 and resets[index] is None:
            # An unopened window opens on its first use
            resets[index] = clock_hours + length_hours
        left[index] -= draw
        need -= draw
    return need


class _DrySpells:
    """The stretches a projection spends with no account left: one opens when the pool empties and
    closes at the reset that next refills an account, at that reset's own time."""

    def __init__(self) -> None:
        self.closed: list[tuple[float, float]] = []
        self.open_since: float | None = None

    def empty_at(self, hours: float) -> None:
        if self.open_since is None:
            self.open_since = hours

    def refilled_at(self, hours: float) -> None:
        if self.open_since is not None:
            self.closed.append((self.open_since, hours))
            self.open_since = None


def project(rooms: list[Room], ceiling: float, length_hours: float, rate_per_hour: float) -> tuple[float, list[tuple[float, float]], float]:
    """Run ``rate_per_hour`` forward over every account's room, refilling each at its reset to
    ``ceiling``. Returns (horizon, dry spells as (start, end) hours from now, points unspent at resets)."""
    left = [room.left for room in rooms]
    resets = [room.resets_in_hours for room in rooms]
    known = [reset for reset in resets if reset is not None]
    horizon = max(known) if known else length_hours
    step_need = rate_per_hour * STEP_HOURS
    spells = _DrySpells()
    clock_hours = 0.0
    unspent, _ = _refill(left, resets, clock_hours, ceiling, length_hours)
    # The resets due at the horizon are counted; nothing is drawn past it
    while clock_hours < horizon:
        unmet = _draw(left, resets, clock_hours, step_need, length_hours)
        if unmet > UNMET_TOLERANCE:
            # The pool empties part way through the step, after the share it could still serve
            spells.empty_at(clock_hours + STEP_HOURS * (1.0 - unmet / step_need))
        clock_hours += STEP_HOURS
        expired, refilled_at = _refill(left, resets, clock_hours, ceiling, length_hours)
        unspent += expired
        if refilled_at is not None:
            spells.refilled_at(refilled_at)
    spells.refilled_at(horizon)
    return horizon, spells.closed, unspent


def window_pace(accounts: list[dict], history: dict[str, list[Sample]], key: str, ceiling: float, now: datetime) -> Pace:
    """One window kind's pace over the accounts with a current figure, from their history plus the
    current read as the newest sample."""
    length_hours = WINDOW_HOURS[key]
    histories = []
    resets = []
    rooms = []
    for account in accounts:
        window = account["usage"][key]
        resets_at = _resets_epoch(window)
        current = Sample(now.timestamp(), resets_at, window["pct"])
        # An account never sampled before has only the current read
        earlier = history[account["email"]] if account["email"] in history else []
        histories.append([*earlier, current])
        resets.append(resets_at)
        resets_in = None if resets_at is None else (resets_at - now.timestamp()) / 3600
        rooms.append(Room(max(0.0, ceiling - window["pct"]), resets_in))
    rate = fleet_rate(histories, resets, now.timestamp(), length_hours)
    capacity = len(accounts) * ceiling / length_hours
    horizon, spells, unspent = project(rooms, ceiling, length_hours, rate)
    return Pace(rate, capacity, horizon, tuple(spells), unspent)


def fleet_paces(accounts: list[dict], threshold: float, now: datetime, rows: list[dict]) -> dict[str, Pace]:
    """Each window's pace over the accounts with a current figure, keyed by cswap's window key; empty
    when none has one (an unread account has no current room to project)."""
    readable = [a for a in accounts if is_current(a)]
    if not readable:
        return {}
    return {key: window_pace(readable, history_samples(rows, key), key, ceiling, now) for key, ceiling in (("sevenDay", threshold), ("fiveHour", 100.0))}


# ---------------------------------------------------------------------------------------------
# Output


def _rates(pace: Pace, unit: str, unit_hours: float) -> str:
    share = pace.share_of_capacity - 1.0
    return f"{pct(pace.rate_per_hour * unit_hours)}/{unit} vs plan {pct(pace.capacity_per_hour * unit_hours)}/{unit} ({share:+.1%})"


def _from_now(now: datetime, hours: float) -> datetime:
    return datetime.fromtimestamp(now.timestamp() + hours * 3600, tz=timezone.utc)


def first_dry_spell(pace: Pace, now: datetime) -> str:
    """The first stretch with no account left, ``Sun 7:51 PM CET for 1d 7h``. Later stretches are
    left out: the table's reset times say when room returns."""
    if not pace.dry_spells:
        raise ValueError(f"pace {pace} never runs dry, so it has no first dry stretch")
    start_hours, end_hours = pace.dry_spells[0]
    return f"{day_clock(_from_now(now, start_hours))} for {duration(end_hours - start_hours)}"


def _unread_count(accounts: list[dict]) -> int:
    return sum(1 for a in accounts if not is_current(a))


def pace_lines(paces: dict[str, Pace], accounts: list[dict], now: datetime) -> list[str]:
    """Each window's rate against the plan and its outcome, one short line each."""
    if not paces:
        return ["pace: no account read now, so no rate or projection"]
    lines = []
    for key, label, unit, unit_hours in PACE_WINDOWS:
        pace = paces[key]
        if pace.first_dry_in_hours is None:
            outcome = f"lasts, {pct(pace.unspent_at_resets)} renews unspent over the next {duration(pace.horizon_hours)}"
        else:
            outcome = f"out from {first_dry_spell(pace, now)}"
        lines.append(f"{label} {_rates(pace, unit, unit_hours)}: {outcome}")
    unread = _unread_count(accounts)
    if unread:
        lines.append(f"pace over {len(accounts) - unread} of {len(accounts)} accounts: {unread} not read now")
    return lines


def quota_lines(doc: dict, threshold: float, strategy: str, now: datetime, pace: list[str]) -> list[str]:
    """Each account's line in rotation order, then the pace lines, then each condition."""
    accounts = rotation_order(doc["accounts"], threshold, strategy)
    return [account_line(a) for a in accounts] + pace + condition_lines(accounts, threshold, now)


def _window_sentence(pace: Pace, key: str, name: str, unit: str, unit_hours: float, now: datetime) -> str:
    """One window's verdict as a bold heading, then bullets: its rate against the plan, then the
    first stretch with no account left, or the room that renews unspent when it lasts."""
    bullets = [f"Using {_rates(pace, unit, unit_hours)}"]
    if pace.first_dry_in_hours is None:
        verdict = "lasts"
        if key == "sevenDay":
            bullets.append(f"{pct(pace.unspent_at_resets)} renews unspent over the next {duration(pace.horizon_hours)}")
    else:
        verdict = "runs out"
        bullets.append(f"Out from {first_dry_spell(pace, now)}")
    return f"**{name}: {verdict}**\n" + "\n".join(f"- {bullet}" for bullet in bullets)


def pace_sentences(paces: dict[str, Pace], accounts: list[dict], now: datetime) -> list[str]:
    if not paces:
        return ["No account is read now, so there is no rate or projection."]
    sentences = [_window_sentence(paces[key], key, name, unit, unit_hours, now) for key, name, unit, unit_hours in SENTENCE_WINDOWS]
    unread = _unread_count(accounts)
    if unread:
        sentences.append(f"Counted over {len(accounts) - unread} of {len(accounts)} accounts; {unread} not read now.")
    return sentences


def _five_cell(window: dict) -> str:
    if "countdown" in window:
        return f"{pct(window['pct'])} · {window['countdown']}"
    return pct(window["pct"])


def _week_cell(window: dict) -> str:
    if "resetsAt" in window:
        return f"{pct(window['pct'])} · {day_clock(_instant(window['resetsAt']), zone_named=False)}"
    return pct(window["pct"])


# The table's word for each place in the switcher's order; the first usable account is "next".
ROTATION_LABELS = {"active": "now", "held": "full", "unread": "unread"}


def table_rows(accounts: list[dict], threshold: float) -> list[str]:
    """One markdown row per account in rotation order. An account is named by its place in the
    order, not its number or email: what matters at a glance is when each one has room."""
    rows = []
    usable_seen = 0
    for account in accounts:
        bucket = rotation_bucket(account, threshold)
        if bucket == "usable":
            label = "next" if usable_seen == 0 else "then"
            usable_seen += 1
        else:
            label = ROTATION_LABELS[bucket]
        if bucket == "unread":
            rows.append(f"| {label} | | {account_text(account)} |")
            continue
        usage = account["usage"]
        tag = f"**{label}**" if bucket == "active" else label
        rows.append(f"| {tag} | {_five_cell(usage['fiveHour'])} | {_week_cell(usage['sevenDay'])} |")
    return rows


def quota_markdown(doc: dict, threshold: float, strategy: str, now: datetime, paces: dict[str, Pace]) -> str:
    """The markdown form: a title line, the accounts in the switcher's order, the pace, then each
    condition that needs action."""
    accounts = rotation_order(doc["accounts"], threshold, strategy)
    table = [f"| Account | 5-hour | Week · resets {zone_name(now)} |", "|---|---|---|", *table_rows(accounts, threshold)]
    parts = [f"Claude usage · {day_clock(now)}", "\n".join(table), "\n\n".join(pace_sentences(paces, accounts, now))]
    conditions = condition_lines(accounts, threshold, now)
    if conditions:
        parts.append("**Needs action**\n" + "\n".join(f"- {line}" for line in conditions))
    return "\n\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cswap-quota", description="Every claude-swap account's usage, the pool's average rate, and where that rate runs out.")
    parser.add_argument("--lines", action="store_true", help="short plain lines instead of the markdown table")
    args = parser.parse_args(argv)
    try:
        doc = run_cswap(CSWAP_LIST)
        threshold = read_threshold()
        strategy = read_strategy()
        now = datetime.now(timezone.utc)
        path = history_path()
        rows = _read_history_rows(path)
        # The pace reads the history from before this run plus the current read as its newest sample
        paces = fleet_paces(doc["accounts"], threshold, now, rows)
        write_history(path, rows, doc["accounts"], now)
    except QuotaError as exc:
        print(f"cswap-quota: {exc}", file=sys.stderr)
        return 1
    if args.lines:
        print("\n".join(quota_lines(doc, threshold, strategy, now, pace_lines(paces, doc["accounts"], now))))
    else:
        print(quota_markdown(doc, threshold, strategy, now, paces))
    return 0


if __name__ == "__main__":
    sys.exit(main())
