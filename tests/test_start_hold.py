"""The Remote Control start hold (board row X3768).

`claude remote-control` refuses to start on an inference-only login. The
owner proxy subcommand's `--ensure --remote-control-start` makes the owner
account active for the start and writes a hold that stops the auto-switch
engine until the owner proxy sees the server register.
"""

from __future__ import annotations

import importlib
import json
import logging
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import autoswitch as autoswitch_mod
from claude_swap import cli, start_hold
from claude_swap.autoswitch import (
    STATE_FILENAME,
    ErrorEvent,
    NoSwitchEvent,
    SwitchEvent,
    TickOutcome,
    engine_state_lock,
)
from claude_swap.start_hold import (
    REMOTE_CONTROL_SCOPE,
    START_HOLD_MAX_S,
    StartHold,
    StartHoldError,
    read_start_hold,
    start_hold_path,
    write_start_hold,
)
from tests.test_autoswitch import EngineHarness, _usage

owner_proxy = importlib.import_module("claude_swap.pin")

FULL_SCOPES = [
    "user:inference", "user:profile", REMOTE_CONTROL_SCOPE,
    "user:file_upload", "user:mcp_servers", "org:create_api_key",
]


def _login(scopes: list[str], *, refresh: bool) -> str:
    blob: dict = {"accessToken": "sk-test", "scopes": scopes}
    if refresh:
        blob["refreshToken"] = "rt-test"
    return json.dumps({"claudeAiOauth": blob})


FULL_LOGIN = _login(FULL_SCOPES, refresh=True)
INFERENCE_LOGIN = _login(["user:inference"], refresh=False)


class FakeSwitcher:
    """The switcher surface the start hold reads: the live login, the active
    account, and `switch_to`. `switch_to` records what it saw."""

    def __init__(self, backup_dir: Path, *, live: str | None, active: str,
                 result: dict | None = None, raises: Exception | None = None,
                 lands: str | None = FULL_LOGIN):
        self.backup_dir = backup_dir
        self.live = live
        self.active = active
        self.result = {"switched": True} if result is None else result
        self.raises = raises
        self.lands = lands
        self.calls: list[tuple[str, bool]] = []
        self.hold_during_switch: StartHold | None = None
        self.lock_free_during_switch: bool | None = None

    def _read_credentials(self) -> str | None:
        return self.live

    def current_account_number(self) -> str | None:
        return self.active

    def switch_to(self, identifier: str, json_output: bool = False,
                  force: bool = False) -> dict | None:
        self.calls.append((identifier, json_output))
        self.hold_during_switch = read_start_hold(self.backup_dir)
        probe = engine_state_lock(self.backup_dir / STATE_FILENAME)
        self.lock_free_during_switch = probe.acquire(timeout=0)
        probe.release()
        if self.raises is not None:
            raise self.raises
        if self.result.get("switched"):
            self.active = identifier
            self.live = self.lands
        return self.result


@pytest.fixture
def owner_is_account_1(monkeypatch):
    monkeypatch.setattr(owner_proxy, "_pinned_email_now",
                        lambda _sw: ("owner@example.com", ""))
    monkeypatch.setattr(owner_proxy, "pinned_slot", lambda _sw: "1")


class TestHoldFile:
    def test_a_missing_file_is_no_hold(self, tmp_path):
        """Asserts: no start_hold.json reads as no hold."""
        assert read_start_hold(tmp_path) is None

    def test_a_written_hold_reads_back(self, tmp_path):
        """Asserts: the hold round-trips through start_hold.json with its
        three fields."""
        hold = StartHold(set_at=1234.5, owner="1", previous="2")
        write_start_hold(tmp_path, hold)
        assert read_start_hold(tmp_path) == hold
        assert json.loads(start_hold_path(tmp_path).read_text()) == {
            "setAt": 1234.5, "owner": "1", "previous": "2"}

    @pytest.mark.parametrize("text", [
        "not json", "[]", '{"owner": "1"}', '{"setAt": 1, "owner": ""}',
        '{"setAt": true, "owner": "1"}', '{"setAt": 1, "owner": "1", "previous": 2}',
    ])
    def test_a_corrupt_file_raises(self, tmp_path, text):
        """Asserts: a hold file that is there but not a hold raises rather
        than reading as no hold or as held."""
        start_hold_path(tmp_path).write_text(text)
        with pytest.raises(StartHoldError):
            read_start_hold(tmp_path)


