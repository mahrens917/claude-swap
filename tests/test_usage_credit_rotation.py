"""Usage-credit rotation: when every account's 5h/7d windows are full, the
auto engine keeps work going on the account that still holds paid usage
credits (Anthropic's extra usage) instead of waiting for the earliest reset,
and `cswap list` shows the money left per account."""

import logging

import pytest

from claude_swap.autoswitch import (
    AllExhaustedEvent,
    PollEvent,
    SpendingUsageCreditsEvent,
    SwitchEvent,
)
from claude_swap.settings import AutoSwitchSettings
from claude_swap.switcher import _format_usage_lines, spend_row_body
from claude_swap.usage_store import UsageEntry
from tests.test_autoswitch import EngineHarness, TickOutcome

_CREDITS_LINE = "sessions run on Account-"
_DETECTOR_LINE = "holds usage-credit room"


def _spend(remaining: float | None, *, used: float = 8.11, reached: bool = False) -> dict:
    limit = None if remaining is None else used + remaining
    return {
        "used": used,
        "limit": limit,
        "remaining": remaining,
        "pct": None if limit is None else round(100 * used / limit, 2),
        "currency": "USD",
        "limit_reached": reached,
        "reported": "dollars",
    }


def _full(spend: dict | None = None) -> dict:
    usage: dict = {"five_hour": {"pct": 100.0}, "seven_day": {"pct": 40.0}}
    if spend is not None:
        usage["spend"] = spend
    return usage


def _open(pct: float = 10.0) -> dict:
    return {"five_hour": {"pct": pct}, "seven_day": {"pct": 10.0}}


def _harness(temp_home) -> EngineHarness:
    h = EngineHarness(temp_home)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.seed(3, "c@example.com")
    h.make_live("a@example.com", 1)
    return h


def _warnings(caplog, needle: str) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and needle in r.getMessage()
    ]


class TestRotationOntoUsageCredits:
    def test_all_windows_full_moves_to_the_account_with_credit_room(
        self, temp_home, caplog
    ):
        """Asserts: with every account's windows full and only #2 holding
        usage credits, the engine switches to #2 under the `usage-credits`
        trigger, emits the spending event, and logs it at WARNING."""
        h = _harness(temp_home)
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            outcome = h.tick_with_usage({
                "1": _full(), "2": _full(_spend(591.89)), "3": _full(),
            })
        assert outcome is TickOutcome.SWITCHED, h.kinds()
        assert h.active_number() == 2
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "usage-credits"
        assert switch.detail == "$591.89 left"
        credits = next(e for e in h.events if isinstance(e, SpendingUsageCreditsEvent))
        assert credits.account["number"] == 2
        assert credits.room.remaining == 591.89
        assert credits.to_json()["reported"] == "dollars"
        assert credits.switched is True
        assert credits.to_json()["event"] == "spending-usage-credits"
        assert not any(isinstance(e, AllExhaustedEvent) for e in h.events)
        lines = _warnings(caplog, _CREDITS_LINE)
        assert lines == [
            "Every account's usage windows are full; sessions run on "
            "Account-2 usage credits ($591.89 left), switching to it"
        ], lines

    def test_an_uncapped_account_outranks_any_finite_amount(self, temp_home):
        """Asserts: when the active has no credits, the peer with no monthly
        cap takes the sessions over a peer with a finite remainder."""
        h = _harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _full(), "2": _full(_spend(10_000.0)), "3": _full(_spend(None)),
        })
        assert outcome is TickOutcome.SWITCHED, h.kinds()
        assert h.active_number() == 3

    def test_a_reached_cap_is_not_credit_room(self, temp_home):
        """Asserts: a peer whose cap the API reports reached is never a
        usage-credit target, and the fleet waits as before."""
        h = _harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _full(), "2": _full(_spend(5.0, reached=True)), "3": _full(),
        })
        assert outcome is TickOutcome.BLOCKED
        assert any(isinstance(e, AllExhaustedEvent) for e in h.events)


