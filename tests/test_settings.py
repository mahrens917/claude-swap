"""Tests for settings.json load/save/merge (settings.py)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import stat
import sys
from pathlib import Path

import pytest

from claude_swap.exceptions import ConfigError
from claude_swap.settings import (
    SETTING_SPECS,
    atomic_write_json,
    AutoSwitchSettings,
    UiSettings,
    account_switch_point,
    effective_settings,
    load_settings,
    load_ui_settings,
    merged_with_cli,
    save_settings,
    set_setting,
    settings_path,
    unset_setting,
)
from claude_swap.usage_store import UsageEntry


def _args(**kwargs) -> argparse.Namespace:
    defaults = {
        "threshold": None,
        "interval": None,
        "cooldown": None,
        "include_api_key_accounts": None,
        "strategy": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class TestLoadSettings:
    def test_missing_file_gives_defaults(self, tmp_path: Path):
        assert load_settings(tmp_path) == AutoSwitchSettings()

    def test_corrupt_file_raises_naming_it(self, tmp_path: Path):
        """Asserts: a settings file that is not JSON raises ConfigError
        naming the file, never reads as defaults."""
        path = settings_path(tmp_path)
        path.write_text("{not json")
        with pytest.raises(ConfigError, match=f"{path} is not valid JSON"):
            load_settings(tmp_path)
        with pytest.raises(ConfigError, match="not valid JSON"):
            load_ui_settings(tmp_path)

    def test_non_object_raises_naming_it(self, tmp_path: Path):
        """Asserts: a settings file holding a JSON array raises ConfigError
        naming the file."""
        path = settings_path(tmp_path)
        path.write_text("[1, 2]")
        with pytest.raises(ConfigError, match=f"{path} is not a JSON object"):
            load_settings(tmp_path)

    def test_non_object_section_raises_naming_it(self, tmp_path: Path):
        """Asserts: an `autoswitch` section that is not an object raises
        ConfigError naming the file and the section."""
        path = settings_path(tmp_path)
        path.write_text(json.dumps({"autoswitch": [90]}))
        with pytest.raises(ConfigError, match=f"{path}: autoswitch is"):
            load_settings(tmp_path)

    def test_an_absent_section_reads_as_defaults(self, tmp_path: Path):
        """Asserts: a file with only a `ui` section (what `config set
        ui.theme` writes) reads every autoswitch key at its default."""
        settings_path(tmp_path).write_text(json.dumps({"ui": {"theme": "light"}}))
        assert load_settings(tmp_path) == AutoSwitchSettings()

    def test_partial_section_fills_defaults(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"schemaVersion": 1, "autoswitch": {"threshold": 80}})
        )
        loaded = load_settings(tmp_path)
        assert loaded.threshold == 80.0
        assert loaded.interval_seconds == AutoSwitchSettings().interval_seconds

    @pytest.mark.parametrize("key,value,why", [
        ("threshold", 200, "outside 50 to 99.9"),
        ("intervalSeconds", 1, "outside"),
        ("unhealthyTicks", 0, "outside"),
        ("threshold", "high", "is not a number"),
        ("includeApiKeyAccounts", 1, "is not true or false"),
        ("unhealthyTicks", 2.5, "is not an integer"),
        ("strategy", "chaos", "is not one of"),
        ("model", 123, "is not a non-empty string"),
        ("model", "", "is not a non-empty string"),
    ])
    def test_a_value_config_set_refuses_raises_naming_the_key(
        self, tmp_path: Path, key, value, why
    ):
        """Asserts: a stored value `cswap config set` would refuse (out of
        range, wrong type, unsupported choice) raises ConfigError naming the
        file and the key, never a clamp or a default."""
        path = settings_path(tmp_path)
        path.write_text(json.dumps({"autoswitch": {key: value}}))
        with pytest.raises(ConfigError) as caught:
            load_settings(tmp_path)
        message = str(caught.value)
        assert f"{path}: autoswitch.{key} = {value!r}" in message
        assert why in message

    def test_consume_first_is_a_valid_strategy(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"strategy": "consume-first"}})
        )
        assert load_settings(tmp_path).strategy == "consume-first"

    def test_set_strategy_consume_first(self, tmp_path: Path):
        set_setting(tmp_path, "autoswitch.strategy", "consume-first")
        assert load_settings(tmp_path).strategy == "consume-first"

    def test_dynamic_is_a_valid_strategy(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"strategy": "dynamic"}})
        )
        assert load_settings(tmp_path).strategy == "dynamic"

    def test_set_strategy_dynamic(self, tmp_path: Path):
        set_setting(tmp_path, "autoswitch.strategy", "dynamic")
        assert load_settings(tmp_path).strategy == "dynamic"


class TestSaveSettings:
    def test_roundtrip(self, tmp_path: Path):
        custom = AutoSwitchSettings(threshold=85.0, cooldown_seconds=60.0)
        save_settings(tmp_path, custom)
        assert load_settings(tmp_path) == custom

    def test_unknown_keys_survive(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({
            "schemaVersion": 1,
            "futureSection": {"x": 1},
            "autoswitch": {"threshold": 80, "futureKnob": True},
        }))
        save_settings(tmp_path, AutoSwitchSettings(threshold=70.0))
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw["futureSection"] == {"x": 1}
        assert raw["autoswitch"]["futureKnob"] is True
        assert raw["autoswitch"]["threshold"] == 70.0

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_file_mode_is_0600(self, tmp_path: Path):
        save_settings(tmp_path, AutoSwitchSettings())
        mode = stat.S_IMODE(settings_path(tmp_path).stat().st_mode)
        assert mode == 0o600

    def test_overwrite_backs_up_the_old_file_to_prev(self, tmp_path: Path):
        save_settings(tmp_path, AutoSwitchSettings(threshold=70.0))
        old_bytes = settings_path(tmp_path).read_bytes()

        save_settings(tmp_path, AutoSwitchSettings(threshold=85.0))

        prev = settings_path(tmp_path).with_name("settings.json.prev")
        assert prev.read_bytes() == old_bytes
        assert load_settings(tmp_path).threshold == 85.0

    def test_first_save_writes_no_backup(self, tmp_path: Path):
        save_settings(tmp_path, AutoSwitchSettings())
        prev = settings_path(tmp_path).with_name("settings.json.prev")
        assert not prev.exists()

    def test_identical_second_save_leaves_prev_unchanged(self, tmp_path: Path):
        # A repeated identical save must not overwrite a real `.prev` with
        # a duplicate of the bytes already on disk.
        save_settings(tmp_path, AutoSwitchSettings(threshold=70.0))
        save_settings(tmp_path, AutoSwitchSettings(threshold=85.0))
        prev = settings_path(tmp_path).with_name("settings.json.prev")
        first_prev_bytes = prev.read_bytes()

        save_settings(tmp_path, AutoSwitchSettings(threshold=85.0))

        assert prev.read_bytes() == first_prev_bytes

    def test_backup_failure_does_not_block_the_save(self, tmp_path: Path, monkeypatch, caplog):
        save_settings(tmp_path, AutoSwitchSettings(threshold=70.0))
        real_replace = os.replace

        def _raise_for_prev(src, dst):
            if str(dst).endswith(".prev"):
                raise OSError("disk full")
            return real_replace(src, dst)

        monkeypatch.setattr(os, "replace", _raise_for_prev)
        with caplog.at_level(logging.WARNING):
            save_settings(tmp_path, AutoSwitchSettings(threshold=85.0))

        assert "back up" in caplog.text.lower()
        prev = settings_path(tmp_path).with_name("settings.json.prev")
        assert not prev.exists()
        assert load_settings(tmp_path).threshold == 85.0

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_prev_mode_is_0600(self, tmp_path: Path):
        save_settings(tmp_path, AutoSwitchSettings(threshold=70.0))
        save_settings(tmp_path, AutoSwitchSettings(threshold=85.0))
        prev = settings_path(tmp_path).with_name("settings.json.prev")
        assert stat.S_IMODE(prev.stat().st_mode) == 0o600

    def test_non_settings_file_gets_no_prev_backup(self, tmp_path: Path):
        other = tmp_path / "state.json"
        atomic_write_json(other, {"a": 1})
        atomic_write_json(other, {"a": 2})
        assert not other.with_name("state.json.prev").exists()

    def test_symlinked_settings_prev_sits_beside_the_link(self, tmp_path: Path):
        repo = tmp_path / "repo"; repo.mkdir()
        live = tmp_path / "live"; live.mkdir()
        tracked = repo / "settings.json"
        tracked.write_text(json.dumps({"autoswitch": {"threshold": 70.0}}))
        old_bytes = tracked.read_bytes()
        link = live / "settings.json"
        link.symlink_to(tracked)

        atomic_write_json(link, {"autoswitch": {"threshold": 85.0}})

        prev = live / "settings.json.prev"
        assert prev.exists()
        assert prev.read_bytes() == old_bytes
        assert not (repo / "settings.json.prev").exists()


class TestReadRawReturnsDict:
    """Regression: callers outside this module expect a dict back."""

    def test_read_raw_returns_a_dict(self, tmp_path: Path):
        from claude_swap.settings import _read_raw

        settings_path(tmp_path).write_text(
            json.dumps({"remoteControl": {"pinned": True}})
        )
        raw = _read_raw(settings_path(tmp_path))
        assert isinstance(raw, dict)
        assert raw["remoteControl"]["pinned"] is True

    def test_the_write_read_returns_a_dict(self, tmp_path: Path):
        """Asserts: the one reader in its read-modify-write mode returns the
        same dict."""
        from claude_swap.settings import _read_raw

        settings_path(tmp_path).write_text(
            json.dumps({"remoteControl": {"debugSlowMs": 5}})
        )
        raw = _read_raw(settings_path(tmp_path), for_write=True)
        assert raw == {"remoteControl": {"debugSlowMs": 5}}

    @pytest.mark.parametrize("body,why", [
        ("{not json", "is not valid JSON"),
        ("[1]", "is not a JSON object"),
    ], ids=["not-json", "not-object"])
    def test_one_reader_names_the_remedy_only_for_a_write(
        self, tmp_path: Path, body, why
    ):
        """Asserts: `_read_raw` is the one strict reader: a corrupt file
        raises ConfigError naming it in both modes, and only the
        read-modify-write mode adds the fix-or-delete remedy; the separate
        write-side reader is gone."""
        from claude_swap import settings as s

        path = settings_path(tmp_path)
        path.write_text(body)
        with pytest.raises(ConfigError, match=why) as read_err:
            s._read_raw(path)
        with pytest.raises(ConfigError, match=why) as write_err:
            s._read_raw(path, for_write=True)
        assert str(path) in str(read_err.value)
        assert "before changing settings" not in str(read_err.value)
        assert str(write_err.value).endswith(
            "fix or delete it before changing settings"
        )
        assert not hasattr(s, "_read_raw_for_write")


class TestUiSettings:
    def test_missing_file_defaults_to_auto(self, tmp_path: Path):
        assert load_ui_settings(tmp_path) == UiSettings(theme="auto")

    def test_reads_auto(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({"ui": {"theme": "auto"}}))
        assert load_ui_settings(tmp_path).theme == "auto"

    def test_reads_light(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({"ui": {"theme": "light"}}))
        assert load_ui_settings(tmp_path).theme == "light"

    def test_unknown_theme_raises_naming_the_key(self, tmp_path: Path):
        """Asserts: an unsupported ui.theme raises ConfigError naming the
        file and the key."""
        path = settings_path(tmp_path)
        path.write_text(json.dumps({"ui": {"theme": "purple"}}))
        with pytest.raises(ConfigError, match=f"{path}: ui.theme = 'purple'"):
            load_ui_settings(tmp_path)

    def test_set_and_unset_ui_theme(self, tmp_path: Path):
        assert set_setting(tmp_path, "ui.theme", "light") == "light"
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw == {"schemaVersion": 1, "ui": {"theme": "light"}}
        assert unset_setting(tmp_path, "ui.theme") is True
        assert "ui" not in json.loads(settings_path(tmp_path).read_text())

    def test_set_rejects_bad_choice(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="dark, light"):
            set_setting(tmp_path, "ui.theme", "purple")


class TestSettingSpecs:
    def test_registry_covers_every_dataclass_field(self):
        by_section: dict[str, set[str]] = {}
        for spec in SETTING_SPECS.values():
            by_section.setdefault(spec.section, set()).add(spec.field)
        assert by_section["autoswitch"] == {
            f.name for f in AutoSwitchSettings.__dataclass_fields__.values()
        }
        assert by_section["ui"] == {
            f.name for f in UiSettings.__dataclass_fields__.values()
        }

    def test_defaults_match_dataclass(self):
        sources = {"autoswitch": AutoSwitchSettings(), "ui": UiSettings()}
        for spec in SETTING_SPECS.values():
            assert spec.default == getattr(sources[spec.section], spec.field)


class TestAStaleBuildWritesNoSettings:
    """X3697: settings.json is written by `cswap config`, `cswap auto`, the
    menu bar and the owner proxy, so a process whose loaded build is no
    longer the installed one writes none of it."""

    def test_set_setting_and_save_settings_are_refused(
        self, tmp_path: Path, stale_build
    ):
        """Asserts: both settings writers raise StaleBuildWriteError, the
        file keeps its bytes, and no ``.prev`` copy is written either."""
        from claude_swap.locking import StaleBuildWriteError

        path = settings_path(tmp_path)
        path.write_text(json.dumps({"schemaVersion": 1,
                                    "autoswitch": {"threshold": 80.0}}))
        before = path.read_bytes()
        with pytest.raises(StaleBuildWriteError):
            set_setting(tmp_path, "autoswitch.threshold", "90")
        with pytest.raises(StaleBuildWriteError):
            save_settings(tmp_path, AutoSwitchSettings(threshold=70.0))
        assert path.read_bytes() == before
        assert not path.with_name(path.name + ".prev").exists()

    def test_the_installed_build_still_writes(self, tmp_path: Path):
        """Asserts: the process's own build writes settings as before."""
        assert set_setting(tmp_path, "autoswitch.threshold", "90") == 90.0


