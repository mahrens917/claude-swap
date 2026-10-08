"""A setup-token account with no usage reading is the probe target.

A ``claude setup-token`` login cannot read the usage endpoint (403 on every
ask), so its only reading comes from the reply headers recorded while it is
the active account. One that has never been active since the store moved to
schema 3 carries no row at all: its decision value is None, which every
ranking gate skipped as "unreadable", so the engine never switched onto it
and it stayed unread for good (board row X3650: zero probe switches in ten
hours on the rcbox, accounts 2 and 3 reading usageStatus "unavailable").
The probe (switching onto the account to read it) is the only way to read
one, so such an account is probe-eligible under every trigger, while an
OAuth account with no reading keeps waiting for its fetch."""

import logging

import pytest

from claude_swap.autoswitch import PROBE_COOLDOWN_S, NoSwitchEvent, SwitchEvent
from claude_swap.usage_store import UsageEntry
from tests.test_autoswitch import (
    _R_LATER,
    _R_SOON,
    EngineHarness,
    TickOutcome,
    _usage7,
)

_UNREAD_LINE = "has had no usage reading for"


def _harness(temp_home, **settings) -> EngineHarness:
    """The rcbox's rotation: consume-first, switch point 99, credit point 100."""
    kwargs = {"strategy": "consume-first", "threshold": 99.0, "credit_threshold": 100.0}
    kwargs.update(settings)
    h = EngineHarness(temp_home, **kwargs)
    h.seed(1, "a@example.com")
    h.seed(2, "tok@example.com")
    h.make_live("a@example.com", 1)
    return h


def _read(value: dict, now: float) -> UsageEntry:
    return UsageEntry(last_good=value, fetched_at=now, age_s=0.0)


def _unread_token() -> UsageEntry:
    """The collector's entry for a setup-token slot with no store row:
    marked header-only from its credential, nothing read."""
    return UsageEntry(header_only=True)


def _switch(h: EngineHarness) -> SwitchEvent:
    return next(e for e in h.events if isinstance(e, SwitchEvent))


def _warnings(caplog, needle: str) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and needle in r.getMessage()
    ]