class TestStayingOnTheActivesCredits:
    def test_the_active_with_credit_room_keeps_the_sessions(self, temp_home, caplog):
        """Asserts: with every window full and the active holding credits,
        the engine stays put (NO_ACTION), arms no reset sleep, and keeps the
        ordinary cadence, even though a peer has more money left."""
        h = _harness(temp_home)
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            outcome = h.tick_with_usage({
                "1": _full(_spend(20.0)), "2": _full(_spend(500.0)), "3": _full(),
            })
        assert outcome is TickOutcome.NO_ACTION, h.kinds()
        assert h.active_number() == 1
        assert not any(isinstance(e, SwitchEvent) for e in h.events)
        assert not any(isinstance(e, AllExhaustedEvent) for e in h.events)
        assert h.engine._blocked_wait_long is False
        assert h.engine._sleep_until_ts is None
        assert h.engine._next_delay(outcome) <= h.settings.interval_seconds * 1.1
        credits = next(e for e in h.events if isinstance(e, SpendingUsageCreditsEvent))
        assert credits.switched is False
        assert credits.account["number"] == 1
        assert _warnings(caplog, _CREDITS_LINE) == [
            "Every account's usage windows are full; sessions run on "
            "Account-1 usage credits ($20.00 left)"
        ]

    def test_the_warning_fires_once_per_credit_account_not_every_tick(
        self, temp_home, caplog
    ):
        """Asserts: two consecutive ticks on the same account's credits emit
        the event both times but the WARNING only once."""
        h = _harness(temp_home)
        fleet = {"1": _full(_spend(20.0)), "2": _full(), "3": _full()}
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            h.tick_with_usage(fleet)
            h.tick_with_usage(fleet)
        events = [e for e in h.events if isinstance(e, SpendingUsageCreditsEvent)]
        assert len(events) == 2
        assert len(_warnings(caplog, _CREDITS_LINE)) == 1


class TestNoCreditsAnywhere:
    def test_all_full_and_no_credits_keeps_the_all_exhausted_wait(
        self, temp_home, caplog
    ):
        """Asserts: with no account holding credit room the all-exhausted
        wait is unchanged: BLOCKED, the event, the long wait armed, and
        neither the credits event nor the detector line."""
        h = _harness(temp_home)
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            outcome = h.tick_with_usage({"1": _full(), "2": _full(), "3": _full()})
        assert outcome is TickOutcome.BLOCKED
        assert any(isinstance(e, AllExhaustedEvent) for e in h.events)
        assert h.engine._blocked_wait_long is True
        assert not any(isinstance(e, SpendingUsageCreditsEvent) for e in h.events)
        assert _warnings(caplog, _DETECTOR_LINE) == []
        assert _warnings(caplog, _CREDITS_LINE) == []


class TestMovingBackOffCredits:
    def test_a_reopened_window_takes_the_sessions_off_credits(self, temp_home):
        """Asserts: while the active (#2) runs on credits with its windows
        full, a candidate whose window reopens is taken by the ordinary
        at-limit rotation on the next tick."""
        h = _harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _full(), "2": _full(_spend(591.89)), "3": _full(),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        h.make_live("b@example.com", 2)
        h.events.clear()
        h.clock.advance(60)
        outcome = h.tick_with_usage({
            "1": _full(), "2": _full(_spend(590.0)), "3": _open(),
        })
        assert outcome is TickOutcome.SWITCHED, h.kinds()
        assert h.active_number() == 3
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "at-limit"
        assert not any(isinstance(e, SpendingUsageCreditsEvent) for e in h.events)


class TestUnusedCreditsDetector:
    def test_exhausted_wait_with_a_quarantined_credit_account_warns(
        self, temp_home, caplog
    ):
        """Asserts: entering the all-exhausted wait while an account outside
        the rotation (quarantined #3) holds credit room logs a WARNING that
        names it."""
        h = _harness(temp_home)
        h.engine._quarantine("3", "c@example.com", "invalid_grant")
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            outcome = h.tick_with_usage({
                "1": _full(), "2": _full(), "3": _full(_spend(42.0)),
            })
        assert outcome is TickOutcome.BLOCKED
        assert any(isinstance(e, AllExhaustedEvent) for e in h.events)
        assert _warnings(caplog, _DETECTOR_LINE) == [
            "All accounts exhausted while Account-3 holds usage-credit room "
            "($42.00 left) the rotation did not use"
        ]

    def test_a_disabled_credit_account_is_the_users_choice_not_a_fault(
        self, temp_home, caplog
    ):
        """Asserts: an account the user disabled is held out of rotation on
        purpose, so its credits raise no detector line."""
        h = _harness(temp_home)
        h.switcher.set_account_disabled("3", True)
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            outcome = h.tick_with_usage({
                "1": _full(), "2": _full(), "3": _full(_spend(42.0)),
            })
        assert outcome is TickOutcome.BLOCKED
        assert _warnings(caplog, _DETECTOR_LINE) == []


class TestMoneyLeftRow:
    def test_capped_row_shows_money_left(self):
        """Asserts: a capped account's `$$` row reads the dollars left of
        the cap, percent first."""
        spend = {"used": 8.11, "limit": 600.0, "remaining": 591.89, "pct": 1.35,
                 "currency": "USD", "limit_reached": False, "reported": "dollars"}
        assert spend_row_body(spend) == "  1%   $591.89 left of $600.00"
        assert _format_usage_lines({"spend": spend}) == [
            "$$:   1%   $591.89 left of $600.00"
        ]

    def test_uncapped_row_shows_used_and_no_cap(self):
        """Asserts: an uncapped account shows what it used and that no cap
        applies, with no percent."""
        assert spend_row_body(_spend(None)) == "$8.11 used, no cap"

    def test_reached_row_names_the_cap(self):
        """Asserts: a reached cap reads as such, naming the cap."""
        spend = {"used": 600.0, "limit": 600.0, "remaining": 0.0, "pct": 100.0,
                 "currency": "USD", "limit_reached": True, "reported": "dollars"}
        assert spend_row_body(spend) == "100%   cap reached ($600.00)"

    def test_poll_line_prints_an_uncapped_credit_account(self):
        """Asserts: the engine's poll line prints a spend-only uncapped
        account's figure rather than failing on its null cap."""
        event = PollEvent(
            active={"number": 6, "email": "a@example.com"},
            headroom={"6": 28.0, "8": None},
            threshold=90.0,
            spend={"8": _spend(None)},
        )
        assert "#8: $$ $8.11 used, no cap" in event.human()