class TestSetUnsetSetting:
    def test_set_writes_minimal_file(self, tmp_path: Path):
        value = set_setting(tmp_path, "autoswitch.threshold", "80")
        assert value == 80.0
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw == {"schemaVersion": 1, "autoswitch": {"threshold": 80.0}}

    def test_set_int_kind_coerces_and_rejects_floats(self, tmp_path: Path):
        assert set_setting(tmp_path, "autoswitch.unhealthyTicks", "5") == 5
        with pytest.raises(ConfigError, match="integer"):
            set_setting(tmp_path, "autoswitch.unhealthyTicks", "3.5")

    def test_set_rejects_out_of_range_without_writing(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="between 50 and 99.9"):
            set_setting(tmp_path, "autoswitch.threshold", "200")
        assert not settings_path(tmp_path).exists()

    def test_set_rejects_unknown_key(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="unknown setting"):
            set_setting(tmp_path, "autoswitch.bogus", "1")

    def test_set_string_kind_round_trips(self, tmp_path: Path):
        assert set_setting(tmp_path, "autoswitch.model", "Fable") == "Fable"
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw["autoswitch"]["model"] == "Fable"
        assert load_settings(tmp_path).model == "Fable"

    def test_set_string_kind_rejects_empty(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="unset"):
            set_setting(tmp_path, "autoswitch.model", "   ")
        assert not settings_path(tmp_path).exists()

    def test_a_null_model_reads_as_unset(self, tmp_path: Path):
        """Asserts: an explicit JSON null on `model` (default None) is the
        documented unset value, not a rejection."""
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"model": None}})
        )
        assert load_settings(tmp_path).model is None

    def test_set_rejects_bool_words_strictly(self, tmp_path: Path):
        assert set_setting(tmp_path, "autoswitch.includeApiKeyAccounts", "FALSE") is False
        with pytest.raises(ConfigError, match="true or false"):
            set_setting(tmp_path, "autoswitch.includeApiKeyAccounts", "falsy")

    def test_set_on_corrupt_file_raises_and_preserves_it(self, tmp_path: Path):
        settings_path(tmp_path).write_text("{not json")
        with pytest.raises(ConfigError, match="not valid JSON"):
            set_setting(tmp_path, "autoswitch.threshold", "80")
        assert settings_path(tmp_path).read_text() == "{not json"

    def test_unset_removes_key_and_empty_section(self, tmp_path: Path):
        set_setting(tmp_path, "autoswitch.threshold", "80")
        assert unset_setting(tmp_path, "autoswitch.threshold") is True
        raw = json.loads(settings_path(tmp_path).read_text())
        assert "autoswitch" not in raw

    def test_unset_stamps_schema_version_on_unversioned_file(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"threshold": 80}})
        )
        assert unset_setting(tmp_path, "autoswitch.threshold") is True
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw["schemaVersion"] == 1

    @pytest.mark.parametrize("bad", [[1, 2], "on", 7, None])
    def test_set_on_a_non_object_section_raises_and_preserves_it(
        self, tmp_path: Path, bad
    ):
        """Asserts: `cswap config set` over a file whose section is not a
        JSON object raises ConfigError naming the file and the section, and
        leaves the file as it was, instead of replacing the section with
        {} and dropping whatever the user had there."""
        body = json.dumps({"schemaVersion": 1, "autoswitch": bad})
        settings_path(tmp_path).write_text(body)
        with pytest.raises(ConfigError, match="autoswitch is .* not a JSON object") as caught:
            set_setting(tmp_path, "autoswitch.threshold", "80")
        assert str(settings_path(tmp_path)) in str(caught.value)
        assert settings_path(tmp_path).read_text() == body

    def test_unset_on_a_non_object_section_raises(self, tmp_path: Path):
        """Asserts: `cswap config unset` over a non-object section raises
        ConfigError naming it instead of answering "was not set"."""
        body = json.dumps({"autoswitch": [1]})
        settings_path(tmp_path).write_text(body)
        with pytest.raises(ConfigError, match="autoswitch is .* not a JSON object"):
            unset_setting(tmp_path, "autoswitch.threshold")
        assert settings_path(tmp_path).read_text() == body

    def test_unset_absent_key_is_noop(self, tmp_path: Path):
        assert unset_setting(tmp_path, "autoswitch.threshold") is False
        assert not settings_path(tmp_path).exists()

    def test_set_setting_backs_up_existing_file_to_prev(self, tmp_path: Path):
        # set_setting is a live writer (cli.py, tui/app.py, menubar.py); it
        # must leave a `.prev` recovery copy like save_settings does.
        set_setting(tmp_path, "autoswitch.threshold", "70")
        old_bytes = settings_path(tmp_path).read_bytes()

        set_setting(tmp_path, "autoswitch.threshold", "85")

        prev = settings_path(tmp_path).with_name("settings.json.prev")
        assert prev.read_bytes() == old_bytes

    def test_unset_setting_backs_up_existing_file_to_prev(self, tmp_path: Path):
        set_setting(tmp_path, "autoswitch.threshold", "70")
        set_setting(tmp_path, "autoswitch.cooldownSeconds", "60")
        old_bytes = settings_path(tmp_path).read_bytes()

        unset_setting(tmp_path, "autoswitch.cooldownSeconds")

        prev = settings_path(tmp_path).with_name("settings.json.prev")
        assert prev.read_bytes() == old_bytes