class TestEnsureRemoteControlStart:
    def test_full_scope_active_login_switches_nothing(
            self, tmp_path, owner_is_account_1, capsys):
        """Asserts: a full-scope active login needs no switch and no hold,
        and the decision is one stderr line."""
        sw = FakeSwitcher(tmp_path, live=FULL_LOGIN, active="2")
        assert start_hold.hold_for_remote_control_start(sw) is None
        assert sw.calls == []
        assert read_start_hold(tmp_path) is None
        assert capsys.readouterr().err == (
            "start hold: not needed, active login is full-scope\n")

    def test_inference_only_active_login_makes_the_owner_active_under_a_hold(
            self, tmp_path, owner_is_account_1, capsys, caplog):
        """Asserts: an inference-only active login makes the owner account
        active through `switch_to`, with the hold already written and the
        engine state lock held while it switches, and the hold line on
        stderr and in the log."""
        sw = FakeSwitcher(tmp_path, live=INFERENCE_LOGIN, active="2")
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            hold = start_hold.hold_for_remote_control_start(
                sw, clock=lambda: 5000.0)
        assert sw.calls == [("1", True)]
        assert sw.hold_during_switch == StartHold(5000.0, "1", "2")
        assert sw.lock_free_during_switch is False
        assert hold == StartHold(5000.0, "1", "2")
        assert read_start_hold(tmp_path) == hold
        line = ("start hold: owner account 1 made active for a Remote Control "
                "start (was 2)")
        assert capsys.readouterr().err == line + "\n"
        assert line in [r.getMessage() for r in caplog.records
                        if r.levelno == logging.INFO]

    def test_no_owner_set_says_so_and_switches_nothing(self, tmp_path,
                                                       monkeypatch, capsys):
        """Asserts: with no owner account set there is nothing to switch
        to, and the decision is one stderr line."""
        monkeypatch.setattr(owner_proxy, "_pinned_email_now", lambda _sw: None)
        sw = FakeSwitcher(tmp_path, live=INFERENCE_LOGIN, active="2")
        assert start_hold.hold_for_remote_control_start(sw) is None
        assert sw.calls == []
        assert capsys.readouterr().err == (
            "start hold: not needed, no owner account is set\n")

    def test_a_refused_switch_removes_the_hold_with_an_error(
            self, tmp_path, owner_is_account_1, capsys, caplog):
        """Asserts: a switch that does not land removes the hold and reports
        an ERROR naming the owner and the reason."""
        sw = FakeSwitcher(tmp_path, live=INFERENCE_LOGIN, active="2",
                          result={"switched": False,
                                  "reason": "target-credential-dead"})
        with caplog.at_level(logging.ERROR, logger="claude-swap"):
            assert start_hold.hold_for_remote_control_start(sw) is None
        assert read_start_hold(tmp_path) is None
        err = capsys.readouterr().err
        assert err == ("start hold: failed: could not make owner account 1 "
                       "active (was 2): target-credential-dead\n")
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    def test_a_raising_switch_removes_the_hold_and_does_not_raise(
            self, tmp_path, owner_is_account_1, capsys):
        """Asserts: a raise from the switch removes the hold, never escapes
        the launch path, and is reported on stderr."""
        sw = FakeSwitcher(tmp_path, live=INFERENCE_LOGIN, active="2",
                          raises=RuntimeError("store busy"))
        assert start_hold.hold_for_remote_control_start(sw) is None
        assert read_start_hold(tmp_path) is None
        assert capsys.readouterr().err == (
            "start hold: failed: RuntimeError: store busy\n")

    def test_an_owner_login_without_the_scope_removes_the_hold(
            self, tmp_path, owner_is_account_1, capsys):
        """Asserts: when the owner's own login lacks the Remote Control
        scope after the switch, the hold is removed with an ERROR, because
        no server can start to release it."""
        sw = FakeSwitcher(tmp_path, live=INFERENCE_LOGIN, active="2",
                          lands=INFERENCE_LOGIN)
        assert start_hold.hold_for_remote_control_start(sw) is None
        assert read_start_hold(tmp_path) is None
        assert "lacks user:sessions:claude_code" in capsys.readouterr().err

    def test_owner_already_active_without_the_scope_is_an_error(
            self, tmp_path, owner_is_account_1, capsys):
        """Asserts: the owner active with an inference-only login is an
        ERROR and no hold, since switching cannot help."""
        sw = FakeSwitcher(tmp_path, live=INFERENCE_LOGIN, active="1")
        assert start_hold.hold_for_remote_control_start(sw) is None
        assert sw.calls == []
        assert read_start_hold(tmp_path) is None
        assert "owner account 1 is active but its login lacks" in (
            capsys.readouterr().err)