class TestConfiguredCapsRankInDollars:
    """X3696: with every setup-token account's monthly cap configured, the
    credit rotation ranks the accounts by dollars left, not by share."""

    def _entries(self, tmp_path, caps: dict[str, str]) -> dict[str, UsageEntry]:
        from claude_swap import usage_store
        from claude_swap.settings import credit_cap_key, set_setting

        idents = {"1": ("a@example.com", ""), "2": ("b@example.com", ""),
                  "3": ("c@example.com", "")}
        store = usage_store.UsageStore(tmp_path / "cache")
        for num, share in (("2", "0.5"), ("3", "0.5")):
            store.record_header_reading(num, idents, {
                usage_store.USAGE_HEADER_5H_PCT: "1.0",
                usage_store.USAGE_HEADER_7D_PCT: "0.4",
                usage_store.USAGE_HEADER_OVERAGE_STATUS: "allowed",
                usage_store.USAGE_HEADER_OVERAGE_PCT: share,
            }, header_only=True)
        for email, dollars in caps.items():
            set_setting(tmp_path, credit_cap_key(email), dollars)
        return store.entries(idents)

    def test_the_larger_dollar_remainder_wins(self, tmp_path):
        """Asserts: two accounts each half through their credits rank by
        dollars: $250 left of a $500 cap beats $100 left of a $200 cap,
        and the switch detail reads the dollars."""
        from claude_swap.autoswitch import _credit_money, _usage_credit_pick

        entries = self._entries(
            tmp_path, {"b@example.com": "200", "c@example.com": "500"}
        )
        pick = _usage_credit_pick("1", ["2", "3"], entries)
        assert pick is not None
        number, room = pick
        assert number == "3"
        assert room.reported == "dollars"
        assert room.remaining == pytest.approx(250.0)
        assert _credit_money(room) == "$250.00 left"

    def test_without_caps_the_shares_tie(self, tmp_path):
        """Asserts: the same two readings with no cap configured stay
        fractions and rank equal, so the larger cap is invisible: the
        first candidate is taken."""
        from claude_swap.autoswitch import _usage_credit_pick

        entries = self._entries(tmp_path, {})
        number, room = _usage_credit_pick("1", ["2", "3"], entries)
        assert room.reported == "fraction"
        assert number == "2"

    def test_the_configured_cap_row_names_the_cap_as_yours(self):
        """Asserts: dollars computed from the configured cap read ``left of
        your $500 cap``; a reached one ``cap reached (your $500 cap)``."""
        spend = {"used": 0.0, "limit": 500.0, "remaining": 500.0, "pct": 0.0,
                 "currency": "USD", "limit_reached": False, "reported": "dollars",
                 "cap_source": "config", "disabled_reason": None}
        assert spend_row_body(spend) == "  0%   $500.00 left of your $500 cap"
        reached = {**spend, "used": 500.0, "remaining": 0.0, "pct": 100.0,
                   "limit_reached": True, "disabled_reason": "out_of_credits"}
        assert spend_row_body(reached) == "100%   cap reached (your $500 cap)"
        refused = {**spend, "limit_reached": True,
                   "disabled_reason": "org_level_disabled"}
        assert spend_row_body(refused) == (
            "  0%   credits refused (org_level_disabled), your $500 cap"
        )


_LOST_ROOM_LINE = "lost its usage-credit room"


def _near_full(pct: float = 99.5, spend: dict | None = None) -> dict:
    usage: dict = {"five_hour": {"pct": pct}, "seven_day": {"pct": 40.0}}
    if spend is not None:
        usage["spend"] = spend
    return usage


def _credit_harness(temp_home, **settings) -> EngineHarness:
    h = EngineHarness(temp_home, strategy="best", threshold=99.0, **settings)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.seed(3, "c@example.com")
    h.make_live("a@example.com", 1)
    return h


