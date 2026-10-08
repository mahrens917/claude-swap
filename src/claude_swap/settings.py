"""Tool settings persisted at ``<backup_root>/settings.json``.

One versioned JSON file for user-tunable claude-swap preferences, written
atomically with the backup dir's 0600/0700 modes. v1 carries the
``autoswitch`` and ``ui`` sections; other sections can be added additively.
Unknown keys (future fields, other tools' experiments) survive a round trip.

Reading is strict: a missing file, section or key reads as its default, but
a corrupt file, a section that is not an object, or a stored value `cswap
config set` would refuse raises ``ConfigError`` naming the file and the key,
so a bad hand edit is reported rather than run on a default nobody chose.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path

from claude_swap import oauth
from claude_swap.exceptions import ConfigError
from claude_swap.fsutil import replace_with_retry, write_all

SETTINGS_SCHEMA_VERSION = 1
SETTINGS_FILENAME = "settings.json"

_logger = logging.getLogger("claude-swap")


@dataclass(frozen=True)
class AutoSwitchSettings:
    """Policy knobs for the auto-switch engine (``cswap auto``).

    ``threshold`` is binding-window utilization (max of the 5h/7d percentages):
    at or above it the engine looks for a better account. 90 rather than 95
    leaves margin for the macOS ~30s Keychain pickup tail and for heavy
    subagent turns burning past the mark before a swap lands. A proactive
    candidate must itself sit below the threshold (never land somewhere that
    re-triggers next tick) and beat the active account's utilization by at
    least ``hysteresis_pct``, so two accounts hovering at the line never
    ping-pong while a strictly better account is always taken. This is
    ``best``'s rule; ``consume-first`` and ``dynamic`` read the threshold
    differently (see ``SETTING_SPECS["autoswitch.threshold"].help`` and the
    README).
    """

    threshold: float = 90.0
    interval_seconds: float = 60.0
    cooldown_seconds: float = 300.0
    hysteresis_pct: float = 10.0
    strategy: str = "consume-first"  # "best" (most headroom), "consume-first" (soonest weekly reset, default), or "dynamic" (consume-first's ranking, fixed ~97% switch bar)
    include_api_key_accounts: bool = False
    decision_log: bool = False
    unhealthy_ticks: int = 3
    # Comma-separated model display name(s) (e.g. "Fable" or "Fable,Opus"),
    # or "all" for every scoped window an account reports. Each named model's
    # per-model weekly limit is folded into the binding window, so the engine
    # switches off an account whose model quota is exhausted even while its
    # 5h/7d windows still have headroom. None = account-wide 5h/7d only
    # (default).
    model: str | None = None
    # `dynamic` only (#375): how long a departed account's cached org
    # context stays "warm" — a candidate never touched inside this window
    # is "cold" and pays the re-write cost on landing.
    cache_ttl_seconds: float = 3600.0
    # `dynamic` only: the least headroom a COLD candidate needs to be worth
    # the re-write cost (measured: one cold landing cost ~19 5h-points on a
    # 19-session fleet).
    cold_switch_cost_pct: float = 20.0
    # `dynamic` only: how long a healthy active is held before rotating to a
    # warm partner, so both accounts' caches stay inside `cache_ttl_seconds`.
    alternation_chunk_seconds: float = 600.0
    # The switch point for an account whose stored reading has usage-credit
    # room (credits on, monthly cap not reached, money left). Such an account
    # keeps answering past its window limit on paid credits, so its last
    # points are safe to spend; an account without credits refuses requests
    # once full, until the next poll moves the sessions. None means "same as
    # `threshold`". Unlike `threshold` it may be 100. Read per account
    # through :func:`account_switch_point`.
    credit_threshold: float | None = None


@dataclass(frozen=True)
class UiSettings:
    """Appearance preferences (``ui`` section). ``theme`` selects the TUI/CLI
    color theme; ``auto`` follows terminal-background detection."""

    theme: str = "auto"


_SECTION_DEFAULT_SOURCES = {"autoswitch": AutoSwitchSettings, "ui": UiSettings}


@dataclass(frozen=True)
class SettingSpec:
    """Metadata for one user-tunable settings.json key.

    Single source of truth for bounds/choices: the check on load
    (`_stored_value`), the strict validation in `cswap config set`
    (`parse_setting_value`) and the clamp of `cswap auto`'s command-line
    overrides (`_clamped`) read from here, so they can't drift.
    """

    section: str  # top-level JSON section ("autoswitch", "ui")
    json_key: str  # camelCase key inside the section
    field: str  # snake_case AutoSwitchSettings field
    kind: str  # "float" | "int" | "bool" | "choice"
    lo: float | None = None
    hi: float | None = None
    choices: tuple[str, ...] = ()
    help: str = ""

    @property
    def dotted(self) -> str:
        return f"{self.section}.{self.json_key}"

    @property
    def default(self):
        return getattr(_SECTION_DEFAULT_SOURCES[self.section](), self.field)


# settings.json uses camelCase (matching the repo's other JSON artifacts);
# dataclass fields stay snake_case.
SETTING_SPECS: dict[str, SettingSpec] = {
    spec.dotted: spec
    for spec in (
        SettingSpec(
            "autoswitch", "threshold", "threshold", "float", 50.0, 99.9,
            help="Switch when the binding 5h/7d window reaches this pct "
            "(dynamic: fixed near 97% instead; still gates blackout/cadence)",
        ),
        SettingSpec(
            "autoswitch", "creditThreshold", "credit_threshold", "float", 50.0, 100.0,
            help="Switch point for an account with usage credits left "
            "(may be 100); unset means the same as threshold",
        ),
        SettingSpec(
            "autoswitch", "intervalSeconds", "interval_seconds", "float", 15.0, 3600.0,
            help="Poll interval for the cswap auto loop, in seconds",
        ),
        SettingSpec(
            "autoswitch", "cooldownSeconds", "cooldown_seconds", "float", 0.0, 86400.0,
            help="Minimum seconds between proactive switches",
        ),
        SettingSpec(
            "autoswitch", "hysteresisPct", "hysteresis_pct", "float", 0.0, 50.0,
            help="A target must beat the active account by this many pct",
        ),
        SettingSpec(
            "autoswitch", "strategy", "strategy", "choice",
            choices=("best", "consume-first", "dynamic"),
            help="How auto-switch picks the target account",
        ),
        SettingSpec(
            "autoswitch", "includeApiKeyAccounts", "include_api_key_accounts", "bool",
            help="Allow rotating onto managed API-key accounts (bill per token)",
        ),
        SettingSpec(
            "autoswitch", "decisionLog", "decision_log", "bool",
            help="Record why each tick switched or did not, to its own log file",
        ),
        SettingSpec(
            "autoswitch", "unhealthyTicks", "unhealthy_ticks", "int", 1, 100,
            help="Consecutive failed polls before an account is unhealthy",
        ),
        SettingSpec(
            "autoswitch", "model", "model", "string",
            help="Also switch on these models' weekly limits (e.g. Fable, Fable,Opus, or all)",
        ),
        SettingSpec(
            "autoswitch", "cacheTtlSeconds", "cache_ttl_seconds", "float", 60.0, 86400.0,
            help="dynamic: how long a departed account's cache stays warm",
        ),
        SettingSpec(
            "autoswitch", "coldSwitchCostPct", "cold_switch_cost_pct", "float", 0.0, 100.0,
            help="dynamic: headroom a cold candidate needs to be admitted",
        ),
        SettingSpec(
            "autoswitch", "alternationChunkSeconds", "alternation_chunk_seconds",
            "float", 60.0, 3600.0,
            help="dynamic: how long to sit before rotating to a warm partner",
        ),
        SettingSpec(
            "ui", "theme", "theme", "choice", choices=("dark", "light", "auto"),
            help="Color theme; auto follows the terminal background",
        ),
    )
}

_AUTOSWITCH_KEYS: dict[str, str] = {
    spec.field: spec.json_key
    for spec in SETTING_SPECS.values()
    if spec.section == "autoswitch"
}


def settings_path(backup_root: Path) -> Path:
    return backup_root / SETTINGS_FILENAME


def parse_model_names(value: str | None) -> tuple[str, ...]:
    """Split a comma-separated model list, trimmed and case-insensitively
    deduped (first spelling wins). Shared by the auto engine and the manual
    switch strategies so both read ``autoswitch.model`` identically."""
    if not value:
        return ()
    seen: dict[str, str] = {}
    for part in value.split(","):
        name = part.strip()
        if name and name.lower() not in seen:
            seen[name.lower()] = name
    return tuple(seen.values())


def account_switch_point(settings: AutoSwitchSettings, entry) -> float:
    """The utilization pct at which this account is switched away from.

    ``credit_threshold`` when it is set and the account's stored reading has
    usage-credit room (:func:`oauth.entry_credit_room`), else ``threshold``.
    ``entry`` is the account's ``UsageEntry`` (None when it has none, which
    holds no credit room). The auto engine and ``cswap list --json``'s
    ``switchThreshold`` both read it here, so the two cannot disagree.
    The point covers every window the account is measured on, the per-model
    weekly windows (``autoswitch.model``, e.g. Fable) included.
    """
    credit_point = settings.credit_threshold
    if credit_point is not None and holds_credit_point(settings, entry):
        return credit_point
    return settings.threshold


def holds_credit_point(settings: AutoSwitchSettings, entry) -> bool:
    """Whether this account switches at ``credit_threshold``: it is set and
    the account's stored reading has usage-credit room.

    Such an account keeps answering past any window limit, a per-model
    weekly window (Fable) at 100 included, on paid credits. False for every
    account while ``credit_threshold`` is unset, so the pre-credit rules
    hold unchanged.
    """
    return (
        settings.credit_threshold is not None
        and oauth.entry_credit_room(entry) is not None
    )


def _clamped(settings: AutoSwitchSettings) -> AutoSwitchSettings:
    """Clamp `cswap auto`'s command-line overrides (`merged_with_cli`) into
    the SETTING_SPECS ranges; bad types -> the default. Never applied to a
    loaded file, which `_stored_value` checks instead."""

    def num(value, default: float, lo: float, hi: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        return float(min(max(value, lo), hi))

    kwargs = {}
    for spec in SETTING_SPECS.values():
        if spec.section != "autoswitch":
            continue
        value = getattr(settings, spec.field)
        if spec.kind in ("float", "int"):
            clamped = num(value, spec.default, spec.lo, spec.hi)
            kwargs[spec.field] = int(clamped) if spec.kind == "int" else clamped
        elif spec.kind == "bool":
            kwargs[spec.field] = bool(value)
        elif spec.kind == "string":
            # A non-empty string keeps as-is; anything else reverts to default
            # (None) so a null/garbage settings.json value disables the filter.
            kwargs[spec.field] = value if isinstance(value, str) and value else spec.default
        else:  # choice
            if value not in spec.choices:
                _logger.warning(
                    "settings.json: unsupported %s %r; using %r",
                    spec.dotted, value, spec.default,
                )
                value = spec.default
            kwargs[spec.field] = value
    return AutoSwitchSettings(**kwargs)


def _read_raw(path: Path, *, for_write: bool = False) -> dict:
    """The settings file as a dict: the one reader of settings.json.

    A file that is there but unreadable, not JSON, or not a JSON object
    raises ``ConfigError`` naming the file: a read would otherwise run every
    setting on a default nobody chose, and a read-modify-write starting from
    ``{}`` would replace a malformed (maybe hand-recoverable) file with a
    near-empty one. ``for_write`` names the caller a read-modify-write, and
    adds the remedy to the message.
    """
    remedy = "; fix or delete it before changing settings" if for_write else ""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # default: EXTERNAL -- source: a fresh install, where nothing has
        # written settings.json yet -- why: every key then holds its
        # documented default, the same as a file that sets none of them.
        return {}
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"could not read {path}: {e}{remedy}") from e
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path} is not valid JSON ({e}){remedy}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} is not a JSON object{remedy}")
    return raw


def _section(path: Path, raw: dict, name: str) -> dict:
    """``raw[name]`` as a dict. A section that is there but not an object
    raises ``ConfigError`` naming the file and the section."""
    if name not in raw:
        # default: EXTERNAL -- source: `cswap config set` writes only the
        # section it is given, so a file can hold `ui` and no `autoswitch`
        # -- why: an absent section sets no key, and every key keeps its
        # documented default.
        return {}
    section = raw[name]
    if not isinstance(section, dict):
        raise ConfigError(f"{path}: {name} is {section!r}, not a JSON object")
    return section


def _stored_value(path: Path, spec: SettingSpec, value):
    """A stored settings value, checked by the rule `cswap config set`
    applies on write (``parse_setting_value``). One the engine would
    otherwise replace with a default or clamp raises ``ConfigError``
    naming the file and the key."""
    where = f"{path}: {spec.dotted} = {value!r}"
    if value is None and spec.default is None:
        # A JSON null on a key whose default is None (`model`,
        # `creditThreshold`) is that documented "unset" value, the one
        # `save_settings` itself writes for it.
        return None
    if spec.kind == "bool":
        if not isinstance(value, bool):
            raise ConfigError(f"{where} is not true or false")
        return value
    if spec.kind == "choice":
        if value not in spec.choices:
            raise ConfigError(f"{where} is not one of: {', '.join(spec.choices)}")
        return value
    if spec.kind == "string":
        if not isinstance(value, str) or not value:
            raise ConfigError(f"{where} is not a non-empty string")
        return value
    number_types = (int,) if spec.kind == "int" else (int, float)
    if isinstance(value, bool) or not isinstance(value, number_types):
        noun = "an integer" if spec.kind == "int" else "a number"
        raise ConfigError(f"{where} is not {noun}")
    if not spec.lo <= value <= spec.hi:
        raise ConfigError(
            f"{where} is outside {format_setting_value(spec.lo)} to "
            f"{format_setting_value(spec.hi)}"
        )
    return int(value) if spec.kind == "int" else float(value)


def load_settings(backup_root: Path) -> AutoSwitchSettings:
    """Load the autoswitch section. A missing file or section, or a key it
    does not set, reads as that key's default; a corrupt file, a section
    that is not an object, or a value `cswap config set` would refuse
    raises ``ConfigError`` naming the file and the key."""
    path = settings_path(backup_root)
    section = _section(path, _read_raw(path), "autoswitch")
    kwargs = {}
    for spec in SETTING_SPECS.values():
        if spec.section == "autoswitch" and spec.json_key in section:
            kwargs[spec.field] = _stored_value(path, spec, section[spec.json_key])
    return AutoSwitchSettings(**kwargs)


def load_ui_settings(backup_root: Path) -> UiSettings:
    """Load the ui section: a missing file, section or key reads as the
    default theme; an unsupported theme raises ``ConfigError`` naming the
    file and the key."""
    path = settings_path(backup_root)
    section = _section(path, _read_raw(path), "ui")
    if "theme" not in section:
        return UiSettings()
    return UiSettings(
        theme=_stored_value(path, SETTING_SPECS["ui.theme"], section["theme"])
    )


def save_settings(backup_root: Path, settings: AutoSwitchSettings) -> None:
    """Write the autoswitch section, preserving unknown keys and sections."""
    path = settings_path(backup_root)
    raw = _read_raw(path, for_write=True)
    raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
    section = _section(path, raw, "autoswitch")
    for field, json_key in _AUTOSWITCH_KEYS.items():
        section[json_key] = getattr(settings, field)
    raw["autoswitch"] = section
    atomic_write_json(path, raw)


def setting_spec(dotted_key: str) -> SettingSpec:
    """Look up a spec by dotted key; unknown keys raise with the valid list."""
    spec = SETTING_SPECS.get(dotted_key)
    if spec is None:
        raise ConfigError(
            f"unknown setting '{dotted_key}'\n"
            f"Valid keys: {', '.join(SETTING_SPECS)}"
        )
    return spec


_BOOL_WORDS = {
    "true": True, "1": True, "yes": True,
    "false": False, "0": False, "no": False,
}


def parse_setting_value(spec: SettingSpec, raw_value: str):
    """Strictly parse a CLI-provided string for `cswap config set`.

    Out-of-range or mistyped values raise ConfigError so the user learns
    about the problem when setting the value; the load check
    (`_stored_value`) refuses the same values in a hand-edited file.
    """
    if spec.kind == "bool":
        # Never bool(str): bool("false") is True.
        parsed = _BOOL_WORDS.get(raw_value.strip().lower())
        if parsed is None:
            raise ConfigError(
                f"{spec.dotted} expects true or false (or 1/0, yes/no), "
                f"got '{raw_value}'"
            )
        return parsed
    if spec.kind == "choice":
        if raw_value not in spec.choices:
            raise ConfigError(
                f"{spec.dotted} must be one of: {', '.join(spec.choices)}"
            )
        return raw_value
    if spec.kind == "string":
        value = raw_value.strip()
        if not value:
            raise ConfigError(
                f"{spec.dotted} expects a non-empty value; use "
                f"'cswap config unset {spec.dotted}' to clear it"
            )
        return value
    try:
        value = int(raw_value) if spec.kind == "int" else float(raw_value)
    except ValueError:
        noun = "an integer" if spec.kind == "int" else "a number"
        raise ConfigError(
            f"{spec.dotted} expects {noun}, got '{raw_value}'"
        ) from None
    if not spec.lo <= value <= spec.hi:
        raise ConfigError(
            f"{spec.dotted} must be between {format_setting_value(spec.lo)} "
            f"and {format_setting_value(spec.hi)}"
        )
    return value


def format_setting_value(value) -> str:
    """Render a settings value the way settings.json writes it."""
    if value is None:
        return "(none)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def set_setting(backup_root: Path, dotted_key: str, raw_value: str):
    """Validate and persist one key for `cswap config set`; returns the value.

    Writes only the given key (plus schemaVersion) — deliberately not
    ``save_settings``, which writes every known key and would freeze the
    current defaults into the file, pinning users to them if a later version
    changes a default. Unknown keys and sections in the file survive.
    """
    spec = setting_spec(dotted_key)
    value = parse_setting_value(spec, raw_value)
    path = settings_path(backup_root)
    raw = _read_raw(path, for_write=True)
    raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
    section = _section(path, raw, spec.section)
    section[spec.json_key] = value
    raw[spec.section] = section
    atomic_write_json(path, raw)
    return value


def unset_setting(backup_root: Path, dotted_key: str) -> bool:
    """Remove one key from settings.json; False if it wasn't set (no write)."""
    spec = setting_spec(dotted_key)
    path = settings_path(backup_root)
    raw = _read_raw(path, for_write=True)
    section = _section(path, raw, spec.section)
    if spec.json_key not in section:
        return False
    raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
    del section[spec.json_key]
    if not section:
        del raw[spec.section]
    atomic_write_json(path, raw)
    return True


