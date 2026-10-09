"""Tests for the `cswap config` subcommand (get/set/unset/list/path)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import cli


def _run(argv: list[str], capsys) -> tuple[int, str, str]:
    """Run `cswap config <argv>`; returns (exit_code, stdout, stderr).

    Success returns normally from main() (no sys.exit), errors raise
    SystemExit — normalize both to an exit code.
    """
    with patch("os.geteuid", return_value=1000, create=True), \
         patch.object(sys, "argv", ["claude-swap", "config", *argv]):
        code = 0
        try:
            cli.main()
        except SystemExit as e:
            code = e.code or 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _settings_file(capsys) -> Path:
    code, out, _ = _run(["path"], capsys)
    assert code == 0
    return Path(out.strip())


class TestConfigList:
    def test_lists_all_keys_as_defaults(self, temp_home, capsys):
        code, out, _ = _run([], capsys)
        assert code == 0
        for key in (
            "autoswitch.threshold",
            "autoswitch.intervalSeconds",
            "autoswitch.cooldownSeconds",
            "autoswitch.hysteresisPct",
            "autoswitch.strategy",
            "autoswitch.includeApiKeyAccounts",
            "autoswitch.unhealthyTicks",
            "autoswitch.model",
            "ui.theme",
        ):
            assert key in out
        # From the schema, not a literal: a bare count drifts every time a
        # setting is added or removed, and its failure names no key.
        from claude_swap.settings import SETTING_SPECS

        assert out.count("(default)") == len(SETTING_SPECS), (
            f"{len(SETTING_SPECS)} settings declared, "
            f"{out.count('(default)')} listed as default"
        )

    def test_set_key_not_marked_default(self, temp_home, capsys):
        _run(["set", "autoswitch.cooldownSeconds", "600"], capsys)
        code, out, _ = _run([], capsys)
        assert code == 0
        cooldown_line = next(
            ln for ln in out.splitlines() if "cooldownSeconds" in ln
        )
        assert "600" in cooldown_line
        assert "(default)" not in cooldown_line

    def test_set_equal_to_default_still_counts_as_set(self, temp_home, capsys):
        _run(["set", "autoswitch.threshold", "90"], capsys)
        _, out, _ = _run([], capsys)
        threshold_line = next(
            ln for ln in out.splitlines() if "threshold" in ln
        )
        assert "(default)" not in threshold_line

    def test_json_list(self, temp_home, capsys):
        code, out, _ = _run(["--json"], capsys)
        assert code == 0
        payload = json.loads(out)
        assert payload["schemaVersion"] == 1
        assert payload["path"].endswith("settings.json")
        from claude_swap.settings import SETTING_SPECS

        by_key = {entry["key"]: entry for entry in payload["settings"]}
        assert set(by_key) == set(SETTING_SPECS)
        assert by_key["autoswitch.threshold"]["value"] == 90.0
        assert by_key["autoswitch.threshold"]["isSet"] is False
        assert by_key["autoswitch.includeApiKeyAccounts"]["value"] is False


class TestConfigSetGet:
    def test_set_then_get(self, temp_home, capsys):
        code, out, _ = _run(["set", "autoswitch.threshold", "80"], capsys)
        assert code == 0
        assert "autoswitch.threshold = 80" in out
        code, out, _ = _run(["get", "autoswitch.threshold"], capsys)
        assert code == 0
        assert out.strip() == "80"

    def test_set_writes_only_that_key(self, temp_home, capsys):
        """The trap guard: no other defaults get materialized into the file."""
        _run(["set", "autoswitch.threshold", "80"], capsys)
        raw = json.loads(_settings_file(capsys).read_text())
        assert set(raw) == {"schemaVersion", "autoswitch"}
        assert set(raw["autoswitch"]) == {"threshold"}
        assert raw["autoswitch"]["threshold"] == 80.0

    def test_set_bool_words(self, temp_home, capsys):
        code, out, _ = _run(
            ["set", "autoswitch.includeApiKeyAccounts", "no"], capsys
        )
        assert code == 0
        assert "= false" in out
        raw = json.loads(_settings_file(capsys).read_text())
        assert raw["autoswitch"]["includeApiKeyAccounts"] is False

    def test_set_preserves_unknown_keys(self, temp_home, capsys):
        path = _settings_file(capsys)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schemaVersion": 1,
            "futureSection": {"x": 1},
            "autoswitch": {"threshold": 80, "futureKnob": True},
        }))
        code, _, _ = _run(["set", "autoswitch.threshold", "70"], capsys)
        assert code == 0
        raw = json.loads(path.read_text())
        assert raw["futureSection"] == {"x": 1}
        assert raw["autoswitch"]["futureKnob"] is True
        assert raw["autoswitch"]["threshold"] == 70.0

    def test_get_json_trailing_and_leading_flag(self, temp_home, capsys):
        _run(["set", "autoswitch.threshold", "80"], capsys)
        for argv in (
            ["get", "autoswitch.threshold", "--json"],
            ["--json", "get", "autoswitch.threshold"],
        ):
            code, out, _ = _run(argv, capsys)
            assert code == 0
            payload = json.loads(out)
            assert payload == {
                "schemaVersion": 1,
                "key": "autoswitch.threshold",
                "value": 80.0,
                "isSet": True,
            }


class TestConfigCreditThreshold:
    """`autoswitch.creditThreshold` through `cswap config` (X3587)."""

    def test_get_unset_reads_none(self, temp_home, capsys):
        """Asserts: an unset creditThreshold reads `(none)`, and its JSON
        value is null."""
        code, out, _ = _run(["get", "autoswitch.creditThreshold"], capsys)
        assert code == 0
        assert out.strip() == "(none)"
        code, out, _ = _run(
            ["get", "autoswitch.creditThreshold", "--json"], capsys
        )
        assert code == 0
        assert json.loads(out)["value"] is None

    def test_set_100_then_get(self, temp_home, capsys):
        """Asserts: `config set autoswitch.creditThreshold 100` succeeds and
        reads back 100, while the same 100 for threshold exits 1."""
        code, out, _ = _run(["set", "autoswitch.creditThreshold", "100"], capsys)
        assert code == 0
        assert "autoswitch.creditThreshold = 100" in out
        code, out, _ = _run(["get", "autoswitch.creditThreshold"], capsys)
        assert out.strip() == "100"
        code, _, err = _run(["set", "autoswitch.threshold", "100"], capsys)
        assert code == 1
        assert "between 50 and 99.9" in err


class TestConfigValidation:
    def test_out_of_range_exits_1(self, temp_home, capsys):
        code, _, err = _run(["set", "autoswitch.threshold", "30"], capsys)
        assert code == 1
        assert "between 50 and 99.9" in err

    def test_unknown_key_exits_1_and_lists_valid_keys(self, temp_home, capsys):
        code, _, err = _run(["set", "autoswitch.bogus", "1"], capsys)
        assert code == 1
        assert "unknown setting" in err
        assert "autoswitch.threshold" in err

    def test_bad_bool_exits_1(self, temp_home, capsys):
        code, _, err = _run(
            ["set", "autoswitch.includeApiKeyAccounts", "falsy"], capsys
        )
        assert code == 1
        assert "true or false" in err

    def test_bad_number_exits_1(self, temp_home, capsys):
        code, _, err = _run(["set", "autoswitch.threshold", "high"], capsys)
        assert code == 1
        assert "expects a number" in err

    def test_int_key_rejects_float(self, temp_home, capsys):
        code, _, err = _run(["set", "autoswitch.unhealthyTicks", "3.5"], capsys)
        assert code == 1
        assert "expects an integer" in err

    def test_bad_strategy_exits_1(self, temp_home, capsys):
        code, _, err = _run(["set", "autoswitch.strategy", "chaos"], capsys)
        assert code == 1
        assert "must be one of: best" in err

    def test_unknown_key_json_error_envelope(self, temp_home, capsys):
        code, out, _ = _run(["--json", "get", "autoswitch.bogus"], capsys)
        assert code == 1
        payload = json.loads(out)
        assert payload["schemaVersion"] == 1
        assert "unknown setting" in payload["error"]["message"]

    def test_corrupt_file_set_exits_1_and_leaves_file_untouched(
        self, temp_home, capsys
    ):
        path = _settings_file(capsys)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        code, _, err = _run(["set", "autoswitch.threshold", "80"], capsys)
        assert code == 1
        assert "not valid JSON" in err
        assert path.read_text() == "{not json"

    def test_missing_value_usage_error_exits_2(self, temp_home, capsys):
        code, _, _ = _run(["set", "autoswitch.threshold"], capsys)
        assert code == 2

    def test_unknown_action_exits_2(self, temp_home, capsys):
        code, _, _ = _run(["frobnicate"], capsys)
        assert code == 2

    def test_json_with_set_rejected(self, temp_home, capsys):
        code, _, _ = _run(
            ["--json", "set", "autoswitch.threshold", "80"], capsys
        )
        assert code == 2


class TestConfigUnset:
    def test_unset_restores_default(self, temp_home, capsys):
        _run(["set", "autoswitch.threshold", "80"], capsys)
        code, out, _ = _run(["unset", "autoswitch.threshold"], capsys)
        assert code == 0
        assert "default: 90" in out
        code, out, _ = _run(["get", "autoswitch.threshold"], capsys)
        assert out.strip() == "90"
        # The emptied autoswitch section is removed entirely.
        raw = json.loads(_settings_file(capsys).read_text())
        assert "autoswitch" not in raw

    def test_unset_when_not_set_is_a_noop(self, temp_home, capsys):
        code, _, err = _run(["unset", "autoswitch.threshold"], capsys)
        assert code == 0
        assert "not set" in err


class TestConfigMisc:
    def test_path_prints_settings_location(self, temp_home, capsys):
        code, out, _ = _run(["path"], capsys)
        assert code == 0
        assert out.strip().endswith("settings.json")

    def test_config_help(self, temp_home, capsys):
        code, out, _ = _run(["--help"], capsys)
        assert code == 0
        assert "autoswitch.threshold" in out
        assert "unset" in out

    def test_main_help_mentions_config(self, temp_home, capsys):
        with patch.object(sys, "argv", ["claude-swap", "--help"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 0
        assert "config" in capsys.readouterr().out

    def test_auto_picks_up_configured_threshold(self, temp_home, capsys):
        """End-to-end: a value set via config drives `cswap auto`."""
        _run(["set", "autoswitch.threshold", "77"], capsys)

        captured = {}

        class FakeEngine:
            def __init__(self, switcher, settings, on_event, *, dry_run=False,
                         state_path=None, clock=None):
                captured["settings"] = settings

            def tick(self):
                from claude_swap.autoswitch import TickOutcome

                return TickOutcome.NO_ACTION

        with patch("claude_swap.autoswitch.AutoSwitchEngine", FakeEngine), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["claude-swap", "auto", "--once"]):
            with pytest.raises(SystemExit):
                cli.main()
        assert captured["settings"].threshold == 77.0


class TestConfigCreditCaps:
    """`creditCaps.<email>`: a setup-token account's monthly usage-credit cap
    in US dollars (X3696)."""

    def test_set_get_list_and_unset(self, temp_home, capsys):
        """Asserts: a cap set for an email with dots is stored under the
        `creditCaps` section keyed by the whole email, read back by get
        and listed, and unset removes it and the empty section."""
        key = "creditCaps.satoshi@satoshi.report"
        code, out, _ = _run(["set", key, "500"], capsys)
        assert code == 0
        assert f"{key} = 500" in out
        raw = json.loads(_settings_file(capsys).read_text())
        assert raw["creditCaps"] == {"satoshi@satoshi.report": 500.0}
        code, out, _ = _run(["get", key], capsys)
        assert (code, out.strip()) == (0, "500")
        code, out, _ = _run(["get", key, "--json"], capsys)
        assert json.loads(out) == {
            "schemaVersion": 1, "key": key, "value": 500.0, "isSet": True,
        }
        code, out, _ = _run([], capsys)
        cap_line = next(ln for ln in out.splitlines() if key in ln)
        assert "500" in cap_line and "(default)" not in cap_line
        code, out, _ = _run(["--json"], capsys)
        by_key = {e["key"]: e for e in json.loads(out)["settings"]}
        assert by_key[key]["value"] == 500.0
        code, out, _ = _run(["unset", key], capsys)
        assert code == 0
        assert "no cap configured" in out
        raw = json.loads(_settings_file(capsys).read_text())
        assert "creditCaps" not in raw

    def test_get_unset_cap_reads_none(self, temp_home, capsys):
        """Asserts: an email with no cap reads `(none)`."""
        code, out, _ = _run(["get", "creditCaps.a@example.com"], capsys)
        assert (code, out.strip()) == (0, "(none)")

    @pytest.mark.parametrize("value", ["0", "-5", "abc", "inf", "nan"])
    def test_a_cap_must_be_a_positive_number(self, temp_home, capsys, value):
        """Asserts: zero, negative, non-numeric and non-finite caps are
        refused naming the key, and nothing is written."""
        key = "creditCaps.a@example.com"
        code, out, err = _run(["set", key, value], capsys)
        assert code == 1
        assert key in out + err
        assert not _settings_file(capsys).exists()

    def test_a_key_without_an_email_is_refused(self, temp_home, capsys):
        """Asserts: `creditCaps.` followed by no email is refused."""
        code, out, err = _run(["set", "creditCaps.nobody", "500"], capsys)
        assert code == 1
        assert "name the account's email" in out + err


def _setup_token_account_with_reading(email: str, share: str) -> None:
    """Add ``email`` as a setup-token account in slot 2 and record a reply
    whose overage headers say credits are allowed at ``share`` of the cap."""
    from claude_swap import usage_store
    from claude_swap.models import Platform
    from claude_swap.switcher import ClaudeAccountSwitcher

    switcher = ClaudeAccountSwitcher()
    switcher.platform = Platform.LINUX
    switcher._setup_directories()
    switcher._init_sequence_file()
    switcher.add_account_from_token("sk-ant-oat01-x", email, slot=2)
    assert switcher.record_usage_headers("2", {
        usage_store.USAGE_HEADER_5H_PCT: "0.3",
        usage_store.USAGE_HEADER_7D_PCT: "0.4",
        usage_store.USAGE_HEADER_OVERAGE_STATUS: "allowed",
        usage_store.USAGE_HEADER_OVERAGE_PCT: share,
    }) is True


class TestConfigCreditBalances:
    """`creditBalances.<email>`: the account's purchased usage-credit
    balance, entered with the account's current reading (X3711)."""

    def test_set_records_the_reading_then_get_list_and_unset(self, temp_home, capsys):
        """Asserts: with a $200 cap at 10% used ($20), `set
        creditBalances.<email> 70` stores {usd 70, enteredAt, usedAtEntry
        20, resetsAtEntry null}; get and list read it back (text and JSON);
        unset removes it and the empty section."""
        email = "support@lucidnews.org"
        _setup_token_account_with_reading(email, "0.1")
        assert _run(["set", f"creditCaps.{email}", "200"], capsys)[0] == 0
        key = f"creditBalances.{email}"
        code, out, err = _run(["set", key, "70"], capsys)
        assert code == 0, out + err
        assert f"{key} = 70 (entered " in out
        assert "$20.00 of the limit used then" in out
        raw = json.loads(_settings_file(capsys).read_text())
        entry = raw["creditBalances"][email]
        assert entry["usd"] == 70.0
        assert entry["usedAtEntry"] == pytest.approx(20.0)
        assert entry["resetsAtEntry"] is None
        assert entry["enteredAt"].endswith("Z")
        code, out, _ = _run(["get", key, "--json"], capsys)
        assert json.loads(out) == {
            "schemaVersion": 1, "key": key, "value": entry, "isSet": True,
        }
        code, out, _ = _run(["get", key], capsys)
        assert out.startswith("70 (entered ")
        code, out, _ = _run([], capsys)
        assert any(key in ln and "70 (entered" in ln for ln in out.splitlines())
        code, out, _ = _run(["--json"], capsys)
        by_key = {e["key"]: e for e in json.loads(out)["settings"]}
        assert by_key[key]["value"] == entry
        code, out, _ = _run(["unset", key], capsys)
        assert code == 0
        assert "no balance entered" in out
        raw = json.loads(_settings_file(capsys).read_text())
        assert "creditBalances" not in raw

    def test_set_with_no_dollar_reading_is_refused_naming_the_account(
        self, temp_home, capsys
    ):
        """Asserts: a setup-token account with no cap has no dollar reading,
        so entering its balance exits 1 naming the account and writes
        nothing."""
        email = "mahrens9175@gmail.com"
        _setup_token_account_with_reading(email, "0.0")
        code, out, err = _run(["set", f"creditBalances.{email}", "70"], capsys)
        assert code == 1
        assert email in out + err
        assert not _settings_file(capsys).exists()

    @pytest.mark.parametrize("value", ["-5", "abc", "inf", "nan"])
    def test_a_balance_must_be_a_non_negative_number(self, temp_home, capsys, value):
        """Asserts: negative, non-numeric and non-finite balances are refused
        naming the key, and nothing is written."""
        key = "creditBalances.a@example.com"
        code, out, err = _run(["set", key, value], capsys)
        assert code == 1
        assert key in out + err
        assert not _settings_file(capsys).exists()

    def test_get_unset_balance_reads_none(self, temp_home, capsys):
        """Asserts: an email with no balance reads `(none)`."""
        code, out, _ = _run(["get", "creditBalances.a@example.com"], capsys)
        assert (code, out.strip()) == (0, "(none)")