class TestCreditSwitchPoint:
    """X3587: an account with usage-credit room switches at creditThreshold,
    every other account at threshold."""

    def test_an_account_with_credit_room_holds_at_99_5(self, temp_home):
        """Asserts: threshold 99, creditThreshold 100, the active at 99.5
        with money left: no switch, and the below-threshold line names its
        own switch point of 100."""
        h = _credit_harness(temp_home, credit_threshold=100.0)
        outcome = h.tick_with_usage({
            "1": _near_full(spend=_spend(50.0)), "2": _open(), "3": _open(),
        })
        assert outcome is TickOutcome.NO_ACTION, h.kinds()
        assert h.active_number() == 1
        no_switch = next(e for e in h.events if e.kind == "no-switch")
        assert no_switch.reason == "below-threshold"
        assert no_switch.detail == "99.5% < 100%"
        poll = next(e for e in h.events if isinstance(e, PollEvent))
        assert poll.switch_bar == 100.0
        assert poll.switch_bars == {"1": 100.0, "2": 99.0, "3": 99.0}
        assert poll.to_json()["switchBarsPct"] == poll.switch_bars
        assert "(switch at 100%)" in poll.human()

    def test_an_account_without_credits_switches_at_99_5(self, temp_home):
        """Asserts: the same 99.5 on an account with no usage credits is past
        its plain threshold of 99, so the engine switches proactively."""
        h = _credit_harness(temp_home, credit_threshold=100.0)
        outcome = h.tick_with_usage({
            "1": _near_full(), "2": _open(), "3": _open(),
        })
        assert outcome is TickOutcome.SWITCHED, h.kinds()
        assert h.active_number() != 1
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "proactive"

    def test_a_reached_cap_switches_at_the_plain_threshold(self, temp_home):
        """Asserts: credits on but the monthly cap reached is no credit room,
        so 99.5 switches at threshold 99."""
        h = _credit_harness(temp_home, credit_threshold=100.0)
        outcome = h.tick_with_usage({
            "1": _near_full(spend=_spend(5.0, reached=True)),
            "2": _open(), "3": _open(),
        })
        assert outcome is TickOutcome.SWITCHED, h.kinds()

    def test_unset_credit_threshold_switches_a_credit_account_as_before(
        self, temp_home
    ):
        """Asserts: with creditThreshold unset, an active holding credits at
        99.5 switches at threshold 99, and the poll line reads 99 for every
        account."""
        h = _credit_harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _near_full(spend=_spend(50.0)), "2": _open(), "3": _open(),
        })
        assert outcome is TickOutcome.SWITCHED, h.kinds()
        poll = next(e for e in h.events if isinstance(e, PollEvent))
        assert poll.switch_bar == 99.0
        assert set(poll.switch_bars.values()) == {99.0}

    def test_every_account_above_reads_each_accounts_own_point(self, temp_home):
        """Asserts: the every-account-above-threshold state reads per-account
        switch points: an active at 99.5 without credits and a peer at 99.5
        WITH credits is not that state, because the peer sits below its own
        point of 100, so the peer is a healthy landing (the plain rule would
        have ranked on recovery instead)."""
        h = _credit_harness(temp_home, credit_threshold=100.0, hysteresis_pct=0.0)
        engine = h.engine
        entries = {
            "1": UsageEntry(last_good=_near_full(), fetched_at=0.0, age_s=0.0),
            "2": UsageEntry(
                last_good=_near_full(99.0, _spend(50.0)), fetched_at=0.0, age_s=0.0
            ),
        }
        usage = {num: e.last_good for num, e in entries.items()}
        headroom = {"1": 0.5, "2": 1.0}
        ordered, _known, _reset, waiting = engine._rank_candidates(
            trigger="proactive",
            consume_first=False,
            oauth_candidates=["2"],
            no_return=None,
            usage=usage,
            headroom=headroom,
            current="1",
            active_headroom=0.5,
            settings=h.settings,
            now=h.clock.now,
            entries=entries,
        )
        assert ordered == ["2"]
        assert waiting is False
        plain, _, _, _ = engine._rank_candidates(
            trigger="proactive",
            consume_first=False,
            oauth_candidates=["2"],
            no_return=None,
            usage=usage,
            headroom=headroom,
            current="1",
            active_headroom=0.5,
            settings=AutoSwitchSettings(
                strategy="best", threshold=99.0, hysteresis_pct=0.0
            ),
            now=h.clock.now,
            entries=entries,
        )
        assert plain == []