def effective_settings(backup_root: Path) -> list[tuple[SettingSpec, object, bool]]:
    """(spec, effective value, explicitly set?) per key, in registry order.

    "Set" means the key is present in the raw file — an explicit value equal
    to the default still counts — so `cswap config`'s "(default)" marker
    reflects the file, not value equality.
    """
    raw = _read_raw(settings_path(backup_root))
    loaded = {
        "autoswitch": load_settings(backup_root),
        "ui": load_ui_settings(backup_root),
    }
    rows = []
    for spec in SETTING_SPECS.values():
        section = raw.get(spec.section)
        is_set = isinstance(section, dict) and spec.json_key in section
        rows.append((spec, getattr(loaded[spec.section], spec.field), is_set))
    return rows


def merged_with_cli(settings: AutoSwitchSettings, args) -> AutoSwitchSettings:
    """Overlay non-None CLI overrides (argparse Namespace) onto settings."""
    overrides = {}
    for attr, field in (
        ("threshold", "threshold"),
        ("interval", "interval_seconds"),
        ("cooldown", "cooldown_seconds"),
        ("include_api_key_accounts", "include_api_key_accounts"),
        ("model", "model"),
        ("strategy", "strategy"),
    ):
        value = getattr(args, attr, None)
        if value is not None:
            overrides[field] = value
    if not overrides:
        return settings
    return _clamped(dataclasses.replace(settings, **overrides))


