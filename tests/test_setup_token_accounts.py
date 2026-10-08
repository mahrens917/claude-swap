"""Setup-token (``claude setup-token``) accounts.

Their scope cannot read the usage endpoint (403 every time), so they are
measured from the reply rate-limit headers alone; they carry no expiry of
their own, so ``add-token`` records when one was added and the login expiry
is a year on.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import autoswitch, oauth, usage_store
from claude_swap.exceptions import ValidationError
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.transfer import export_accounts, import_accounts
from claude_swap.usage_store import FetchRecord, UsageEntry, UsageStore

TOKEN_JSON = json.dumps(
    {"claudeAiOauth": {"accessToken": "sk-ant-oat01-x", "scopes": ["user:inference"]}}
)
BROWSER_JSON = json.dumps(
    {
        "claudeAiOauth": {
            "accessToken": "tok",
            "refreshToken": "rtok",
            "scopes": ["user:inference", "user:profile"],
        }
    }
)
IDENT = {"1": ("a@x.com", "")}
HEADERS = {
    usage_store.USAGE_HEADER_5H_PCT: "0.05",
    usage_store.USAGE_HEADER_7D_PCT: "0.30",
}
DAY = 86400.0


class FakeClock:
    def __init__(self, start: float = 1_800_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def store(tmp_path, clock):
    return UsageStore(tmp_path / "cache", clock=clock)


def _iso(ts: float) -> str:
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _linux_switcher() -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._init_sequence_file()
    return s


class _patched_home:
    """Redirect HOME/Path.home() to ``home`` for a second machine."""

    def __init__(self, home: Path):
        self.home = home
        self._patches: list = []

    def __enter__(self):
        self._patches = [
            patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)}),
            patch("pathlib.Path.home", return_value=self.home),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


# ---------------------------------------------------------------------------
# Identifying a setup-token credential
# ---------------------------------------------------------------------------


class TestIsSetupTokenCredential:
    def test_the_add_token_wrapper_is_a_setup_token(self):
        """Asserts: the credential `add-token` writes reads as a setup-token."""
        assert oauth.is_setup_token_credential(TOKEN_JSON) is True

    @pytest.mark.parametrize(
        "creds",
        [
            BROWSER_JSON,
            "sk-ant-api03-" + "a" * 40,
            "",
            None,
            "not json",
            json.dumps({"claudeAiOauth": {"accessToken": "t", "scopes": ["user:profile"]}}),
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "t",
                        "refreshToken": "r",
                        "scopes": ["user:inference"],
                    }
                }
            ),
            json.dumps({"claudeAiOauth": {"accessToken": "t"}}),
        ],
        ids=[
            "browser-login", "api-key", "empty", "none", "garbled",
            "other-scope", "has-refresh-token", "no-scopes",
        ],
    )
    def test_nothing_else_is(self, creds):
        """Asserts: a browser login, an API key or a malformed blob is never
        taken for a setup-token, so OAuth accounts keep endpoint fetching."""
        assert oauth.is_setup_token_credential(creds) is False


# ---------------------------------------------------------------------------
# The usage store: header readings and the header-only decision value
# ---------------------------------------------------------------------------


class TestHeaderOnlyReading:
    def test_a_403_row_refuses_an_ordinary_header_reading(self, store):
        """Asserts: a browser-login row failed with a non-429 error still
        refuses a header reading (the existing OAuth behaviour)."""
        store.record({"1": FetchRecord(error="http-403")}, IDENT)
        assert store.record_header_reading("1", IDENT, HEADERS) is False

    def test_a_403_row_takes_a_setup_token_reading_and_drops_the_failure(
        self, store, clock
    ):
        """Asserts: for a setup-token row the reply headers are the only
        measurement, so a 403 or 429 left by an endpoint ask never refuses
        them, and the reading clears that failure and the poll plan."""
        store.record({"1": FetchRecord(error="http-403")}, IDENT)
        store.record(
            {"1": FetchRecord(error="http-429", retry_after_s=3600.0)}, IDENT
        )
        clock.advance(10)
        assert (
            store.record_header_reading("1", IDENT, HEADERS, header_only=True)
            is True
        )
        entry = store.entries(IDENT)["1"]
        assert entry.last_good["five_hour"]["pct"] == pytest.approx(5.0)
        assert entry.last_good["seven_day"]["pct"] == pytest.approx(30.0)
        assert entry.fetched_at == clock.now
        assert entry.consecutive_failures == 0
        assert entry.last_error is None
        assert entry.backoff_until is None
        assert entry.next_poll_at is None

    def test_an_old_reading_is_still_decision_trusted(self, store, clock):
        """Asserts: an idle setup-token account's last reading stays its
        decision value days later (nothing but use can refresh it)."""
        store.record_header_reading("1", IDENT, HEADERS, header_only=True)
        clock.advance(3 * DAY)
        plain = store.entries(IDENT)["1"]
        assert plain.decision_value() is None  # an OAuth row this old is unknown
        value = usage_store.with_header_only(plain).decision_value()
        assert value == {
            "five_hour": {"pct": pytest.approx(5.0)},
            "seven_day": {"pct": pytest.approx(30.0)},
        }

    def test_a_window_reset_since_the_reading_reads_zero_with_no_reset(
        self, store, clock
    ):
        """Asserts: a 5h or 7d window whose reset passed since the reading
        reads 0% with no reset, and a window still running keeps its figure."""
        headers = {
            **HEADERS,
            usage_store.USAGE_HEADER_5H_RESET: str(clock.now + 3600),
            usage_store.USAGE_HEADER_7D_RESET: str(clock.now + 5 * DAY),
        }
        store.record_header_reading("1", IDENT, headers, header_only=True)
        clock.advance(2 * 3600)
        value = usage_store.with_header_only(store.entries(IDENT)["1"]).decision_value()
        assert value["five_hour"] == {"pct": 0.0}
        assert value["seven_day"]["pct"] == pytest.approx(30.0)
        assert "resets_at" in value["seven_day"]

    def test_a_per_model_window_is_unmeasured_and_never_gates(self, store):
        """Asserts: a scoped (per-model) figure is dropped from a
        setup-token decision value, so `autoswitch.model Fable` reads only
        the 5h/7d windows for it."""
        store.record(
            {"1": FetchRecord(usage={"scoped": [{"name": "Fable", "pct": 100.0}]})},
            IDENT,
        )
        store.record_header_reading("1", IDENT, HEADERS, header_only=True)
        value = usage_store.with_header_only(store.entries(IDENT)["1"]).decision_value()
        assert "scoped" not in value
        assert oauth.account_headroom(value, ("Fable",)) == pytest.approx(70.0)

    def test_no_reading_is_unknown(self, store):
        """Asserts: a setup-token account never measured has no value."""
        entry = usage_store.with_header_only(store.entries(IDENT)["1"])
        assert entry.decision_value() is None

    def test_due_candidate_never_picks_a_setup_token_row(self, store, clock):
        """Asserts: the scheduler never chooses a setup-token row to fetch."""
        entry = usage_store.with_header_only(store.entries(IDENT)["1"])
        assert usage_store.due_candidate(["1"], {"1": entry}, clock.now) is None


# ---------------------------------------------------------------------------
# The autoswitch admission gates
# ---------------------------------------------------------------------------


class TestAutoswitchGates:
    @staticmethod
    def _old_failed_entry(header_only: bool) -> UsageEntry:
        now = 1_800_000_000.0
        return UsageEntry(
            last_good={"five_hour": {"pct": 5.0}, "seven_day": {"pct": 3.0}},
            fetched_at=now - 2 * DAY,
            age_s=2 * DAY,
            consecutive_failures=3,
            last_error="http-429",
            backoff_until=now + 3600,
            header_only=header_only,
        )

    def test_consume_first_does_not_hold_on_an_idle_token_account(self):
        """Asserts: the consume-first stale-usage hold is not taken for a
        setup-token candidate whose reading is old (it can never be fresher
        until it is used), while an OAuth row of the same age still holds."""
        now = 1_800_000_000.0
        assert autoswitch.candidate_usage_is_stale(self._old_failed_entry(True), now) is False
        assert autoswitch.candidate_usage_is_stale(self._old_failed_entry(False), now) is True

    def test_proactive_trusts_a_token_account_with_old_endpoint_failures(self):
        """Asserts: endpoint failures recorded on a setup-token row do not
        make it untrustworthy for the proactive bar."""
        now = 1_800_000_000.0
        assert autoswitch.candidate_is_untrustworthy(self._old_failed_entry(True), now) is False
        assert autoswitch.candidate_is_untrustworthy(self._old_failed_entry(False), now) is True

    def test_a_token_account_may_be_the_probe_target(self):
        """Asserts: a setup-token account with an old reading is admissible
        as the probe target that learns its weekly reset."""
        now = 1_800_000_000.0
        entries = {"2": self._old_failed_entry(True)}
        assert autoswitch._probe_source_fresh(entries, "2", now) is True


# ---------------------------------------------------------------------------
# The switcher: never fetched, headers recorded, login expiry
# ---------------------------------------------------------------------------


class TestSwitcherSetupTokenAccounts:
    def test_collector_never_fetches_a_setup_token_account(self, temp_home):
        """Asserts: the usage collector never reserves or fetches a
        setup-token slot, and its entry is marked header-only."""
        switcher = _linux_switcher()
        switcher.add_account_from_token("sk-ant-oat01-x", "tok@example.com", slot=2)
        with patch.object(switcher, "_run_usage_fetches", return_value={}) as fetches:
            entries = switcher.usage_entries_by_account()
        for call in fetches.call_args_list:
            assert all(str(info[0]) != "2" for info in call.args[0])
        assert entries["2"].header_only is True

    def test_reply_headers_are_recorded_over_an_endpoint_403(self, temp_home):
        """Asserts: the owner proxy's header reading lands on a setup-token
        slot whose row carries the endpoint's 403 (the box's defect)."""
        switcher = _linux_switcher()
        switcher.add_account_from_token("sk-ant-oat01-x", "tok@example.com", slot=2)
        ident = {"2": ("tok@example.com", "")}
        switcher._usage_store.record({"2": FetchRecord(error="http-403")}, ident)
        assert switcher.record_usage_headers("2", HEADERS) is True
        entry = switcher.usage_entries_by_account(fetch=set())["2"]
        assert entry.last_error is None
        assert entry.decision_value()["seven_day"]["pct"] == pytest.approx(30.0)

    def test_add_token_records_the_add_time_and_a_year_of_login(self, temp_home):
        """Asserts: `add-token` stamps `tokenAddedAt` on a setup-token and
        `list --json` emits `loginExpiresAt` 365 days on with `loginKind`."""
        switcher = _linux_switcher()
        switcher.add_account_from_token("sk-ant-oat01-x", "tok@example.com", slot=2)
        record = switcher._get_sequence_data()["accounts"]["2"]
        added = datetime.strptime(record["tokenAddedAt"], "%Y-%m-%dT%H:%M:%SZ")
        added_ts = added.replace(tzinfo=timezone.utc).timestamp()
        payload = switcher.list_accounts(json_output=True, read_only=True)
        row = next(r for r in payload["accounts"] if r["number"] == 2)
        assert row["loginKind"] == "setup-token"
        assert row["loginExpiresAt"] == _iso(added_ts + 365 * DAY)

    def test_api_key_rows_name_their_kind_and_carry_no_expiry(self, temp_home):
        """Asserts: an API key gets no token stamp and reads `api-key`."""
        switcher = _linux_switcher()
        switcher.add_account_from_token("sk-ant-api03-" + "a" * 40, slot=1)
        assert "tokenAddedAt" not in switcher._get_sequence_data()["accounts"]["1"]
        payload = switcher.list_accounts(json_output=True, read_only=True)
        row = payload["accounts"][0]
        assert row["loginKind"] == "api-key"
        assert "loginExpiresAt" not in row

    def test_a_token_stored_before_the_stamp_has_no_expiry_until_stamped(
        self, temp_home, capsys
    ):
        """Asserts: `cswap token-added` stamps the day on a token slot that
        lacks it, and the login expiry is that day plus 365 days."""
        switcher = _linux_switcher()
        switcher.add_account_from_token("sk-ant-oat01-x", "tok@example.com", slot=3)
        data = switcher._get_sequence_data()
        del data["accounts"]["3"]["tokenAddedAt"]
        switcher._write_json(switcher.sequence_file, data)
        row = switcher.list_accounts(json_output=True, read_only=True)["accounts"][0]
        assert "loginExpiresAt" not in row

        switcher.stamp_token_added("tok@example.com", "2026-10-07")
        row = switcher.list_accounts(json_output=True, read_only=True)["accounts"][0]
        assert row["loginExpiresAt"] == "2027-10-07T00:00:00Z"
        assert "2027-10-07" in capsys.readouterr().out

    def test_token_added_refuses_a_browser_login_and_a_bad_day(self, temp_home):
        """Asserts: the stamp is refused on a slot that is not a setup-token
        and on a day that is not YYYY-MM-DD."""
        switcher = _linux_switcher()
        switcher.add_account_from_token("sk-ant-api03-" + "a" * 40, slot=1)
        with pytest.raises(ValidationError, match="not a setup-token"):
            switcher.stamp_token_added("1", "2026-10-07")
        with pytest.raises(ValidationError, match="YYYY-MM-DD"):
            switcher.stamp_token_added("1", "10/07/2026")

    def test_human_list_shows_the_token_login_countdown(self, temp_home, capsys):
        """Asserts: the human `cswap list` login line counts down a token's
        year as it does a browser login's expiry."""
        switcher = _linux_switcher()
        switcher.add_account_from_token("sk-ant-oat01-x", "tok@example.com", slot=2)
        capsys.readouterr()
        switcher.list_accounts(read_only=True)
        out = capsys.readouterr().out
        assert "login" in out
        assert "364d" in out or "365d" in out

    def test_export_and_import_carry_the_token_add_time(self, tmp_path):
        """Asserts: `tokenAddedAt` survives `cswap export` then `cswap
        import` onto another machine, so the restored store keeps the
        token's expiry (unlike `added`, which import restamps)."""
        src_home = tmp_path / "src"
        (src_home / ".claude").mkdir(parents=True)
        out = tmp_path / "b.cswap"
        with _patched_home(src_home):
            src = _linux_switcher()
            src.add_account_from_token("sk-ant-oat01-x", "tok@example.com", slot=2)
            src.stamp_token_added("2", "2026-10-07")
            export_accounts(src, str(out))
        assert json.loads(out.read_text())["accounts"][0]["tokenAddedAt"] == (
            "2026-10-07T00:00:00Z"
        )
        dst_home = tmp_path / "dst"
        (dst_home / ".claude").mkdir(parents=True)
        with _patched_home(dst_home):
            dst = _linux_switcher()
            import_accounts(dst, str(out))
            assert dst._get_sequence_data()["accounts"]["2"]["tokenAddedAt"] == (
                "2026-10-07T00:00:00Z"
            )
            row = dst.list_accounts(json_output=True, read_only=True)["accounts"][0]
            assert row["loginExpiresAt"] == "2027-10-07T00:00:00Z"
            assert row["loginKind"] == "setup-token"