class TestLostCreditRoomAlarm:
    """X3587 alarm: an active held past threshold on its credits that loses
    the room while still over threshold logs a WARNING."""

    def test_room_gone_under_a_held_account_warns(self, temp_home, caplog):
        """Asserts: tick 1 holds the active at 99.5 on its credits; tick 2
        reads the cap reached at 99.6, logs one WARNING naming the account,
        its usage and the plain threshold, and switches."""
        h = _credit_harness(temp_home, credit_threshold=100.0)
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            first = h.tick_with_usage({
                "1": _near_full(spend=_spend(1.0)), "2": _open(), "3": _open(),
            })
            assert first is TickOutcome.NO_ACTION, h.kinds()
            assert _warnings(caplog, _LOST_ROOM_LINE) == []
            h.clock.advance(60)
            second = h.tick_with_usage({
                "1": _near_full(99.6, _spend(0.0, reached=True)),
                "2": _open(), "3": _open(),
            })
        assert second is TickOutcome.SWITCHED, h.kinds()
        assert _warnings(caplog, _LOST_ROOM_LINE) == [
            "Account-1 lost its usage-credit room at 99.6% used; it now "
            "switches at the plain threshold 99%"
        ]

    def test_a_healthy_credit_pool_stays_quiet(self, temp_home, caplog):
        """Asserts: two held ticks with money left, and a credit account that
        loses its room while BELOW threshold, raise no alarm."""
        h = _credit_harness(temp_home, credit_threshold=100.0)
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            for _ in range(2):
                h.tick_with_usage({
                    "1": _near_full(spend=_spend(30.0)), "2": _open(), "3": _open(),
                })
                h.clock.advance(60)
            h.tick_with_usage({
                "1": _near_full(50.0, _spend(0.0, reached=True)),
                "2": _open(), "3": _open(),
            })
        assert _warnings(caplog, _LOST_ROOM_LINE) == []

    def test_no_alarm_without_a_credit_threshold(self, temp_home, caplog):
        """Asserts: with creditThreshold unset nothing is ever held on
        credits, so a cap reached over threshold raises no alarm."""
        h = _credit_harness(temp_home)
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            h.tick_with_usage({
                "1": _near_full(98.0, _spend(1.0)), "2": _open(), "3": _open(),
            })
            h.clock.advance(60)
            h.tick_with_usage({
                "1": _near_full(99.5, _spend(0.0, reached=True)),
                "2": _open(), "3": _open(),
            })
        assert _warnings(caplog, _LOST_ROOM_LINE) == []


def _fable(pct: float, spend: dict | None = None) -> dict:
    """An account whose 5h/7d are open and whose Fable weekly window reads
    ``pct``."""
    usage: dict = {
        "five_hour": {"pct": 10.0},
        "seven_day": {"pct": 10.0},
        "scoped": [{"name": "Fable", "pct": pct}],
    }
    if spend is not None:
        usage["spend"] = spend
    return usage


def _fable_harness(temp_home, **settings) -> EngineHarness:
    h = EngineHarness(temp_home, model="Fable", **settings)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.seed(3, "c@example.com")
    h.make_live("a@example.com", 1)
    return h


class TestCreditPointOnTheModelWindow:
    """X3647: the credit switch point covers the per-model weekly window
    (Fable) exactly as it covers the 5h/7d windows."""

    def test_a_credit_account_at_fable_99_5_stays(self, temp_home):
        """Asserts: threshold 99, creditThreshold 100, the active's Fable
        window at 99.5 (5h/7d at 10) with money left: no switch, and the
        below-threshold line names its own point of 100."""
        h = _fable_harness(
            temp_home, strategy="best", threshold=99.0, credit_threshold=100.0
        )
        outcome = h.tick_with_usage({
            "1": _fable(99.5, _spend(50.0)), "2": _fable(10.0), "3": _fable(10.0),
        })
        assert outcome is TickOutcome.NO_ACTION, h.kinds()
        assert h.active_number() == 1
        no_switch = next(e for e in h.events if e.kind == "no-switch")
        assert no_switch.reason == "below-threshold"
        assert no_switch.detail == "99.5% < 100%"

    def test_an_account_without_credits_at_fable_99_5_leaves(self, temp_home):
        """Asserts: the same Fable 99.5 with no usage credits is past the
        plain threshold of 99, so the engine switches proactively."""
        h = _fable_harness(
            temp_home, strategy="best", threshold=99.0, credit_threshold=100.0
        )
        outcome = h.tick_with_usage({
            "1": _fable(99.5), "2": _fable(10.0), "3": _fable(10.0),
        })
        assert outcome is TickOutcome.SWITCHED, h.kinds()
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "proactive"

    def test_unset_credit_threshold_leaves_a_credit_account_at_fable_99_5(
        self, temp_home
    ):
        """Asserts: with creditThreshold unset, an active holding credits at
        Fable 99.5 switches at threshold 99 as before."""
        h = _fable_harness(temp_home, strategy="best", threshold=99.0)
        outcome = h.tick_with_usage({
            "1": _fable(99.5, _spend(50.0)), "2": _fable(10.0), "3": _fable(10.0),
        })
        assert outcome is TickOutcome.SWITCHED, h.kinds()