class TestEnsureFlag:
    def _ensured(self, monkeypatch):
        """Stub the proxy repair so `run(ensure=True)` reaches its end."""
        monkeypatch.setattr(owner_proxy, "_wiring_present", lambda _sw: True)
        monkeypatch.setattr(owner_proxy, "heal", lambda _sw, **_k: (False, ""))
        monkeypatch.setattr(owner_proxy, "_dead_wired_configs",
                            lambda _sw, **_k: [])

    def test_plain_ensure_never_switches_or_holds(
            self, tmp_path, monkeypatch, owner_is_account_1, capsys):
        """Asserts: plain `--ensure` (run before every hand-launched claude)
        with an inference-only active login switches nothing, writes no
        hold and prints nothing."""
        self._ensured(monkeypatch)
        sw = FakeSwitcher(tmp_path, live=INFERENCE_LOGIN, active="2")
        assert owner_proxy.run(sw, None, ensure=True) == 0
        assert sw.calls == []
        assert read_start_hold(tmp_path) is None
        assert capsys.readouterr() == ("", "")

    def test_remote_control_start_runs_the_hold_after_the_repair(
            self, tmp_path, monkeypatch, owner_is_account_1, capsys):
        """Asserts: `--ensure --remote-control-start` exits 0 and makes the
        owner active, with nothing on stdout."""
        self._ensured(monkeypatch)
        sw = FakeSwitcher(tmp_path, live=INFERENCE_LOGIN, active="2")
        assert owner_proxy.run(sw, None, ensure=True,
                               remote_control_start=True) == 0
        assert sw.calls == [("1", True)]
        out, err = capsys.readouterr()
        assert out == ""
        assert "made active for a Remote Control start (was 2)" in err

    def test_remote_control_start_with_nothing_wired_still_reports(
            self, tmp_path, monkeypatch, capsys):
        """Asserts: the early return for an unwired machine with no owner
        still prints the start's decision line."""
        monkeypatch.setattr(owner_proxy, "_wiring_present", lambda _sw: False)
        monkeypatch.setattr(owner_proxy, "_pinned_email_now", lambda _sw: None)
        sw = FakeSwitcher(tmp_path, live=INFERENCE_LOGIN, active="2")
        assert owner_proxy.run(sw, None, ensure=True,
                               remote_control_start=True) == 0
        assert capsys.readouterr().err == (
            "start hold: not needed, no owner account is set\n")

    def test_remote_control_start_without_ensure_is_refused(self, capsys):
        """Asserts: `--remote-control-start` alone is an argument error."""
        with pytest.raises(SystemExit) as exc:
            cli._pin_command(["--remote-control-start"])
        assert exc.value.code == 2
        assert "only together with --ensure" in capsys.readouterr().err


class TestEnsureAgainstARealStore:
    def test_the_owner_becomes_active_through_the_real_switch(
            self, temp_home, monkeypatch):
        """Asserts: through the real switcher, an inference-only active
        account 2 gives way to the full-scope owner account 1, and the
        live login then carries the Remote Control scope."""
        h = EngineHarness(temp_home)
        h.seed(1, "owner@example.com")
        h.seed(2, "token@example.com")
        # Attributed: this test owns both slots and replaces their seeds.
        h.switcher._write_account_credentials(
            "1", "owner@example.com", FULL_LOGIN, attributed=True)
        h.switcher._write_account_credentials(
            "2", "token@example.com", INFERENCE_LOGIN, attributed=True)
        (temp_home / ".claude" / ".credentials.json").write_text(INFERENCE_LOGIN)
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "token@example.com",
                             "accountUuid": "uuid-2"}}))
        h.set_active(2)
        monkeypatch.setattr(owner_proxy, "_pinned_email_now",
                            lambda _sw: ("owner@example.com", ""))
        monkeypatch.setattr(owner_proxy, "pinned_slot", lambda _sw: "1")
        hold = start_hold.hold_for_remote_control_start(h.switcher)
        assert hold is not None and (hold.owner, hold.previous) == ("1", "2")
        assert h.active_number() == 1
        assert REMOTE_CONTROL_SCOPE in start_hold.credential_scopes(
            h.switcher._read_credentials())
        assert read_start_hold(h.switcher.backup_dir) == hold


@pytest.fixture
def harness(temp_home: Path) -> EngineHarness:
    h = EngineHarness(temp_home)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.seed(3, "c@example.com")
    h.make_live("a@example.com", 1)
    h.engine.settings = replace(h.engine.settings, strategy="best")
    return h


# Account 1 is over the threshold and 3 has the most room, so without a
# hold this tick switches 1 -> 3 (TestDecisionTable's proactive case).
_SWITCHING_USAGE = {"1": _usage(95), "2": _usage(40), "3": _usage(20)}


class TestAutoSwitchHonoursTheHold:
    def _hold(self, h, age_s: float) -> StartHold:
        hold = StartHold(set_at=h.clock.now - age_s, owner="1", previous="2")
        write_start_hold(h.switcher.backup_dir, hold)
        return hold

    def test_a_fresh_hold_switches_nothing(self, harness):
        """Asserts: a hold younger than START_HOLD_MAX_S stops the tick with
        no-switch reason start-hold, and leaves the hold in place."""
        hold = self._hold(harness, 10.0)
        outcome = harness.tick_with_usage(_SWITCHING_USAGE)
        assert outcome is TickOutcome.NO_ACTION
        assert harness.active_number() == 1
        reasons = [e.reason for e in harness.events
                   if isinstance(e, NoSwitchEvent)]
        assert reasons == ["start-hold"]
        assert read_start_hold(harness.switcher.backup_dir) == hold
        payload = next(e for e in harness.events
                       if isinstance(e, NoSwitchEvent)).to_json()
        assert payload["event"] == "no-switch"
        assert payload["reason"] == "start-hold"

    def test_a_stale_hold_is_removed_with_an_error_and_the_tick_runs(
            self, harness):
        """Asserts: a hold older than START_HOLD_MAX_S is deleted with a
        non-transient ERROR event naming the expiry, and the tick then
        switches as it would have."""
        self._hold(harness, START_HOLD_MAX_S + 1)
        outcome = harness.tick_with_usage(_SWITCHING_USAGE)
        assert read_start_hold(harness.switcher.backup_dir) is None
        errors = [e for e in harness.events if isinstance(e, ErrorEvent)]
        assert len(errors) == 1
        assert errors[0].message.startswith(
            "start hold expired after 300s with no Remote Control registration")
        assert errors[0].transient is False
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 3

    def test_a_dry_run_engine_leaves_a_stale_hold_to_the_live_one(
            self, harness):
        """Asserts: dry-run neither deletes a stale hold nor reports it."""
        self._hold(harness, START_HOLD_MAX_S + 1)
        harness.engine.dry_run = True
        harness.tick_with_usage(_SWITCHING_USAGE)
        assert read_start_hold(harness.switcher.backup_dir) is not None
        assert not any(isinstance(e, ErrorEvent) for e in harness.events)

    def test_a_hold_landing_after_the_ticks_read_stops_the_switch(
            self, harness):
        """Asserts: the re-read under the state lock refuses a switch when
        a hold was written after the tick's first read."""
        self._hold(harness, 1.0)
        real = autoswitch_mod.read_start_hold
        reads = []

        def first_read_predates_the_hold(backup_dir):
            reads.append(1)
            return None if len(reads) == 1 else real(backup_dir)

        with patch.object(autoswitch_mod, "read_start_hold",
                          side_effect=first_read_predates_the_hold), \
             patch.object(harness.switcher, "switch_to",
                          wraps=harness.switcher.switch_to) as switch_to:
            outcome = harness.tick_with_usage(_SWITCHING_USAGE)
        assert len(reads) >= 2, "the lock-side re-read never ran"
        assert outcome is TickOutcome.NO_ACTION
        switch_to.assert_not_called()
        assert harness.active_number() == 1
        assert not any(isinstance(e, SwitchEvent) for e in harness.events)
        assert [e.reason for e in harness.events
                if isinstance(e, NoSwitchEvent)] == ["start-hold"]

    def test_a_corrupt_hold_is_a_tick_error(self, harness):
        """Asserts: a corrupt hold file makes the tick an ERROR, never a
        switch."""
        start_hold_path(harness.switcher.backup_dir).write_text("not json")
        outcome = harness.tick_with_usage(_SWITCHING_USAGE)
        assert outcome is TickOutcome.ERROR
        assert harness.active_number() == 1