# ---------------------------------------------------------------------------
# A refused token is charged to the slot that owns it
# ---------------------------------------------------------------------------


class TestRecordTokenRefused:
    """`record_token_refused` is the owner proxy's entry point: it charges a
    `/v1/messages` refusal to the slot whose stored credential carries the
    request's own token, never to whichever slot is live when the proxy's
    worker runs (a switch in between moved the live login and the refusal
    was dropped)."""

    def _two_slots(self, temp_home):
        """Slot 1 a live browser login, slot 2 a setup-token not live."""
        s = _linux_switcher()
        s._write_account_credentials("1", "a@x.com", BROWSER_JSON)
        s._write_account_config(
            "1", "a@x.com",
            json.dumps({"oauthAccount": {"emailAddress": "a@x.com", "accountUuid": "u1"}}),
        )
        data = s._get_sequence_data()
        data["accounts"]["1"] = {
            "email": "a@x.com", "uuid": "u1", "organizationUuid": "",
            "organizationName": "", "added": "2024-01-01T00:00:00Z",
        }
        data["sequence"] = [1]
        data["activeAccountNumber"] = 1
        s._write_json(s.sequence_file, data)
        s.add_account_from_token("sk-ant-oat01-x", "tok@x.com", slot=2)
        (temp_home / ".claude" / ".credentials.json").write_text(BROWSER_JSON)
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "a@x.com", "accountUuid": "u1"},
        }))
        assert s.current_account_number() == "1"  # premise: slot 2 not live
        return s

    def _strikes(self, s, num, email):
        return s._usage_store.entries({num: (email, "")})[num].auth_dead_strikes

    def test_a_refused_token_on_a_slot_not_live_strikes_that_slot(self, temp_home):
        """Asserts: a refusal of slot 2's setup-token while slot 1 is live
        strikes slot 2 and leaves slot 1 alone."""
        s = self._two_slots(temp_home)
        assert s.record_token_refused("sk-ant-oat01-x", 401) is True
        assert self._strikes(s, "2", "tok@x.com") > 0
        assert self._strikes(s, "1", "a@x.com") == 0

    def test_a_token_no_slot_carries_strikes_nothing(self, temp_home):
        """Asserts: a refusal of a token no stored credential carries returns
        False and strikes no slot."""
        s = self._two_slots(temp_home)
        assert s.record_token_refused("sk-ant-oat01-gone", 401) is False
        assert self._strikes(s, "2", "tok@x.com") == 0
        assert self._strikes(s, "1", "a@x.com") == 0

    def test_a_slot_that_left_the_roster_is_logged_at_warning(
        self, temp_home, caplog
    ):
        """Asserts: when the slot owning the token leaves the roster between
        the lookup and the strike, the call returns False, strikes nothing,
        and logs one WARNING naming the slot and the reason, never the
        token."""
        import logging

        s = self._two_slots(temp_home)
        with patch.object(s, "_slot_identity", return_value=None), \
                caplog.at_level(logging.WARNING):
            assert s.record_token_refused("sk-ant-oat01-x", 401) is False
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        text = warnings[0].getMessage()
        assert "account 2" in text and "left the roster" in text
        assert "sk-ant-oat01-x" not in text
        assert self._strikes(s, "2", "tok@x.com") == 0

    def test_a_token_only_the_previous_copy_holds_is_logged_at_warning(
        self, temp_home, caplog
    ):
        """Asserts: a token that maps to slot 2 only through its retained
        previous copy strikes nothing and logs one WARNING naming the slot
        and that reason, never the token."""
        import logging

        s = self._two_slots(temp_home)
        with patch.object(s, "slot_for_access_token", return_value="2"), \
                caplog.at_level(logging.WARNING):
            assert s.record_token_refused("sk-ant-oat01-old", 401) is False
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        text = warnings[0].getMessage()
        assert "account 2" in text and "previous copy" in text
        assert "sk-ant-oat01-old" not in text
        assert self._strikes(s, "2", "tok@x.com") == 0

    def test_an_unreadable_current_copy_is_logged_at_warning(
        self, temp_home, caplog
    ):
        """Asserts: when slot 2's saved credential cannot be read, the
        refusal strikes nothing and the WARNING names the slot and says the
        credential could not be read."""
        import logging

        s = self._two_slots(temp_home)
        with patch.object(
            s, "_read_account_credentials_ex", return_value=(None, True)
        ), caplog.at_level(logging.WARNING):
            assert s.record_token_refused("sk-ant-oat01-x", 401) is False
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "account 2" in warnings[0].getMessage()
        assert "could not be read" in warnings[0].getMessage()

    def test_a_browser_login_token_is_never_struck(self, temp_home):
        """Asserts: a refusal of the live browser login's token strikes
        nothing; its refresh machinery owns its verdict."""
        s = self._two_slots(temp_home)
        assert s.record_token_refused("tok", 401) is False
        assert self._strikes(s, "1", "a@x.com") == 0