class TestEffectiveSettings:
    def test_missing_file_reports_all_defaults(self, tmp_path: Path):
        rows = effective_settings(tmp_path)
        assert len(rows) == len(SETTING_SPECS)
        assert all(not is_set for _, _, is_set in rows)

    def test_presence_not_value_equality_marks_set(self, tmp_path: Path):
        set_setting(tmp_path, "autoswitch.threshold", "90")  # equals default
        by_key = {spec.dotted: is_set for spec, _, is_set in effective_settings(tmp_path)}
        assert by_key["autoswitch.threshold"] is True
        assert by_key["autoswitch.intervalSeconds"] is False


class TestMergedWithCli:
    def test_no_flags_returns_settings_unchanged(self):
        base = AutoSwitchSettings(threshold=80.0)
        assert merged_with_cli(base, _args()) is base

    def test_cli_beats_settings(self):
        base = AutoSwitchSettings(threshold=80.0, cooldown_seconds=10.0)
        merged = merged_with_cli(base, _args(threshold=60.0, interval=30.0))
        assert merged.threshold == 60.0
        assert merged.interval_seconds == 30.0
        assert merged.cooldown_seconds == 10.0  # untouched

    def test_cli_values_are_clamped(self):
        merged = merged_with_cli(AutoSwitchSettings(), _args(interval=1.0))
        assert merged.interval_seconds == 15.0

    def test_boolean_override(self):
        merged = merged_with_cli(
            AutoSwitchSettings(), _args(include_api_key_accounts=True)
        )
        assert merged.include_api_key_accounts is True

    def test_model_override(self):
        merged = merged_with_cli(AutoSwitchSettings(), _args(model="Fable"))
        assert merged.model == "Fable"

    def test_strategy_override(self):
        merged = merged_with_cli(AutoSwitchSettings(), _args(strategy="consume-first"))
        assert merged.strategy == "consume-first"