class TestFleetFableWallWithCredits:
    """X3647: every account's Fable window full and some account with credit
    room is the usage-credit move, never the fleet-wide model wall."""

    def test_dynamic_moves_onto_the_credit_account(self, temp_home, caplog):
        """Asserts: under `dynamic`, every Fable window at 100 and only #2
        holding credits, the engine switches to #2 under `usage-credits`
        and emits the spending event, instead of reading a fleet-wide model
        wall and ranking on 5h/7d alone."""
        h = _fable_harness(
            temp_home, strategy="dynamic", threshold=90.0, credit_threshold=100.0
        )
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            outcome = h.tick_with_usage({
                "1": _fable(100.0), "2": _fable(100.0, _spend(80.0)),
                "3": _fable(100.0),
            })
        assert outcome is TickOutcome.SWITCHED, h.kinds()
        assert h.active_number() == 2
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "usage-credits"
        assert any(isinstance(e, SpendingUsageCreditsEvent) for e in h.events)
        assert len(_warnings(caplog, _CREDITS_LINE)) == 1

    def test_dynamic_active_with_credits_keeps_the_sessions(self, temp_home):
        """Asserts: the active itself holding credits with every Fable
        window full stays put on its credits (NO_ACTION, no reset sleep)."""
        h = _fable_harness(
            temp_home, strategy="dynamic", threshold=90.0, credit_threshold=100.0
        )
        outcome = h.tick_with_usage({
            "1": _fable(100.0, _spend(80.0)), "2": _fable(100.0), "3": _fable(100.0),
        })
        assert outcome is TickOutcome.NO_ACTION, h.kinds()
        assert h.active_number() == 1
        credits = next(e for e in h.events if isinstance(e, SpendingUsageCreditsEvent))
        assert credits.switched is False
        assert h.engine._blocked_wait_long is False

    def test_unset_credit_threshold_keeps_the_model_wall_verdict(self, temp_home):
        """Asserts: with creditThreshold unset the same fleet takes no
        usage-credit move: the fleet-wide model wall stands as before."""
        h = _fable_harness(temp_home, strategy="dynamic", threshold=90.0)
        h.tick_with_usage({
            "1": _fable(100.0), "2": _fable(100.0, _spend(80.0)), "3": _fable(100.0),
        })
        assert not any(isinstance(e, SpendingUsageCreditsEvent) for e in h.events)
        assert not any(
            isinstance(e, SwitchEvent) and e.trigger == "usage-credits"
            for e in h.events
        )

    def test_a_quarantined_credit_account_does_not_cancel_the_fleet_wall(
        self, temp_home, caplog
    ):
        """Asserts: every Fable window full and the only credit account (#3)
        quarantined, so outside the rotation: the fleet-wide Fable wall
        stands (#3's credit point no longer ends it), so the engine reads
        the active on its open 5h/7d windows and stays, with no
        all-exhausted wait and no unused-credit detector line."""
        h = _fable_harness(
            temp_home, strategy="dynamic", threshold=90.0, credit_threshold=100.0
        )
        h.engine._quarantine("3", "c@example.com", "invalid_grant")
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            outcome = h.tick_with_usage({
                "1": _fable(100.0), "2": _fable(100.0),
                "3": _fable(100.0, _spend(42.0)),
            })
        assert outcome is TickOutcome.NO_ACTION, h.kinds()
        assert not any(isinstance(e, AllExhaustedEvent) for e in h.events)
        assert _warnings(caplog, _DETECTOR_LINE) == []


class TestModelWallPredicateReadsCreditPoints:
    """X3647: `_model_window_binds_everywhere` reads each account at its own
    switch point."""

    def _entries(self, usage: dict) -> dict[str, UsageEntry]:
        return {
            num: UsageEntry(last_good=value, fetched_at=0.0, age_s=0.0)
            for num, value in usage.items()
        }

    def test_a_credit_account_ends_the_fleet_wall(self):
        """Asserts: two accounts model-walled at Fable 100 and one holding
        credits at Fable 100: no fleet-wide wall when creditThreshold is
        set; the plain rule (unset) still reads one."""
        from claude_swap.autoswitch import _model_window_binds_everywhere

        usage = {"1": _fable(100.0), "2": _fable(100.0, _spend(5.0)), "3": _fable(100.0)}
        entries = self._entries(usage)
        held = AutoSwitchSettings(threshold=90.0, credit_threshold=100.0)
        plain = AutoSwitchSettings(threshold=90.0)
        rotation = ("1", "2", "3")
        assert _model_window_binds_everywhere(
            usage, ("Fable",), held, entries, rotation
        ) is False
        assert _model_window_binds_everywhere(
            usage, ("Fable",), plain, entries, rotation
        ) is True

    def test_only_the_rotation_counts(self):
        """Asserts: the credit account (#2) outside the rotation (quarantined
        or disabled) does not end the wall the rotation (#1, #3) is under,
        and an open account outside it does not either."""
        from claude_swap.autoswitch import _model_window_binds_everywhere

        held = AutoSwitchSettings(threshold=90.0, credit_threshold=100.0)
        usage = {"1": _fable(100.0), "2": _fable(100.0, _spend(5.0)), "3": _fable(100.0)}
        entries = self._entries(usage)
        assert _model_window_binds_everywhere(
            usage, ("Fable",), held, entries, ("1", "3")
        ) is True
        open_outside = {"1": _fable(100.0), "2": _fable(10.0), "3": _fable(100.0)}
        assert _model_window_binds_everywhere(
            open_outside, ("Fable",), held, self._entries(open_outside), ("1", "3")
        ) is True
        assert _model_window_binds_everywhere(
            open_outside, ("Fable",), held, self._entries(open_outside), ("1", "2", "3")
        ) is False

    def test_without_credit_room_the_wall_stands(self):
        """Asserts: creditThreshold set but no account with credit room:
        every account is read at the plain threshold and the wall binds."""
        from claude_swap.autoswitch import _model_window_binds_everywhere

        usage = {"1": _fable(95.0), "2": _fable(100.0, _spend(0.0, reached=True))}
        settings = AutoSwitchSettings(threshold=90.0, credit_threshold=100.0)
        assert _model_window_binds_everywhere(
            usage, ("Fable",), settings, self._entries(usage), ("1", "2")
        ) is True