class TestTheUnreadTokenAccountIsProbed:
    def test_an_at_limit_active_probes_the_unread_token_account(self, temp_home):
        """Asserts: with the active at its limit and the only candidate a
        setup-token account with no reading, the engine switches onto that
        account under the `probe` trigger and records its probe cooldown."""
        h = _harness(temp_home)
        now = h.clock.now
        outcome = h.tick_with_entries({
            "1": _read(_usage7(100.0, 60.0, _R_LATER), now),
            "2": _unread_token(),
        })
        assert outcome is TickOutcome.SWITCHED, h.kinds()
        assert h.active_number() == 2
        assert _switch(h).trigger == "probe"
        assert h.state()["probeCooldown"]["2"] == now + PROBE_COOLDOWN_S

    def test_an_active_past_its_switch_point_probes_it_too(self, temp_home):
        """Asserts: an active above its switch point but not walled (the
        `proactive` trigger) also probes the unread setup-token account."""
        h = _harness(temp_home)
        now = h.clock.now
        outcome = h.tick_with_entries({
            "1": _read(_usage7(99.5, 60.0, _R_LATER), now),
            "2": _unread_token(),
        })
        assert outcome is TickOutcome.SWITCHED, h.kinds()
        assert h.active_number() == 2
        assert _switch(h).trigger == "probe"

    def test_a_below_threshold_consume_first_tick_probes_it(self, temp_home):
        """Asserts: the consume-first nudge below the switch point, which
        already probes a measured account whose weekly reset is unknown,
        probes the unread setup-token account the same way."""
        h = _harness(temp_home)
        now = h.clock.now
        outcome = h.tick_with_entries({
            "1": _read(_usage7(40.0, 60.0, _R_LATER), now),
            "2": _unread_token(),
        })
        assert outcome is TickOutcome.SWITCHED, h.kinds()
        assert h.active_number() == 2
        assert _switch(h).trigger == "probe"

    def test_an_at_limit_probe_is_not_held_by_the_switch_cooldown(self, temp_home):
        """Asserts: the at-limit probe escapes a walled active inside the
        anti-flap cooldown like every at-limit move: `_perform` keys its
        cooldown recheck on the tick's trigger, never on the `probe` name."""
        h = _harness(temp_home)
        now = h.clock.now
        h.switcher._write_json(
            h.switcher.backup_dir / "autoswitch_state.json",
            {"lastSwitchAt": now - 10.0},
        )
        outcome = h.tick_with_entries({
            "1": _read(_usage7(100.0, 60.0, _R_LATER), now),
            "2": _unread_token(),
        })
        assert outcome is TickOutcome.SWITCHED, h.kinds()
        assert _switch(h).trigger == "probe"

    def test_a_measured_landing_outranks_the_unread_probe(self, temp_home):
        """Asserts: when the active must leave and a measured candidate can
        take the sessions, the engine lands there, never on the unread
        account it knows nothing about."""
        h = _harness(temp_home)
        h.seed(3, "c@example.com")
        now = h.clock.now
        outcome = h.tick_with_entries({
            "1": _read(_usage7(100.0, 60.0, _R_LATER), now),
            "2": _unread_token(),
            "3": _read(_usage7(10.0, 10.0, _R_SOON), now),
        })
        assert outcome is TickOutcome.SWITCHED, h.kinds()
        assert h.active_number() == 3
        assert _switch(h).trigger == "at-limit"

    def test_the_probe_cooldown_keeps_it_from_being_reprobed(self, temp_home):
        """Asserts: an unread setup-token account still inside its probe
        cooldown is not switched onto again; the at-limit tick reports that
        nothing readable is left instead."""
        h = _harness(temp_home)
        now = h.clock.now
        h.switcher._write_json(
            h.switcher.backup_dir / "autoswitch_state.json",
            {"probeCooldown": {"2": now + PROBE_COOLDOWN_S - 1}},
        )
        outcome = h.tick_with_entries({
            "1": _read(_usage7(100.0, 60.0, _R_LATER), now),
            "2": _unread_token(),
        })
        assert outcome is TickOutcome.BLOCKED, h.kinds()
        assert h.active_number() == 1
        assert not any(isinstance(e, SwitchEvent) for e in h.events)

    def test_a_probe_then_the_return_does_not_reprobe_next_tick(self, temp_home):
        """Asserts: after a probe switch and a return to the first account,
        the next at-limit tick does not pick the same unread account again
        while the cooldown the probe wrote is still running."""
        h = _harness(temp_home)
        now = h.clock.now
        entries = {
            "1": _read(_usage7(100.0, 60.0, _R_LATER), now),
            "2": _unread_token(),
        }
        assert h.tick_with_entries(entries) is TickOutcome.SWITCHED
        h.set_active(1)
        h.make_live("a@example.com", 1)
        h.events.clear()
        h.clock.advance(600.0)
        outcome = h.tick_with_entries(entries)
        assert outcome is not TickOutcome.SWITCHED, h.kinds()
        assert h.active_number() == 1

    def test_an_unread_oauth_account_is_not_probed(self, temp_home):
        """Asserts: an OAuth account with no reading keeps today's rule (it
        can be fetched, so it is never switched onto blind): the at-limit
        tick blocks with no candidate readable."""
        h = _harness(temp_home)
        now = h.clock.now
        outcome = h.tick_with_entries({
            "1": _read(_usage7(100.0, 60.0, _R_LATER), now),
            "2": UsageEntry(),
        })
        assert outcome is TickOutcome.BLOCKED, h.kinds()
        assert h.active_number() == 1
        hold = next(e for e in h.events if isinstance(e, NoSwitchEvent))
        assert hold.reason == "no-comparison"

    def test_a_struck_token_account_is_not_probed(self, temp_home):
        """Asserts: a setup-token account carrying the API's refusal
        sentinel is a known state, not an unread one, and is never probed."""
        h = _harness(temp_home)
        now = h.clock.now
        outcome = h.tick_with_entries({
            "1": _read(_usage7(100.0, 60.0, _R_LATER), now),
            "2": UsageEntry(sentinel="relogin-required", header_only=True),
        })
        assert outcome is not TickOutcome.SWITCHED, h.kinds()
        assert h.active_number() == 1

    def test_dynamic_walled_active_probes_the_unread_token_account(self, temp_home):
        """Asserts: under `dynamic`, an active about to wall (98%, the
        `proactive` arm that never enters `_rank_candidates_pass`) with the
        only candidate an unread setup-token account switches onto it as a
        probe and records its probe cooldown."""
        h = _harness(temp_home, strategy="dynamic", threshold=90.0)
        now = h.clock.now
        outcome = h.tick_with_entries({
            "1": _read(_usage7(98.0, 60.0, _R_LATER), now),
            "2": _unread_token(),
        })
        assert outcome is TickOutcome.SWITCHED, h.kinds()
        assert h.active_number() == 2
        assert _switch(h).trigger == "probe"
        assert h.state()["probeCooldown"]["2"] == now + PROBE_COOLDOWN_S

    def test_dynamic_walled_active_takes_a_measured_landing_first(self, temp_home):
        """Asserts: under `dynamic` the unread account ranks after every
        measured landing: with a healthy measured candidate the walled
        active lands there under `proactive`, never on the probe."""
        h = _harness(temp_home, strategy="dynamic", threshold=90.0)
        h.seed(3, "c@example.com")
        now = h.clock.now
        outcome = h.tick_with_entries({
            "1": _read(_usage7(98.0, 60.0, _R_LATER), now),
            "2": _unread_token(),
            "3": _read(_usage7(10.0, 10.0, _R_SOON), now),
        })
        assert outcome is TickOutcome.SWITCHED, h.kinds()
        assert h.active_number() == 3
        assert _switch(h).trigger == "proactive"

    def test_dynamic_healthy_active_probes_the_unread_token_account(self, temp_home):
        """Asserts: under `dynamic`, a healthy active (40%, the alternation
        arm) with no alternation partner probes the unread setup-token
        account instead of holding it unread."""
        h = _harness(temp_home, strategy="dynamic", threshold=90.0)
        now = h.clock.now
        outcome = h.tick_with_entries({
            "1": _read(_usage7(40.0, 60.0, _R_LATER), now),
            "2": _unread_token(),
        })
        assert outcome is TickOutcome.SWITCHED, h.kinds()
        assert h.active_number() == 2
        assert _switch(h).trigger == "probe"

    @pytest.mark.parametrize("active_pct", [98.0, 40.0], ids=["walled", "healthy"])
    def test_dynamic_probe_cooldown_keeps_it_from_being_reprobed(
        self, temp_home, active_pct
    ):
        """Asserts: under `dynamic`, on both its own arms, an unread
        setup-token account inside its probe cooldown is not switched onto."""
        h = _harness(temp_home, strategy="dynamic", threshold=90.0)
        now = h.clock.now
        h.switcher._write_json(
            h.switcher.backup_dir / "autoswitch_state.json",
            {"probeCooldown": {"2": now + PROBE_COOLDOWN_S - 1}},
        )
        h.tick_with_entries({
            "1": _read(_usage7(active_pct, 60.0, _R_LATER), now),
            "2": _unread_token(),
        })
        assert h.active_number() == 1, h.kinds()
        assert not any(isinstance(e, SwitchEvent) for e in h.events)

    def test_dynamic_healthy_probe_respects_the_switch_cooldown(self, temp_home):
        """Asserts: the healthy-arm probe is a discretionary move, held by
        the anti-flap switch cooldown the way alternation is."""
        h = _harness(temp_home, strategy="dynamic", threshold=90.0)
        now = h.clock.now
        h.switcher._write_json(
            h.switcher.backup_dir / "autoswitch_state.json",
            {"lastSwitchAt": now - 10.0},
        )
        outcome = h.tick_with_entries({
            "1": _read(_usage7(40.0, 60.0, _R_LATER), now),
            "2": _unread_token(),
        })
        assert outcome is TickOutcome.NO_ACTION, h.kinds()
        hold = next(e for e in h.events if isinstance(e, NoSwitchEvent))
        assert hold.reason == "cooldown"

    def test_a_stale_probe_pick_does_not_label_a_dynamic_switch(self, temp_home):
        """Asserts: a probe pick an earlier tick's ranking left behind never
        names a later `dynamic` arm's switch a probe: the walled active's
        landing on a measured candidate is `proactive`, and no probe
        cooldown is written for it."""
        h = _harness(temp_home, strategy="dynamic", threshold=90.0)
        now = h.clock.now
        h.engine._last_probe_num = "2"
        h.switcher._write_json(
            h.switcher.backup_dir / "autoswitch_state.json", {}
        )
        outcome = h.tick_with_entries({
            "1": _read(_usage7(98.0, 60.0, _R_LATER), now),
            "2": _read(_usage7(10.0, 10.0, _R_SOON), now),
        })
        assert outcome is TickOutcome.SWITCHED, h.kinds()
        assert _switch(h).trigger == "proactive"
        assert "2" not in (h.state().get("probeCooldown") or {})

    def test_the_collector_marks_a_rowless_token_slot_header_only(self, temp_home):
        """Asserts: the header-only mark the probe keys on comes from the
        slot's stored login (a setup-token credential), not from a store
        row: a token slot with no row at all still reads header-only, with
        nothing read."""
        h = _harness(temp_home)
        h.switcher.add_account_from_token("sk-ant-oat01-x", "tok3@example.com", slot=3)
        entries = h.switcher.usage_entries_by_account(fetch=set())
        assert entries["3"].header_only is True
        assert entries["3"].decision_value() is None
        store = h.switcher._usage_store
        rows = store._read_rows()
        rows.pop("3", None)
        store._write_rows(rows)
        entries = h.switcher.usage_entries_by_account(fetch=set())
        assert "3" not in store._read_rows()
        assert entries["3"].header_only is True
        assert entries["3"].decision_value() is None