class TestAtomicWriteThroughSymlink:
    """A rename does not follow links, so renaming onto a symlinked path
    detaches it and the target silently stops updating. Covers the write
    itself plus the two placement decisions it forces: the temp file goes
    beside the RESOLVED target (else EXDEV across mounts), the 0700 chmod
    stays on the directory cswap owns (else it narrows — or cannot touch —
    a foreign one)."""

    def test_write_preserves_the_link_and_updates_the_target(self, tmp_path):
        repo = tmp_path / "repo"; repo.mkdir()
        live = tmp_path / "live"; live.mkdir()
        tracked = repo / "settings.json"
        tracked.write_text(json.dumps({"tracked": True}))
        link = live / "settings.json"
        link.symlink_to(tracked)

        atomic_write_json(link, {"written": "through"})

        assert link.is_symlink(), "the dotfiles link must survive the write"
        assert json.loads(tracked.read_text()) == {"written": "through"}

    def test_dangling_link_writes_where_it_points(self, tmp_path):
        target = tmp_path / "gone" / "settings.json"
        link = tmp_path / "settings.json"
        link.symlink_to(target)

        atomic_write_json(link, {"dangling": "ok"})

        assert link.is_symlink()
        assert json.loads(target.read_text()) == {"dangling": "ok"}

    def test_plain_file_write_unchanged(self, tmp_path):
        p = tmp_path / "settings.json"
        atomic_write_json(p, {"plain": 1})
        assert not p.is_symlink()
        assert json.loads(p.read_text()) == {"plain": 1}

    def test_temp_file_is_created_beside_the_target(self, tmp_path, monkeypatch):
        """Beside the LINK, the rename hits EXDEV whenever the target is on
        another mount — the write fails outright. Assert the placement
        directly; staging two filesystems in a unit test is not portable."""
        import os as os_mod
        from claude_swap import settings as S
        repo = tmp_path / "repo"; repo.mkdir()
        live = tmp_path / "live"; live.mkdir()
        tracked = repo / "settings.json"; tracked.write_text("{}")
        link = live / "settings.json"; link.symlink_to(tracked)
        seen = []
        real_open = os_mod.open
        # The CREATE, not `mkstemp`: the writer computes the name itself and
        # opens it inside its guard, so an interrupt in the create still leaves
        # a name to unlink. `os.path.dirname` of that name is the placement.
        monkeypatch.setattr(
            S.os, "open",
            lambda path, *a, **kw: (seen.append(os_mod.path.dirname(str(path))),
                                    real_open(path, *a, **kw))[1],
        )

        atomic_write_json(link, {"x": 1})

        # `_backup_prev` (T0938, settings.py) makes its OWN `.prev` copy
        # first, beside the LINK's own directory by design (that
        # function's own docstring: the backup must sit where the
        # link is, not where it points) -- but #199 (which adds it)
        # is still an open PR, so this checks its own premise rather
        # than assuming every tree already carries it.
        expected = [str(live), str(repo)] if hasattr(S, "_backup_prev") else [str(repo)]
        assert seen == expected, (
            f"tmp must land beside the link (_backup_prev) first when "
            f"it exists, then beside the target (the main write), got "
            f"{seen}"
        )

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
    def test_hardening_stays_on_the_directory_cswap_owns(self, tmp_path):
        """The 0700 belongs to cswap's own dir. On the target's parent it
        would narrow a foreign directory, and raise PermissionError when
        that parent cannot be chmod'ed at all."""
        repo = tmp_path / "repo"; repo.mkdir(mode=0o755)
        live = tmp_path / "live"; live.mkdir()
        tracked = repo / "settings.json"; tracked.write_text("{}")
        link = live / "settings.json"; link.symlink_to(tracked)

        atomic_write_json(link, {"x": 1})

        assert (repo.stat().st_mode & 0o777) == 0o755, "foreign dir untouched"
        assert (live.stat().st_mode & 0o777) == 0o700, "our dir hardened"
        assert (tracked.stat().st_mode & 0o777) == 0o600, "file still 0600"