class TestDynamicBarReadsTheCreditPoint:
    """X3647: under `dynamic` an account holding its credit point is blocked
    and departs at that point, never at the strategy's fixed 97."""

    @staticmethod
    def _entry(usage: dict) -> UsageEntry:
        return UsageEntry(last_good=usage, fetched_at=0.0, age_s=0.0)

    def test_the_bar_per_strategy_and_credit_room(self):
        """Asserts: `account_switch_bar_pct` is the credit point for an
        account holding credits under `dynamic` and `best`, 97 for a plain
        account under `dynamic`, and the threshold for a plain `best` one."""
        from claude_swap.autoswitch import account_switch_bar_pct

        credit = self._entry(_full(_spend(20.0)))
        plain = self._entry(_full())
        dynamic = AutoSwitchSettings(strategy="dynamic", threshold=90.0, credit_threshold=100.0)
        best = AutoSwitchSettings(strategy="best", threshold=90.0, credit_threshold=100.0)
        assert account_switch_bar_pct(dynamic, credit) == 100.0
        assert account_switch_bar_pct(dynamic, plain) == 97.0
        assert account_switch_bar_pct(best, credit) == 100.0
        assert account_switch_bar_pct(best, plain) == 90.0
        unset = AutoSwitchSettings(strategy="dynamic", threshold=90.0)
        assert account_switch_bar_pct(unset, credit) == 97.0

    @pytest.mark.parametrize("with_credits", [True, False], ids=["credits", "plain"])
    def test_a_credit_active_at_98_is_not_a_dynamic_departure(
        self, temp_home, with_credits
    ):
        """Asserts: under `dynamic`, an active at 98% holding usage credits
        (credit point 100) is not classified `proactive` and stays, while
        the same active without credits leaves for the open peer."""
        hot = {"five_hour": {"pct": 98.0}, "seven_day": {"pct": 10.0}}
        active = {**hot, "spend": _spend(20.0)} if with_credits else hot
        h = EngineHarness(
            temp_home, strategy="dynamic", threshold=90.0, credit_threshold=100.0
        )
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        h.tick_with_usage({"1": active, "2": _open()})
        want_active = 1 if with_credits else 2
        assert h.active_number() == want_active, h.kinds()
        switched = [e for e in h.events if isinstance(e, SwitchEvent)]
        assert bool(switched) is not with_credits, h.kinds()


class TestDynamicCandidatesAtTheirOwnBar:
    """X3647 U8: `dynamic`'s own proactive and alternation lists judge each
    candidate at its own bar, so a candidate holding its credit point is
    not dropped at the fixed 97 line."""

    def test_the_ranking_keeps_a_credit_candidate_at_98(self):
        """Asserts: `_rank_dynamic_candidates` keeps a candidate at 98%
        whose bar is its credit point of 100, and drops a plain one at 98%
        whose bar is 97."""
        from claude_swap.autoswitch import _rank_dynamic_candidates

        bars = {"2": 100.0, "3": 97.0}
        warm, cold = _rank_dynamic_candidates(
            ["2", "3"], {"2": 2.0, "3": 2.0}, {"2": None, "3": None},
            0.0, {}, 3600.0, bars.__getitem__,
        )
        assert warm == []
        assert cold == ["2"]

    def test_a_walled_active_lands_on_the_credit_candidate_at_98(self, temp_home):
        """Asserts: under `dynamic` an active at 98% without credits (about
        to wall, the `proactive` trigger) switches to the only candidate,
        itself at 98% but holding usage credits (credit point 100)."""
        h = EngineHarness(
            temp_home, strategy="dynamic", threshold=90.0, credit_threshold=100.0
        )
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        hot = {"five_hour": {"pct": 98.0}, "seven_day": {"pct": 10.0}}
        outcome = h.tick_with_usage({"1": hot, "2": {**hot, "spend": _spend(20.0)}})
        assert outcome is TickOutcome.SWITCHED, h.kinds()
        assert h.active_number() == 2
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "proactive"


