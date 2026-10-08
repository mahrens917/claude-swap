"""Usage-credit rotation: when every account's 5h/7d windows are full, the
auto engine keeps work going on the account that still holds paid usage
credits (Anthropic's extra usage) instead of waiting for the earliest reset,
and `cswap list` shows the money left per account."""

import logging

from claude_swap.autoswitch import (
    AllExhaustedEvent,
    PollEvent,
    SpendingUsageCreditsEvent,
    SwitchEvent,
)
from claude_swap.switcher import _format_usage_lines, spend_row_body
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
        assert credits.remaining == 591.89
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
                 "currency": "USD", "limit_reached": False}
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
                 "currency": "USD", "limit_reached": True}
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