class TestTheWrittenFileLandsAt0600:
    """The published file is EXACTLY 0600, whatever the umask.

    This does not distinguish setting the mode before the publish from after
    it -- both land 0600 -- and it is not claimed to. What it pins is that
    the mode is set at all: ``mkstemp`` masks its request, so dropping the
    call publishes 0400 under a restrictive umask and 0600 under a lax one,
    and only one of those is a file its owner can still write.
    """

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes only")
    @pytest.mark.parametrize("umask", [0o022, 0o377])
    def test_whatever_the_umask(self, tmp_path, umask):
        p = tmp_path / "settings.json"
        previous = os.umask(umask)
        try:
            atomic_write_json(p, {"a": 1})
        finally:
            os.umask(previous)
        assert oct(p.stat().st_mode & 0o777) == "0o600"


class TestCreditThreshold:
    """`autoswitch.creditThreshold` (X3587): the switch point for an account
    holding usage-credit room, allowed up to and including 100."""

    def test_default_is_unset(self, tmp_path: Path):
        """Asserts: with no key in the file the setting reads None, meaning
        "same as threshold"."""
        assert load_settings(tmp_path).credit_threshold is None

    def test_set_accepts_100(self, tmp_path: Path):
        """Asserts: `config set` takes 100 for creditThreshold and the load
        reads it back unclamped."""
        assert set_setting(tmp_path, "autoswitch.creditThreshold", "100") == 100.0
        assert load_settings(tmp_path).credit_threshold == 100.0

    def test_threshold_still_rejects_100(self, tmp_path: Path):
        """Asserts: the plain threshold keeps its range below 100."""
        with pytest.raises(ConfigError, match="between 50 and 99.9"):
            set_setting(tmp_path, "autoswitch.threshold", "100")

    def test_set_rejects_above_100_and_below_the_threshold_floor(self, tmp_path: Path):
        """Asserts: creditThreshold refuses 100.5 and 49, the same floor as
        threshold's, without writing the file."""
        for raw in ("100.5", "49"):
            with pytest.raises(ConfigError, match="between 50 and 100"):
                set_setting(tmp_path, "autoswitch.creditThreshold", raw)
        assert not settings_path(tmp_path).exists()

    def test_load_refuses_out_of_range_and_keeps_null(self, tmp_path: Path):
        """Asserts: a hand-written 120 raises ConfigError naming the key and
        an explicit null reads as unset."""
        path = settings_path(tmp_path)
        path.write_text(json.dumps({"autoswitch": {"creditThreshold": 120}}))
        with pytest.raises(ConfigError, match="autoswitch.creditThreshold = 120"):
            load_settings(tmp_path)
        path.write_text(json.dumps({"autoswitch": {"creditThreshold": None}}))
        assert load_settings(tmp_path).credit_threshold is None

    def test_unset_returns_to_none(self, tmp_path: Path):
        """Asserts: `config unset` removes the key and the setting reads None."""
        set_setting(tmp_path, "autoswitch.creditThreshold", "100")
        assert unset_setting(tmp_path, "autoswitch.creditThreshold") is True
        assert load_settings(tmp_path).credit_threshold is None


def _credit_entry(*, remaining: float | None = 50.0, reached: bool = False,
                  sentinel: str | None = None) -> UsageEntry:
    spend = {
        "used": 10.0,
        "limit": None if remaining is None else 10.0 + remaining,
        "remaining": remaining,
        "pct": None,
        "currency": "USD",
        "limit_reached": reached,
        "reported": "dollars",
    }
    return UsageEntry(
        sentinel=sentinel,
        last_good={"five_hour": {"pct": 99.5}, "spend": spend},
    )


class TestAccountSwitchPoint:
    """`account_switch_point`: creditThreshold for an account with credit
    room, threshold for every other one."""

    def test_credit_room_reads_the_credit_threshold(self):
        """Asserts: a stored reading with money left under the cap, and one
        with no cap, both switch at creditThreshold."""
        s = AutoSwitchSettings(threshold=99.0, credit_threshold=100.0)
        assert account_switch_point(s, _credit_entry()) == 100.0
        assert account_switch_point(s, _credit_entry(remaining=None)) == 100.0

    def test_no_room_reads_the_plain_threshold(self):
        """Asserts: no row, no spend object, a reached cap, no money left and
        a sentinel row all switch at the plain threshold."""
        s = AutoSwitchSettings(threshold=99.0, credit_threshold=100.0)
        assert account_switch_point(s, None) == 99.0
        assert account_switch_point(
            s, UsageEntry(last_good={"five_hour": {"pct": 99.5}})
        ) == 99.0
        assert account_switch_point(s, _credit_entry(reached=True)) == 99.0
        assert account_switch_point(s, _credit_entry(remaining=0.0)) == 99.0
        assert account_switch_point(
            s, _credit_entry(sentinel="relogin_required")
        ) == 99.0

    def test_unset_credit_threshold_is_the_threshold_for_every_account(self):
        """Asserts: with creditThreshold unset, an account holding credits
        still switches at threshold, the rule before X3587."""
        s = AutoSwitchSettings(threshold=99.0)
        assert account_switch_point(s, _credit_entry()) == 99.0