def _fraction(
    pct: float | None = 0.0, *, reached: bool = False, reason: str | None = None
) -> dict:
    """A setup-token account's spend read off its reply headers
    (``usage_store._header_fraction_spend``'s shape)."""
    return {
        "reported": "fraction", "used": None, "limit": None, "remaining": None,
        "pct": pct, "currency": None, "limit_reached": reached,
        "disabled_reason": reason,
    }


class TestHeaderMeasuredCredits:
    """X3655: a setup-token account's usage credits, known only as a share
    of its cap from its reply headers, count as credit room."""

    def test_all_full_moves_to_a_setup_token_account_on_credits(
        self, temp_home, caplog
    ):
        """Asserts: with every window full and only #2 holding an allowed
        fraction spend, the engine switches to #2 on credits, and the event
        and WARNING name its share of the cap rather than "no cap"."""
        h = _harness(temp_home)
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            outcome = h.tick_with_usage({
                "1": _full(), "2": _full(_fraction(0.0)), "3": _full(),
            })
        assert outcome is TickOutcome.SWITCHED, h.kinds()
        assert h.active_number() == 2
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "usage-credits"
        assert switch.detail == "credits on, 0% of cap used"
        credits = next(e for e in h.events if isinstance(e, SpendingUsageCreditsEvent))
        payload = credits.to_json()
        assert payload["reported"] == "fraction"
        assert payload["remaining"] is None
        assert payload["capUsedPct"] == 0.0
        assert _warnings(caplog, _CREDITS_LINE) == [
            "Every account's usage windows are full; sessions run on "
            "Account-2 usage credits (credits on, 0% of cap used), switching to it"
        ]

    def test_out_of_credits_is_not_room(self, temp_home):
        """Asserts: a fraction spend refused out_of_credits is no credit
        target, so the fleet waits."""
        h = _harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _full(),
            "2": _full(_fraction(None, reached=True, reason="out_of_credits")),
            "3": _full(),
        })
        assert outcome is TickOutcome.BLOCKED
        assert any(isinstance(e, AllExhaustedEvent) for e in h.events)

    def test_a_known_dollar_amount_outranks_a_fraction(self, temp_home):
        """Asserts: with the active out of credits, a peer with dollars left
        takes the sessions over a peer with an unused fraction."""
        h = _harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _full(), "2": _full(_fraction(0.0)), "3": _full(_spend(1.0)),
        })
        assert outcome is TickOutcome.SWITCHED, h.kinds()
        assert h.active_number() == 3

    def test_more_of_the_cap_unused_ranks_first_among_fractions(self, temp_home):
        """Asserts: between two fraction peers, the one with less of its cap
        used takes the sessions."""
        h = _harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _full(), "2": _full(_fraction(80.0)), "3": _full(_fraction(10.0)),
        })
        assert outcome is TickOutcome.SWITCHED, h.kinds()
        assert h.active_number() == 3

    def test_an_allowed_fraction_account_takes_the_credit_switch_point(self):
        """Asserts: `account_switch_point` gives an account whose reply
        allowed its credits the credit threshold (100), and one out of
        credits the plain threshold."""
        from claude_swap.settings import account_switch_point

        settings = AutoSwitchSettings(threshold=99.0, credit_threshold=100.0)
        on = UsageEntry(last_good={"five_hour": {"pct": 99.5}, "spend": _fraction()})
        out = UsageEntry(last_good={
            "five_hour": {"pct": 99.5},
            "spend": _fraction(None, reached=True, reason="out_of_credits"),
        })
        assert account_switch_point(settings, on) == 100.0
        assert account_switch_point(settings, out) == 99.0

    @pytest.mark.parametrize("spend, words", [
        (_fraction(0.0), "credits on, 0% of cap used"),
        (_fraction(None), "credits on, share of cap used unknown"),
        (_fraction(None, reached=True, reason="out_of_credits"),
         "credits on, out of credits"),
        (_fraction(None, reached=True, reason="org_level_disabled"),
         "credits refused (org_level_disabled)"),
        (_fraction(None, reached=True), "credits on, cap reached"),
    ])
    def test_list_and_dashboard_rows_name_the_fraction(self, spend, words):
        """Asserts: the `cswap list` `$$` row and the dashboard spend row read
        a fraction spend in words, with no dollar figure and no leading
        percent in the list row."""
        from claude_swap.tui.widgets import usage_rows

        assert spend_row_body(spend) == words
        assert _format_usage_lines({"spend": spend}) == [f"$$: {words}"]
        row = usage_rows({"spend": spend}, 0.0)[0]
        assert row[0] == "$$"
        assert row[1] == spend["pct"]
        assert row[2] == words