def _backup_prev(path: Path, data: dict) -> None:
    """Best-effort ``.prev`` copy of ``path``'s pre-write bytes.

    Only ``atomic_write_json`` calls this, and only for settings.json.
    Skipped when the file doesn't exist yet, or the incoming write is a
    no-op (a repeated identical save must not replace the one real previous
    generation with a duplicate of itself — mirrors credentials.py's
    ``_retain_previous_backup``). Beside the LINK (``path``), never the
    resolved target: where settings.json is a symlink into a dotfiles
    repo, the backup must sit where the link is, not where it points.
    Never blocks the write: a failure here is logged and dropped.
    """
    try:
        current = path.read_bytes()
    except FileNotFoundError:
        return
    except OSError as e:
        _logger.warning("Could not read %s for backup (%s)", path, e)
        return
    if current == json.dumps(data, indent=2).encode("utf-8"):
        return
    prev_path = path.with_name(path.name + ".prev")
    tmp_path = prev_path.with_name(prev_path.name + f".{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        try:
            fd = os.open(str(tmp_path), flags, 0o600)
        except OSError:
            # NOT OURS TO REMOVE: O_EXCL refused because somebody
            # else already holds this exact pid-stamped name, so
            # the cleanup below must not unlink a file this call
            # never created (same idiom as switcher.py's own
            # O_EXCL create).
            tmp_path = None
            raise
        try:
            write_all(fd, current)
        finally:
            os.close(fd)
        os.replace(str(tmp_path), str(prev_path))
    except OSError as e:
        _logger.warning("Could not back up %s (%s)", path, e)
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def atomic_write_json(path: Path, data: dict) -> None:
    """Atomically write JSON with the backup dir's 0600/0700 modes.

    Shared by settings.json and the autoswitch state file (and any future
    machine-local state files beside them).

    **Writes THROUGH a symlink, never over it.** A rename swaps a directory
    ENTRY and does not follow links, so renaming onto a symlinked path
    DETACHES the link: the write succeeds, the content is right, and the
    link target silently stops receiving updates — until something restores
    the link (a dotfiles deploy), taking every change written since with
    it. Same shape as #192/#193, which fixed ``session.py``'s own writer;
    this is the shared JSON writer. Three consequences, each deliberate:

    - A DANGLING link still writes where it points; linking a path is a
      request to write there.
    - The temp file is created beside the RESOLVED target, so the rename
      stays on one filesystem and remains atomic (beside the LINK it would
      hit EXDEV whenever the target lives on another mount).
    - The 0700 hardening stays on the directory cswap owns. Applying it to
      the resolved parent would narrow a directory belonging to something
      else, and raise ``PermissionError`` outright when that parent is not
      ours to chmod. The written file still gets 0600, set on the fd before
      the publish, so the secret is never exposed at any point.
    """
    target = Path(os.path.realpath(path)) if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    if path.name == SETTINGS_FILENAME:
        _backup_prev(path, data)
    if sys.platform != "win32":
        # `path.parent`, NOT the target's: see the docstring.
        os.chmod(path.parent, 0o700)
    # THE NAME BEFORE THE FILE. `mkstemp` picks the name internally and opens
    # the file before it returns, so an interrupt in that window strands a temp
    # nothing can name. `O_EXCL` keeps the collision safety mkstemp gave.
    tmp_path = str(target.parent
                   / f".{target.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = -1
    try:
        try:
            fd = os.open(
                tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # NOT OURS TO REMOVE. `O_EXCL` refused because somebody holds
            # the name, so the cleanup below must not unlink their file.
            tmp_path = None
            raise
        write_all(fd, json.dumps(data, indent=2).encode("utf-8"))
        if sys.platform != "win32":
            # On the fd, BEFORE the publish. Not for secrecy: `mkstemp`
            # opens at 0600 and a umask only clears bits, so its temp is
            # never wider. It is so the try block ends AT the publish — a
            # chmod on the target after it can fail once the rename has
            # handed the temp name to whoever draws it next.
            os.fchmod(fd, 0o600)
        os.close(fd)
        fd = -1
        replace_with_retry(tmp_path, str(target))
    except BaseException:
        if fd >= 0:
            os.close(fd)
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise
