"""A setup-token account the API refuses is struck out of rotation.

A ``claude setup-token`` credential has no refresh path and never reaches the
usage endpoint, so nothing but the owner proxy's view of a ``/v1/messages``
refusal can tell that its token died early (revoked, or the plan lapsed).
``ClaudeAccountSwitcher.record_credential_refused`` turns that refusal into
the same quarantine a dead browser login gets, and the auto engine leaves a
struck active token slot on its next tick.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import patch

from claude_swap import oauth, usage_store
from claude_swap.json_output import USAGE_RELOGIN_REQUIRED
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import (
    STRIKE_SOURCE_API_REFUSAL,
    FetchRecord,
    UsageEntry,
    UsageStore,
)
from tests.test_autoswitch import EngineHarness, TickOutcome, _usage7

TOKEN = "sk-ant-oat01-x"
EMAIL = "tok@example.com"
HEADERS = {
    usage_store.USAGE_HEADER_5H_PCT: "0.05",
    usage_store.USAGE_HEADER_7D_PCT: "0.30",
}
WARNING_TEXT = (
    "setup-token account 2 (tok@example.com) refused by the API (http-401); "
    "out of rotation until its token is re-added with cswap add-token"
)


def _linux_switcher() -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._init_sequence_file()
    return s


def _token_switcher() -> tuple[ClaudeAccountSwitcher, str]:
    """A switcher holding a setup-token in slot 2; returns it and the
    fingerprint of the slot's stored credential."""
    switcher = _linux_switcher()
    switcher.add_account_from_token(TOKEN, EMAIL, slot=2)
    stored = switcher._read_account_credentials("2", EMAIL)
    assert oauth.is_setup_token_credential(stored)  # premise
    return switcher, oauth.credential_fingerprint(stored)


def _warnings(caplog) -> list[str]:
    return [
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    ]


class TestRecordCredentialRefused:
    def test_a_refused_live_token_slot_is_struck_and_warned(
        self, temp_home, caplog
    ):
        """Asserts: a 401 on the live setup-token slot's own credential
        strikes the slot (relogin_required, token_dead) even though a reply
        header reading preceded it, and logs the one WARNING naming slot,
        email and status."""
        switcher, fp = _token_switcher()
        assert switcher.record_usage_headers("2", HEADERS) is True
        live = temp_home / ".claude" / ".credentials.json"
        live.write_text(switcher._read_account_credentials("2", EMAIL))
        with (
            patch.object(switcher, "current_account_number", return_value="2"),
            caplog.at_level(logging.INFO, logger="claude-swap"),
        ):
            assert switcher.record_credential_refused("2", 401, fp) is True
            entry = switcher.usage_entries_by_account(fetch=set())["2"]
        assert _warnings(caplog) == [WARNING_TEXT]
        assert entry.strike_source == STRIKE_SOURCE_API_REFUSAL
        assert entry.token_dead() is True
        assert entry.sentinel == USAGE_RELOGIN_REQUIRED

    def test_the_live_bytes_are_matched_when_they_differ_from_the_backup(
        self, temp_home
    ):
        """Asserts: on the active slot the refused credential is matched
        against the live login too, so live bytes that differ from the saved
        backup still strike, and the strike binds the saved copy's
        fingerprint, so `token_dead` read against the saved copy (what the
        collector and the switch gates compare) still holds it."""
        switcher, saved_fp = _token_switcher()
        live_bytes = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": TOKEN,
                    "scopes": ["user:inference"],
                    "subscriptionType": "max",
                }
            }
        )
        (temp_home / ".claude" / ".credentials.json").write_text(live_bytes)
        with patch.object(switcher, "current_account_number", return_value="2"):
            assert switcher.record_credential_refused(
                "2", 401, oauth.credential_fingerprint(live_bytes)
            ) is True
            entry = switcher.usage_entries_by_account(fetch=set())["2"]
        assert oauth.credential_fingerprint(live_bytes) != saved_fp  # premise
        assert entry.token_dead(stored_fp=saved_fp) is True

    def test_a_replaced_credential_strikes_nothing(self, temp_home, caplog):
        """Asserts: a refusal naming a fingerprint no stored credential
        carries (the token was replaced since the request went out) strikes
        nothing and logs no WARNING."""
        switcher, _ = _token_switcher()
        stale_fp = oauth.credential_fingerprint(
            json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat01-old"}})
        )
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            assert switcher.record_credential_refused("2", 401, stale_fp) is False
        entry = switcher.usage_entries_by_account(fetch=set())["2"]
        assert entry.auth_dead_strikes == 0
        assert _warnings(caplog) == []

    def test_a_browser_login_is_never_struck_by_a_refusal(self, temp_home):
        """Asserts: an OAuth (browser login) slot whose stored credential
        matches the refused fingerprint is not struck; its refresh machinery
        owns its verdict."""
        switcher = _linux_switcher()
        creds = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "tok",
                    "refreshToken": "rtok",
                    "scopes": ["user:inference", "user:profile"],
                }
            }
        )
        switcher._write_account_credentials("1", "b@example.com", creds)
        data = switcher._get_sequence_data()
        data["accounts"]["1"] = {
            "email": "b@example.com",
            "uuid": "uuid-1",
            "organizationUuid": "",
            "organizationName": "",
            "added": "2024-01-01T00:00:00Z",
        }
        data["sequence"] = [1]
        switcher._write_json(switcher.sequence_file, data)
        fp = oauth.credential_fingerprint(creds)
        assert switcher.record_credential_refused("1", 401, fp) is False
        entry = switcher.usage_entries_by_account(fetch=set())["1"]
        assert entry.auth_dead_strikes == 0

    def test_an_unknown_slot_strikes_nothing(self, temp_home):
        """Asserts: a slot the roster does not hold strikes nothing."""
        switcher, fp = _token_switcher()
        assert switcher.record_credential_refused("9", 401, fp) is False

    def test_a_burst_of_refusals_strikes_and_warns_once(self, temp_home, caplog):
        """Asserts: repeated refusals of one credential strike once and log
        one WARNING."""
        switcher, fp = _token_switcher()
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            assert switcher.record_credential_refused("2", 401, fp) is True
            assert switcher.record_credential_refused("2", 403, fp) is False
        assert _warnings(caplog) == [WARNING_TEXT]

    def test_add_token_clears_the_strike(self, temp_home):
        """Asserts: `add-token` into the struck slot lifts the quarantine."""
        switcher, fp = _token_switcher()
        assert switcher.record_credential_refused("2", 401, fp) is True
        switcher.add_account_from_token("sk-ant-oat01-new", EMAIL, slot=2)
        entry = switcher.usage_entries_by_account(fetch=set())["2"]
        assert entry.auth_dead_strikes == 0
        assert entry.strike_source is None
        assert entry.sentinel is None


class TestStrikeSourceInTheStore:
    IDENT = {"1": ("a@example.com", "")}

    def test_an_endpoint_strike_after_a_refusal_drops_the_source(self, tmp_path):
        """Asserts: a refresh-endpoint strike overwrites the refusal's
        source, so the race doubt applies to it again."""
        store = UsageStore(tmp_path)
        assert store.strike_refused_credential("1", self.IDENT, "sha256-full:a")
        store.record(
            {"1": FetchRecord(error="invalid_grant", struck_fp="sha256:b")},
            self.IDENT,
        )
        assert store.entries(self.IDENT)["1"].strike_source is None

    def test_a_refusal_strike_is_not_doubted_as_a_race(self, tmp_path):
        """Asserts: a refusal landing after a recorded reading binds (the
        refresh race doubt does not excuse it), while the same timing on a
        refresh-endpoint strike is doubted."""
        store = UsageStore(tmp_path)
        store.record_header_reading("1", self.IDENT, HEADERS, header_only=True)
        store.strike_refused_credential("1", self.IDENT, "sha256-full:a")
        assert store.entries(self.IDENT)["1"].token_dead() is True
        refreshed = UsageStore(tmp_path / "b")
        refreshed.record_header_reading("1", self.IDENT, HEADERS, header_only=True)
        refreshed.record(
            {"1": FetchRecord(error="invalid_grant", struck_fp="sha256:b")},
            self.IDENT,
        )
        assert refreshed.entries(self.IDENT)["1"].token_dead() is False


class TestAutoswitchLeavesARefusedToken:
    @staticmethod
    def _harness(temp_home) -> EngineHarness:
        h = EngineHarness(temp_home, threshold=90.0, strategy="consume-first")
        for num, email in enumerate(("a", "b", "c"), 1):
            h.seed(num, f"{email}@example.com")
        h.make_live("a@example.com", 1)
        return h

    def test_a_struck_active_token_slot_is_left_on_the_next_tick(
        self, temp_home
    ):
        """Asserts: an active setup-token slot reading relogin_required is
        left on the first tick (failover), with no unhealthy-tick count."""
        h = self._harness(temp_home)
        now = h.clock.now
        entries = {
            "1": UsageEntry(sentinel=USAGE_RELOGIN_REQUIRED, header_only=True),
            "2": UsageEntry(last_good=_usage7(10.0, 10.0), fetched_at=now, age_s=0.0),
            "3": UsageEntry(last_good=_usage7(20.0, 20.0), fetched_at=now, age_s=0.0),
        }
        h.tick_with_entries(entries)
        assert h.active_number() in (2, 3)

    def test_a_struck_active_browser_login_keeps_the_unhealthy_count(
        self, temp_home
    ):
        """Asserts: the control: a browser-login active reading
        relogin_required still spends the unhealthy ticks before failover."""
        h = self._harness(temp_home)
        now = h.clock.now
        entries = {
            "1": UsageEntry(sentinel=USAGE_RELOGIN_REQUIRED),
            "2": UsageEntry(last_good=_usage7(10.0, 10.0), fetched_at=now, age_s=0.0),
            "3": UsageEntry(last_good=_usage7(20.0, 20.0), fetched_at=now, age_s=0.0),
        }
        assert h.tick_with_entries(entries) is TickOutcome.NO_ACTION
        assert h.active_number() == 1