class TestTheUnreadCandidateAlarm:
    def test_warns_once_when_a_candidate_stays_unread_past_a_probe_cooldown(
        self, temp_home, caplog
    ):
        """Asserts: a rotation candidate with no usage reading for longer
        than one probe cooldown, while the engine looks for a candidate,
        logs one WARNING naming the account and how long, and only once for
        that stretch however many ticks follow."""
        h = _harness(temp_home)
        h.switcher._write_json(
            h.switcher.backup_dir / "autoswitch_state.json",
            {"probeCooldown": {"2": h.clock.now + 10 * PROBE_COOLDOWN_S}},
        )
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            for _ in range(4):
                h.tick_with_entries({
                    "1": _read(_usage7(100.0, 60.0, _R_LATER), h.clock.now),
                    "2": _unread_token(),
                })
                h.clock.advance(PROBE_COOLDOWN_S / 2 + 1)
        lines = _warnings(caplog, _UNREAD_LINE)
        assert len(lines) == 1, lines
        assert "Account-2" in lines[0]
        assert "60 min" in lines[0], lines[0]

    def test_a_reading_ends_the_stretch_and_a_new_one_warns_again(
        self, temp_home, caplog
    ):
        """Asserts: once the candidate is read the stretch ends, and a later
        unread stretch past a probe cooldown warns once more."""
        h = _harness(temp_home)
        h.seed(3, "c@example.com")
        unread = {"3": UsageEntry()}

        def tick(three: UsageEntry | None) -> None:
            now = h.clock.now
            h.tick_with_entries({
                "1": _read(_usage7(40.0, 60.0, _R_SOON), now),
                "2": _read(_usage7(40.0, 60.0, _R_LATER), now),
                "3": three if three is not None else _read(_usage7(40.0, 60.0, _R_LATER), now),
            })

        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            tick(unread["3"])
            h.clock.advance(PROBE_COOLDOWN_S + 1)
            tick(unread["3"])
            tick(unread["3"])
            tick(None)
            h.clock.advance(60.0)
            tick(unread["3"])
            h.clock.advance(PROBE_COOLDOWN_S + 1)
            tick(unread["3"])
        lines = _warnings(caplog, _UNREAD_LINE)
        assert len(lines) == 2, lines
        assert all("Account-3" in line for line in lines)

    def test_a_healthy_pool_logs_nothing(self, temp_home, caplog):
        """Asserts: zero alarm lines when every candidate carries a reading."""
        h = _harness(temp_home)
        with caplog.at_level(logging.WARNING, logger="claude-swap"):
            for _ in range(3):
                now = h.clock.now
                h.tick_with_entries({
                    "1": _read(_usage7(40.0, 60.0, _R_SOON), now),
                    "2": _read(_usage7(40.0, 60.0, _R_LATER), now),
                })
                h.clock.advance(PROBE_COOLDOWN_S + 1)
        assert _warnings(caplog, _UNREAD_LINE) == []
