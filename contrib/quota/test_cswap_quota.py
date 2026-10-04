"""Tests for cswap_quota.py. Run with ``pytest`` from this directory."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import cswap_quota as q
from cswap_quota import Pace, Room, Sample

NOW = 1_788_400_000  # Thu 2026-09-03 01:46:40 UTC
CLOCK = datetime.fromtimestamp(NOW, tz=timezone.utc)
THRESHOLD = 99.0
HOUR = 3600
FRESH_AGE = 56.1
# A fixed-offset zone with no daylight saving, so expected times do not depend on the date
JAPAN = "JST-9"
# A zone with daylight saving, written as a POSIX rule so no tz database is needed
CENTRAL_EUROPE = "CET-1CEST,M3.5.0,M10.5.0/3"


def _set_zone(zone: str | None) -> None:
    if zone is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = zone
    time.tzset()


@pytest.fixture(autouse=True)
def local_zone():
    """Every test runs with the machine's local zone forced to JAPAN, then restored."""
    saved = os.environ.get("TZ")
    _set_zone(JAPAN)
    yield
    _set_zone(saved)


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def aged(row: dict, age: float, now: float = NOW) -> dict:
    fetched_at = datetime.fromtimestamp(now - age, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return row | {"usageFetchedAt": fetched_at, "usageAgeSeconds": age}


def row(
    number: int,
    five: float,
    seven: float,
    *,
    active: bool = False,
    will_last: bool = True,
    spend: tuple[float, float] | None = None,
    five_reset: float | None = NOW + 3600,
    status: str = "ok",
    now: float = NOW,
    email: str | None = None,
) -> dict:
    """One account row shaped as ``cswap list --json`` prints it."""
    five_hour: dict = {"pct": five}
    if five_reset is not None:
        five_hour |= {"resetsAt": iso(five_reset), "countdown": "1h 0m"}
    seven_day = {"pct": seven, "resetsAt": iso(now + 86_400), "countdown": "1d 0h", "willLastToReset": will_last}
    usage: dict = {"fiveHour": five_hour, "sevenDay": seven_day}
    if spend is not None:
        usage["spend"] = {"used": spend[0], "limit": spend[1], "currency": "USD"}
    account = {
        "number": number,
        "email": email or f"user{number}@example.com",
        "active": active,
        "usageStatus": status,
        "usage": usage if status == "ok" else None,
    }
    return aged(account, FRESH_AGE, now) if status == "ok" else account


def lines(*accounts: dict, strategy: str = "consume-first") -> list[str]:
    return q.quota_lines({"accounts": list(accounts)}, THRESHOLD, strategy, CLOCK, [])


# ---------------------------------------------------------------------------------------------
# Short lines


def test_each_account_prints_one_line_with_the_active_one_starred() -> None:
    """Asserts: each account is one line of its five-hour and weekly use with their countdowns, the
    active account carries *, an unopened five-hour window shows no countdown, and a week on pace to
    end before its reset says RUNS OUT; with no condition holding, nothing else prints."""
    out = lines(row(1, 2.0, 24.0, active=True), row(2, 0.0, 98.0, will_last=False, five_reset=None))
    assert out == ["#1* 5h 2.0% (1h 0m) · 7d 24.0% (1d 0h)", "#2 5h 0.0% · 7d 98.0% (1d 0h) RUNS OUT"]


def test_a_weekly_window_without_optional_fields_prints_its_percentage_alone() -> None:
    """Asserts: a weekly window with no countdown and no willLastToReset prints its percentage alone."""
    account = row(7, 3.0, 5.0)
    del account["usage"]["sevenDay"]["countdown"]
    del account["usage"]["sevenDay"]["willLastToReset"]
    assert lines(account) == ["#7 5h 3.0% (1h 0m) · 7d 5.0%"]


def last_good(number: int, five: float, seven: float, age: float, **failure: str) -> dict:
    """An unavailable row carrying cswap's earlier good reading as lastGoodUsage."""
    good = aged(row(number, five, seven, will_last=seven < 90.0), age)
    unavailable = row(number, 0.0, 0.0, status="unavailable")
    kept = {"lastGoodUsage": good["usage"], "lastGoodFetchedAt": good["usageFetchedAt"], "lastGoodAgeSeconds": age}
    return unavailable | failure | kept


def test_an_old_figure_prints_with_the_time_it_was_read_and_judges_no_condition() -> None:
    """Asserts: a figure older than the 900 s bound prints marked with its read time and age, raises
    no threshold condition, and only the active account adds a line saying the switcher is deciding
    on figures that are not current."""
    out = lines(aged(row(3, 50.0, 99.0, active=True), 7_300.0))
    assert out == [
        "#3* 5h 50.0% (1h 0m) · 7d 99.0% (1d 0h), as of 8:45 AM JST (2h 1m old)",
        "#3: the active account, so the switcher is deciding on figures that are not current",
    ]


def test_an_unavailable_account_shows_its_last_good_reading_and_the_reason() -> None:
    """Asserts: an unavailable row with lastGoodUsage prints those values with their read time, then
    the reason: a 429 as rate-limited until its retry, another error as the last fetch with its
    retry when one is set."""
    retry = iso(NOW + 600)
    out = lines(
        last_good(3, 12.0, 99.0, 1_200.0, usageError="http-429", usageRetryAt=retry),
        last_good(4, 1.0, 2.0, 1_200.0, usageError="timeout", usageRetryAt=retry),
        last_good(5, 1.0, 2.0, 1_200.0, usageError="http-503"),
    )
    assert out == [
        "#3 5h 12.0% (1h 0m) · 7d 99.0% (1d 0h) RUNS OUT, as of 10:27 AM JST (20m old), rate-limited until 10:57 AM JST",
        "#4 5h 1.0% (1h 0m) · 7d 2.0% (1d 0h), as of 10:27 AM JST (20m old), last fetch timeout, retry 10:57 AM JST",
        "#5 5h 1.0% (1h 0m) · 7d 2.0% (1d 0h), as of 10:27 AM JST (20m old), last fetch http-503",
    ]


def test_unavailable_and_dead_login_accounts_print_their_status_and_what_to_do() -> None:
    """Asserts: an unavailable account with no earlier reading says its usage is unknown and why, and
    any other non-ok status says to log in as that account and re-add its slot."""
    out = lines(
        row(4, 0.0, 0.0, status="unavailable"),
        row(5, 0.0, 0.0, status="relogin_required"),
        row(6, 0.0, 0.0, status="unavailable") | {"usageError": "http-429"},
    )
    assert out == [
        "#4 unknown: no fetch error recorded",
        "#5 relogin_required",
        "#6 unknown: last fetch http-429",
        "#5: relogin_required, needs `/login` as that account, then `! cswap add --slot 5`",
    ]


def test_threshold_spend_and_login_conditions_each_print_one_line() -> None:
    """Asserts: a week at the threshold resetting within 48 h, extra-usage spend at 80 percent of its
    cap, and a login expiring within 3 days each print their condition line."""
    account = row(6, 10.0, 99.0, spend=(800.0, 1000.0)) | {"loginExpiresAt": iso(NOW + 86_400)}
    out = lines(account)
    assert out[1] == "#6: 7d at the switcher's 99% threshold, resets in 1d 0h; its remaining 1.0% expires unused unless `! cswap switch 6`"
    assert out[2] == "#6: extra usage $800.00 of $1000.00 this month, billed beyond the plan"
    # A day after 01:46:40 UTC Thu is 10:47 AM Fri in UTC+9
    assert out[3] == "#6: login expires Fri Sep 4, 10:47 AM JST, `/login` as that account before it lapses"


def test_the_pace_lines_sit_between_the_accounts_and_the_conditions() -> None:
    """Asserts: the short form prints each account, then the pace lines it is given, then each condition."""
    out = q.quota_lines({"accounts": [row(6, 10.0, 50.0, spend=(800.0, 1000.0))]}, THRESHOLD, "consume-first", CLOCK, ["7d pace X"])
    assert out[1] == "7d pace X"
    assert out[2].startswith("#6: extra usage")


# ---------------------------------------------------------------------------------------------
# Rotation order


def _rotation_fixture() -> list[dict]:
    soon, late = row(2, 0.0, 30.0), row(3, 40.0, 10.0)
    soon["usage"]["sevenDay"]["resetsAt"] = iso(NOW + 3_600)
    late["usage"]["sevenDay"]["resetsAt"] = iso(NOW + 7_200)
    held_week, held_five = row(4, 0.0, 99.0), row(5, 99.0, 30.0)
    held_week["usage"]["sevenDay"]["resetsAt"] = iso(NOW + 86_400)
    held_five["usage"]["fiveHour"]["resetsAt"] = iso(NOW + 600)
    return [row(6, 0.0, 0.0, status="unavailable"), held_week, late, held_five, row(1, 5.0, 50.0, active=True), soon]


@pytest.mark.parametrize("strategy", ["consume-first", "dynamic"])
def test_soonest_reset_strategies_order_usable_accounts_by_weekly_reset(strategy: str) -> None:
    """Asserts: under consume-first and dynamic the active account comes first, then each account
    under the threshold by soonest weekly reset, then each held at the threshold by when it can take
    work again, then an account with no current read."""
    assert [a["number"] for a in q.rotation_order(_rotation_fixture(), THRESHOLD, strategy)] == [1, 2, 3, 5, 4, 6]


def test_the_best_strategy_orders_usable_accounts_by_most_headroom() -> None:
    """Asserts: under best, accounts under the threshold come by most headroom (100 minus their
    higher window), whatever their weekly resets: #2 at 0/30 (70 left) comes before #3 at 40/10 (60
    left), and once #2's five-hour window reaches 50 (50 left) #3 comes first, while consume-first
    still puts #2 first for its sooner weekly reset."""
    accounts = _rotation_fixture()
    assert [a["number"] for a in q.rotation_order(accounts, THRESHOLD, "best")] == [1, 2, 3, 5, 4, 6]
    accounts[5]["usage"]["fiveHour"]["pct"] = 50.0  # #2 now has 50 left, #3 still 60
    assert [a["number"] for a in q.rotation_order(accounts, THRESHOLD, "best")] == [1, 3, 2, 5, 4, 6]
    assert [a["number"] for a in q.rotation_order(accounts, THRESHOLD, "consume-first")] == [1, 2, 3, 5, 4, 6]


def test_an_unknown_strategy_is_an_error_naming_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Asserts: a strategy this program does not know exits non-zero naming it, printing nothing on stdout."""
    _install_fake_cswap(tmp_path, monkeypatch, [row(1, 1.0, 1.0, active=True, now=time.time())], strategy="random")
    assert q.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "value='random'" in captured.err


# ---------------------------------------------------------------------------------------------
# Markdown

# A week on pace to run dry 6 h from CLOCK until the reset 24 h on, and a five-hour window that lasts.
RUNNING_OUT = {
    "sevenDay": Pace(4.0, 2.0, 48.0, ((6.0, 24.0),), 0.0),
    "fiveHour": Pace(1.0, 2.0, 5.0, (), 30.0),
}


def test_the_markdown_form_is_a_title_a_table_the_pace_then_the_conditions() -> None:
    """Asserts: the markdown opens with a title line carrying the local time and zone, then one row per
    account in the switcher's order named only now/next/unread (no number, no email), the active one
    bold, the weekly reset as a local weekday and time under a header naming the zone; then each
    window's verdict with its rate and first dry stretch as bullets; then each condition."""
    accounts = [
        row(4, 0.0, 0.0, status="unavailable"),
        row(2, 0.0, 98.0, will_last=False, five_reset=None, spend=(800.0, 1000.0)),
        row(1, 2.0, 24.0, active=True),
    ]
    text = q.quota_markdown({"accounts": accounts}, THRESHOLD, "consume-first", CLOCK, RUNNING_OUT)
    assert text == "\n".join(
        [
            "Claude usage · Thu 10:47 AM JST",
            "",
            "| Account | 5-hour | Week · resets JST |",
            "|---|---|---|",
            "| **now** | 2.0% · 1h 0m | 24.0% · Fri 10:47 AM |",
            "| next | 0.0% | 98.0% · Fri 10:47 AM |",
            "| unread | | unknown: no fetch error recorded |",
            "",
            "**Week: runs out**",
            "- Using 96.0%/day vs plan 48.0%/day (+100.0%)",
            "- Out from Thu 4:47 PM JST for 18h",
            "",
            "**5-hour: lasts**",
            "- Using 1.0%/hour vs plan 2.0%/hour (-50.0%)",
            "",
            "Counted over 2 of 3 accounts; 1 not read now.",
            "",
            "**Needs action**",
            "- #2: extra usage $800.00 of $1000.00 this month, billed beyond the plan",
        ]
    )
    assert "example.com" not in text


def test_the_table_labels_later_usable_accounts_then_and_held_ones_full() -> None:
    """Asserts: the second usable account reads 'then' and an account held at the threshold reads 'full'."""
    accounts = [row(1, 2.0, 24.0, active=True), row(2, 0.0, 10.0), row(3, 0.0, 20.0), row(4, 0.0, 99.0)]
    rows = q.table_rows(q.rotation_order(accounts, THRESHOLD, "consume-first"), THRESHOLD)
    assert [r.split(" | ")[0] for r in rows] == ["| **now**", "| next", "| then", "| full"]


def test_with_no_account_read_now_the_pace_says_so() -> None:
    """Asserts: with no current figure the markdown says there is no rate, and the short form says the same."""
    assert q.pace_sentences({}, [], CLOCK) == ["No account is read now, so there is no rate or projection."]
    assert q.pace_lines({}, [], CLOCK) == ["pace: no account read now, so no rate or projection"]


def test_the_pace_lines_name_the_rate_the_plan_the_share_and_when_it_runs_out() -> None:
    """Asserts: a window over its plan reads as its rate and the plan's, the share over, then the first
    dry stretch's local start and length; one under its plan reads as what renews unspent."""
    out = q.pace_lines(RUNNING_OUT, [row(1, 2.0, 24.0, active=True)], CLOCK)
    assert out == [
        "7d 96.0%/day vs plan 48.0%/day (+100.0%): out from Thu 4:47 PM JST for 18h",
        "5h 1.0%/hour vs plan 2.0%/hour (-50.0%): lasts, 30.0% renews unspent over the next 5h",
    ]


def test_only_the_first_dry_stretch_is_shown() -> None:
    """Asserts: a projection with two dry stretches names only the first in the markdown verdict."""
    pace = Pace(4.0, 2.0, 48.0, ((6.0, 24.0), (30.0, 40.0)), 0.0)
    text = q.pace_sentences({"sevenDay": pace, "fiveHour": RUNNING_OUT["fiveHour"]}, [], CLOCK)[0]
    assert text.count("Out from") == 1
    assert "for 18h" in text


def test_every_percentage_shows_one_decimal_place() -> None:
    """Asserts: a percentage prints to one decimal place, rounding 87.46 to 87.5 and hiding cswap's
    float residue (14.000000000000002) as 14.0."""
    assert q.pct(87.46) == "87.5%"
    assert q.pct(14.000000000000002) == "14.0%"
    assert q.pct(0.0) == "0.0%"


def test_a_reset_stamped_a_second_before_the_hour_reads_as_the_hour() -> None:
    """Asserts: a 23:59:59 UTC reset prints as the next whole hour in the local zone (9 AM in UTC+9)."""
    assert q.day_clock(datetime(2026, 10, 9, 23, 59, 59, tzinfo=timezone.utc)) == "Sat 9 AM JST"


def test_times_follow_the_machines_local_zone_and_its_daylight_saving() -> None:
    """Asserts: with the local zone set to central Europe, a September time prints in CEST, a December
    time in CET, and the table header names the zone in force now."""
    _set_zone(CENTRAL_EUROPE)
    assert q.day_clock(CLOCK) == "Thu 3:47 AM CEST"
    assert q.clock(datetime(2026, 12, 3, 1, 46, 40, tzinfo=timezone.utc)) == "2:47 AM CET"
    text = q.quota_markdown({"accounts": [row(1, 2.0, 24.0, active=True)]}, THRESHOLD, "consume-first", CLOCK, RUNNING_OUT)
    assert "| Account | 5-hour | Week · resets CEST |" in text.splitlines()


# ---------------------------------------------------------------------------------------------
# Rate and projection

RESET = 1_000 * HOUR
OPENED = RESET - 168 * HOUR


def test_spend_inside_one_window_is_the_rise_between_samples_however_far_apart() -> None:
    """Asserts: two samples of one window ten hours apart count their whole rise; a fall adds nothing;
    a reset stamp a second off is the same window."""
    samples = [Sample(OPENED + HOUR, RESET, 40.0), Sample(OPENED + 11 * HOUR, RESET - 1, 70.0), Sample(OPENED + 12 * HOUR, RESET, 69.0)]
    assert q.spent_since(samples, since=OPENED + HOUR, length_hours=168.0) == 30.0


def test_a_rise_that_straddles_the_span_start_counts_only_its_share_inside() -> None:
    """Asserts: a rise of 87 between readings 3 h apart, the span starting 1 h before the later one,
    counts a third of it."""
    samples = [Sample(OPENED + HOUR, RESET, 9.0), Sample(OPENED + 4 * HOUR, RESET, 96.0)]
    assert q.spent_since(samples, since=OPENED + 3 * HOUR, length_hours=168.0) == 29.0


def test_a_window_first_seen_counts_its_figure_spread_from_its_opening() -> None:
    """Asserts: an account's first sample of a window counts its whole figure when the window opened
    inside the span, the share after the span's start when it opened before; an unopened window
    counts nothing."""
    older = Sample(OPENED - 10 * HOUR, RESET - 168 * HOUR, 95.0)
    first = Sample(OPENED + 10 * HOUR, RESET, 20.0)
    assert q.spent_since([older, first], since=OPENED - HOUR, length_hours=168.0) == 20.0
    assert q.spent_since([first], since=OPENED + 5 * HOUR, length_hours=168.0) == 10.0
    assert q.spent_since([Sample(OPENED, None, 0.0)], since=OPENED - HOUR, length_hours=5.0) == 0.0


def test_a_rate_over_the_pool_runs_dry_and_refills_at_a_reset_inside_a_step() -> None:
    """Asserts: 10 points an hour over 20 and 0 left runs dry at hour 2 and stays dry until the empty
    account resets at hour 6.1, the stretch ending at that reset's own time rather than the step's
    end; the 99 it refills lasts to the horizon."""
    horizon, spells, unspent = q.project([Room(20.0, 10.0), Room(0.0, 6.1)], 99.0, 168.0, 10.0)
    assert horizon == 10.0
    assert spells == [(2.0, 6.1)]
    assert unspent == 0.0


def test_each_stretch_with_no_account_left_is_its_own_spell() -> None:
    """Asserts: 10 points an hour over 20 left with refills of 30 at hours 4 and 12 runs dry from 2 to
    4 and again from 7 to 12."""
    horizon, spells, _ = q.project([Room(20.0, 12.0), Room(0.0, 4.0)], 30.0, 168.0, 10.0)
    assert horizon == 12.0
    assert spells == [(2.0, 4.0), (7.0, 12.0)]


def test_a_slow_rate_reports_the_room_that_renews_unspent() -> None:
    """Asserts: 1 point an hour over 50 left resetting at hour 8 leaves 42 unspent and never runs dry."""
    horizon, spells, unspent = q.project([Room(50.0, 8.0)], 99.0, 168.0, 1.0)
    assert (horizon, spells, unspent) == (8.0, [], 42.0)


def test_the_fleet_rate_weighs_each_accounts_window_by_its_hours() -> None:
    """Asserts: A's window open 100 h and B's 20 h, A spending 50 before B opened and B 40 inside its
    window, gives 90 over 100 h and 40 over 20 h, a fleet rate of 130/120 per hour; an unopened
    window adds no span."""
    now = 1_000 * HOUR
    a_reset = now + 68 * HOUR
    b_reset = now + 148 * HOUR
    a = [Sample(now - 99 * HOUR, a_reset, 0.0), Sample(now - 30 * HOUR, a_reset, 50.0), Sample(now, a_reset, 50.0)]
    b = [Sample(now - 19 * HOUR, b_reset, 0.0), Sample(now, b_reset, 40.0)]
    assert q.fleet_rate([a, b, [Sample(now, None, 0.0)]], [a_reset, b_reset, None], now, 168.0) == 130.0 / 120


def test_the_pace_reads_the_history_and_the_current_read_as_one_series() -> None:
    """Asserts: a window opened 120 h ago with a history sample 24 h ago at 40 and the current read
    at 70 spent all 70 inside its window, a rate of 70/120 per hour, against a plan of 99 per 168 h."""
    resets_at = NOW + 48 * HOUR
    account = row(1, 0.0, 70.0, active=True, email="a@example.com")
    account["usage"]["sevenDay"]["resetsAt"] = iso(resets_at)
    history_rows = [{"at": NOW - 24 * HOUR, "account": "a@example.com", "sevenDay": {"resetsAt": resets_at, "pct": 40.0}, "fiveHour": {"resetsAt": None, "pct": 0.0}}]
    pace = q.window_pace([account], q.history_samples(history_rows, "sevenDay"), "sevenDay", 99.0, CLOCK)
    assert pace.rate_per_hour == 70.0 / 120
    assert pace.capacity_per_hour == 99.0 / 168
    assert pace.first_dry_in_hours is None


def test_with_no_history_the_rate_comes_from_the_current_read_alone() -> None:
    """Asserts: on a first run, an account at 70 in a window opened 120 h ago reads a rate of 70/120
    per hour, the same as with history, so the first run needs no special case."""
    account = row(1, 0.0, 70.0, active=True)
    account["usage"]["sevenDay"]["resetsAt"] = iso(NOW + 48 * HOUR)
    paces = q.fleet_paces([account], 99.0, CLOCK, [])
    assert paces["sevenDay"].rate_per_hour == 70.0 / 120


# ---------------------------------------------------------------------------------------------
# History


def test_the_history_path_prefers_its_variable_then_xdg_state_then_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Asserts: CSWAP_QUOTA_HISTORY wins; without it XDG_STATE_HOME/cswap-quota/history.jsonl; without
    both ~/.local/state/cswap-quota/history.jsonl."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("CSWAP_QUOTA_HISTORY", str(tmp_path / "h.jsonl"))
    assert q.history_path() == tmp_path / "h.jsonl"
    monkeypatch.delenv("CSWAP_QUOTA_HISTORY")
    assert q.history_path() == tmp_path / "state" / "cswap-quota" / "history.jsonl"
    monkeypatch.delenv("XDG_STATE_HOME")
    assert q.history_path() == tmp_path / "home" / ".local" / "state" / "cswap-quota" / "history.jsonl"


def test_a_write_appends_current_figures_only_and_prunes_old_lines(tmp_path: Path) -> None:
    """Asserts: a write keeps lines from the last 8 days, drops older ones, adds one compact line per
    account whose figure is current (an old figure and an unavailable account add none), keyed by
    the account email with each window's reset and percent, creating the directory."""
    path = tmp_path / "deep" / "history.jsonl"
    old = {"at": NOW - 9 * 86_400, "account": "x@example.com", "sevenDay": {"resetsAt": None, "pct": 1.0}, "fiveHour": {"resetsAt": None, "pct": 1.0}}
    recent = old | {"at": NOW - 7 * 86_400}
    accounts = [row(1, 2.0, 24.0, active=True), aged(row(2, 5.0, 5.0), 7_300.0), row(3, 0.0, 0.0, status="unavailable"), row(4, 0.0, 9.0, five_reset=None)]
    q.write_history(path, [old, recent], accounts, CLOCK)
    written = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert written[0] == recent
    assert [r["account"] for r in written[1:]] == ["user1@example.com", "user4@example.com"]
    assert written[1] == {
        "at": float(NOW),
        "account": "user1@example.com",
        "sevenDay": {"resetsAt": float(NOW + 86_400), "pct": 24.0},
        "fiveHour": {"resetsAt": float(NOW + 3600), "pct": 2.0},
    }
    assert written[2]["fiveHour"] == {"resetsAt": None, "pct": 0.0}
    assert ": " not in path.read_text(encoding="utf-8")


def test_a_corrupt_history_line_exits_non_zero_naming_the_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Asserts: a history line that is not JSON stops the run with the file and line number, printing nothing on stdout."""
    history = _install_fake_cswap(tmp_path, monkeypatch, [row(1, 1.0, 1.0, active=True, now=time.time())])
    history.write_text("not json\n", encoding="utf-8")
    assert q.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "line 1 is not JSON" in captured.err


# ---------------------------------------------------------------------------------------------
# The whole program against a fake cswap


def _install_fake_cswap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, accounts: list[dict], strategy: str = "consume-first", body: str | None = None) -> Path:
    """Put a `cswap` script first on PATH answering list, threshold and strategy from files, and
    point the history at tmp_path; returns the history path."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (tmp_path / "list.json").write_text(json.dumps({"schemaVersion": 1, "accounts": accounts}), encoding="utf-8")
    (tmp_path / "threshold.json").write_text(json.dumps({"key": "autoswitch.threshold", "value": THRESHOLD}), encoding="utf-8")
    (tmp_path / "strategy.json").write_text(json.dumps({"key": "autoswitch.strategy", "value": strategy}), encoding="utf-8")
    if body is None:
        body = (
            'case "$*" in\n'
            f'  "list --json") cat "{tmp_path}/list.json" ;;\n'
            f'  "config get autoswitch.threshold --json") cat "{tmp_path}/threshold.json" ;;\n'
            f'  "config get autoswitch.strategy --json") cat "{tmp_path}/strategy.json" ;;\n'
            '  *) echo "unexpected: $*" >&2; exit 2 ;;\n'
            "esac"
        )
    script = bin_dir / "cswap"
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    history = tmp_path / "history.jsonl"
    monkeypatch.setenv("CSWAP_QUOTA_HISTORY", str(history))
    return history


def test_a_first_run_prints_a_rate_and_starts_the_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Asserts: with no history file the program prints the markdown with a weekly rate worked out from
    the current read alone, then writes one history line per current account; a second run reads it."""
    now = time.time()
    history = _install_fake_cswap(tmp_path, monkeypatch, [row(1, 10.0, 30.0, active=True, now=now), row(2, 0.0, 5.0, now=now)])
    assert not history.exists()
    assert q.main([]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Claude usage · ")
    assert "**Week: lasts**" in out
    assert "- Using " in out
    assert len(history.read_text(encoding="utf-8").splitlines()) == 2
    assert q.main(["--lines"]) == 0
    assert capsys.readouterr().out.startswith("#1* 5h 10.0%")
    assert len(history.read_text(encoding="utf-8").splitlines()) == 4


def test_a_failing_cswap_exits_non_zero_with_its_error_and_prints_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Asserts: cswap exiting non-zero makes the program exit 1 with cswap's own error on stderr, no
    stdout, and no history written."""
    history = _install_fake_cswap(tmp_path, monkeypatch, [], body='echo "token store locked" >&2; exit 3')
    assert q.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "cswap-quota: `cswap list --json` exited 3: token store locked\n"
    assert not history.exists()


def test_a_missing_cswap_exits_non_zero_saying_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Asserts: with no cswap on PATH the program exits 1 saying cswap is not on PATH."""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("CSWAP_QUOTA_HISTORY", str(tmp_path / "history.jsonl"))
    assert q.main([]) == 1
    assert "cswap is not on PATH" in capsys.readouterr().err


def test_an_unusable_threshold_exits_non_zero_naming_the_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Asserts: a threshold outside (0, 100] is an error naming the value; no default stands in."""
    _install_fake_cswap(tmp_path, monkeypatch, [])
    (tmp_path / "threshold.json").write_text(json.dumps({"value": None}), encoding="utf-8")
    assert q.main([]) == 1
    assert "value=None" in capsys.readouterr().err
