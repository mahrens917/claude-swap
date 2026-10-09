# claude-swap

Multi-account switcher for Claude Code. Easily switch between multiple Claude accounts without logging out, or let it switch for you before you hit a rate limit. Track usage for every account in a live dashboard, and run accounts in parallel. Works with both the Claude Code CLI and the VS Code extension.

## Installation

### Using uv (recommended)

```bash
uv tool install claude-swap
```

### Using pipx

```bash
pipx install claude-swap
```

### From source

```bash
git clone https://github.com/realiti4/claude-swap.git
cd claude-swap
uv sync
uv run cswap help
```

### Updating

```bash
cswap upgrade          # uv/pipx installs on macOS/Linux: auto-detects and upgrades
# or run your installer directly:
uv tool upgrade claude-swap
pipx upgrade claude-swap
```

**Upgrades and the usage store** (in claude-swap's data directory, `~/.local/share/claude-swap` on Linux and `~/.claude-swap-backup` on macOS): a process keeps running the code it loaded, so a process of the previous build (`cswap auto`, the menu bar, or the owner proxy including any draining process it keeps for in-flight requests, which loads cswap into its own process) keeps writing with old code after an install. Two rules keep it from damaging what the new build writes:

- *The store's file name carries its schema version*: `cache/usage-v4.json` for `schemaVersion` 4. A process of an older schema writes only its own file, which the new build never reads or writes. On first load, when `usage-v4.json` is absent and the previous `cache/usage.json` holds schema 3 or 4, its rows are copied forward once (schema 3 migrated) under the store's lock, and `usage.json` is never read or written again. A future schema bump follows the same path: a new file name, and a one-time copy forward from the previous version's file.
- *Every write checks the build*: before writing any file another cswap process shares, a process compares the claude-swap build it loaded (a sha256 over the package's own source files, labelled with the version and, for a VCS install, the commit) with the build installed on disk now, re-hashing only when a package file's stat changed. That covers the usage store, the auto engine's state file, `settings.json`, `sequence.json`, `.migrations.json`, every stored account credential and config (files and macOS Keychain items, deletes included), the live credential and `~/.claude.json` a switch writes, the unclaimed-credential stash, the mappings, the session manifests and the session profile credentials a credential change or a remove clears, an import, and the cache files such as `cache/update_check.json`. A switch, swap, move, add, remove, import or purge checks once before its first write, so a refusal leaves the store whole rather than half-written. On a mismatch the write is refused with `StaleBuildWriteError`, the file keeps its bytes, and ONE WARNING is logged per file per process: `refusing to write <file>: this process loaded build X, build Y is installed`. While an install is replacing the package files a file can vanish or be unreadable for a moment; the installed build then cannot be read, which is treated as not the loaded build, and the WARNING reads `refusing to write <file>: this process loaded build X, the installed build cannot be read (<error>), as while an install replaces the package files, so it is treated as not the loaded build`. Reads stay allowed.
- *What an operator sees during an install*: each process still running the previous build (`cswap auto`, the menu bar, a draining owner proxy) logs one of those WARNING lines per shared file the first time it tries to write it, and writes nothing from then on; restarting it onto the new build ends them. A process of the new build logs none. The owner proxy, on its first refusal, logs one line `WARNING usage and credential-refusal recording stopped in this process (pid N), and so did wall switches: refusing to write ...` and from then on records no usage or credential refusal and switches off no wall: a 429 that would have switched accounts is relayed with its rate-limit headers stripped, as when a switch does not land, and its connections stay up while the processes of the new build do the recording and switching.

A build never overwrites a store it cannot read either: an unknown schema version or an unparseable file makes every write raise `UsageStoreVersionError`, logged at ERROR with the file and both versions, and the file stays as it was. A build older than the one that introduced these rules cannot be changed, so for that one install stop every process of the old build before the first command of the new one.

## Usage

### Add your first account

Log into Claude Code with your first account, then:

```bash
cswap add
```

### Add more accounts

Log in with another account, then:

```bash
cswap add
```

Do not run `/logout` first: current Claude Code may revoke the refresh token stored for the account you are leaving.

### Switch accounts

Rotate to the next account:

```bash
cswap switch
```

Or switch to a specific account:

```bash
cswap switch 2
cswap switch user@example.com
cswap switch dev                # or by alias, once set with `cswap alias 2 dev`
```

Not sure which one? `cswap list` is the dashboard — every account's 5-hour and 7-day usage and reset times at a glance:

```bash
cswap list
```

Or let claude-swap auto-pick by remaining quota — `cswap switch --strategy best` (most quota left) or `--strategy next-available` (skip rate-limited accounts).

**Note:** You usually don't need to restart — on Linux/Windows the new account is picked up automatically, and on macOS after the Keychain cache expires. To apply it instantly, restart Claude Code or reopen the VS Code extension tab. See [Tips](#tips) for the per-platform details.

### Automatic switching

Let claude-swap watch your usage and switch for you. When the active account's 5-hour or 7-day window reaches the threshold (default 90%), it switches to the account whose weekly window resets soonest (the default strategy; `--strategy best` picks the most quota left instead) — before you hit the limit, and safe to run while Claude Code is working:

```bash
cswap auto                     # foreground loop, polls every 60s
cswap auto --threshold 80      # switch earlier
cswap auto --model Fable       # also switch when the Fable weekly limit is hit
cswap auto --once              # single check-and-switch, for cron/scripts
cswap auto --dry-run           # log what it would do, never switch
cswap auto --strategy consume-first   # burn the soonest-resetting account first
```

<details>
<summary>How it behaves & advanced usage</summary>

- Runs safely alongside Claude Code: switches take the same credential locks Claude Code uses, so a swap never collides with a token refresh.
- A cooldown (default 5 min) and a hysteresis margin stop it flip-flopping near the threshold: a proactive switch only lands on an account that's below the threshold *and* better than the current one by the margin — a candidate that clears the margin is always taken, but two accounts hovering at the line never ping-pong. `consume-first` (the default) is the exception: no margin — below the threshold it moves only to a sooner-resetting account, at the threshold it takes any account still under it, soonest reset first. `dynamic` admits candidates below its own ~97% bar instead of the configured threshold. When every account is exhausted it keeps checking on a bounded slow cadence, waking sooner for an imminent reset.
- **Usage credits** (Anthropic's extra usage, the monthly dollar cap billed beyond the 5h/7d windows) pay only for winding down, never for continuing. When every account's windows are full, the engine waits for the earliest reset (the all-exhausted wait) whatever credits any account holds: it never switches to an account because it has credits and never holds the sessions on the active because of them. If the active account still has credit room at that moment, Claude Code keeps billing those credits for whatever is already in flight, so the engine logs one WARNING per all-exhausted stretch, `every account is at its limit; sessions on Account-N now bill its usage credits ($70.00 left (balance)): wind down, start nothing new`, and every `all-exhausted` event carries `activeBillsCredits: true` with the same money words in `activeCredits` (`activeBillsCredits: false` and `activeCredits: null` otherwise). The money words read each account's `remaining` by its `remainingBasis`: money left from an entered balance (`creditBalances.<email>`), else room under the monthly limit, else a setup-token account's share of its cap used.
- **Credit switch point** (`cswap config set autoswitch.creditThreshold 100`): an account with usage-credit room (credits on, monthly cap not reached, money left) keeps answering past its window limit on credits, so its last points are safe to spend; an account without credits refuses requests once full, until the next poll moves the sessions. `autoswitch.creditThreshold` is the switch point for accounts with credit room (50 to 100, and unlike `autoswitch.threshold` it may be 100); every other account keeps `autoswitch.threshold`. Unset (the default) means the same as the threshold. Every threshold comparison the engine makes (leaving the active, admitting a landing, the every-account-above-threshold state, the consume-first ranking tiers, the poll line) reads each account's own switch point, and `cswap list --json` carries it per account as `switchThreshold`. Under the `dynamic` strategy, whose bar is otherwise a fixed 97%, an account with credit room is blocked and departs at its credit switch point too. The dashboard's preview reads the same per-account switch points, so it names the move the engine makes. The credit switch point covers the per-model weekly windows (`autoswitch.model`, e.g. Fable) too: an account with credit room is not walled by a model window below its credit switch point, and is walled at it like any other account, so when every account's model window is full the fleet-wide model wall stands and the sessions keep working on the remaining 5h/7d room, credits or not. Only accounts the rotation can move to count here: a quarantined or disabled account below its point does not lift the model wall for the rest. When an account held past the threshold on its credits loses that room (cap reached or money spent) while still over the threshold, a WARNING names it and its usage, and it switches at the plain threshold from then on.
- An API outage (a burst of server errors on the active account's requests) moves it off immediately, whether or not it's below the threshold — the cooldown and hysteresis above don't apply, since it's the API that's failing, not the account. The account it left is held out of rotation for 15 minutes so it isn't switched straight back onto while the outage continues. Needs the `cswap-pin` request trace armed (off by default) on cswap-pin 0.1.148 or newer; it's a silent no-op on an older pin, or with the trace off.
- **Strategies** (`--strategy`, or `cswap config set autoswitch.strategy`): `consume-first` (default) proactively keeps you on the account whose **weekly window resets soonest** — use-it-or-lose-it — switching to a sooner-resetting account (with room to spare) even below the threshold, so perishable weekly quota isn't wasted. `best` stays put until the active account nears its limit, then moves to the account with the most quota left. `dynamic` ranks like `consume-first` but also re-checks whether a `--model` window is really what's binding each tick, and never lands on an account that has no room on the window actually in force — its own proactive switch point is fixed at ~97% used, not the configured `--threshold` (which still decides whether a fleet-wide `--model` blackout drops the model set for ranking, and steers poll cadence and the exhausted-fleet recovery hold).
- Usage polling is adaptive — a couple of accounts per check, busy alternates watched more closely, and exhausted ones checked about every ten minutes (or slower after 429s) — so API traffic stays flat no matter how many accounts you manage.
- It fails safe: if a usage check errors it keeps trusting the last-known numbers while retries back off, and an expired token on an idle machine makes it hold rather than fail over (Claude Code refreshes the token on your next message).
- An account whose refresh token has died is quarantined and reported until you either log in with it and re-run `cswap add --slot N`, or replace its stored credentials from a known-good export — a plain `cswap import backup.cswap` replaces dead-token slots on its own (`--force` is still required to replace other existing accounts; note a stale export can carry an already-superseded token). API-key accounts are never rotated onto unless you pass `--include-api-key-accounts`.
- To hold an account out of rotation yourself — a work account you don't want touched, one you're resting — run `cswap disable <num|email>`; `cswap enable <num|email>` puts it back. Disabled accounts are skipped by auto-switch, bare `cswap switch`, and the `best` / `next-available` strategies, but stay fully managed and remain a valid explicit `cswap switch <num|email>` target — with the caveat that a RUNNING auto-switch leaves a disabled account on its next tick, so an explicit switch onto one sticks only while auto is stopped. They show a `(disabled)` marker in `cswap list`, in the [TUI](#interactive-dashboard-tui), and in the [menu bar](#menu-bar-macos) — both of which also let you toggle the state in place (TUI: menu → *Disable / enable account…*; menu bar: *Disable / enable account*).
- By default only the account-wide 5h/7d windows drive switching. If you work on one model and hit its **weekly per-model limit** first (e.g. Fable), add `--model Fable` (or `cswap config set autoswitch.model Fable`) to fold that model's window into the decision, so it switches off an account whose model quota is spent even while its 5h/7d windows still have room.
  - **Model names** are Anthropic's own per-model `display_name`s, matched case-insensitively. The exact strings for your accounts are the per-model rows in `cswap list` (e.g. a line reading `Fable: 100%`).

For cron/systemd timers, `--once` reports the outcome in its exit code (`0` switched, `1` error, `2` nothing to do, `3` blocked — no viable target), and `--json` emits one JSON event per line:

```bash
*/5 * * * * cswap auto --once --json >> ~/.cswap-auto.log 2>&1
```

Defaults like the threshold and cooldown are configurable with `cswap config set autoswitch.threshold 80` — flags override them (see [Configuration](#configuration)).

</details>

### Run multiple accounts at the same time (session mode)

Launch Claude Code as a specific account in the current terminal only — every other terminal and the VS Code extension stay on your default account, so two accounts can work in parallel.

```bash
cswap run 2                     # launch Claude Code as account 2, here only
cswap run user@example.com      # by email
cswap run 2 -- --resume         # everything after '--' is forwarded to claude
cswap run 2 --share-history     # share your chat history with this account too
cswap run 2 --require-session   # refuse rather than run plain claude on the default login
```

Sessions use your normal `~/.claude` setup (settings, CLAUDE.md, skills, MCP servers, etc.), but each account keeps its own chat history — pass `--share-history` if you want your accounts to continue the same conversations.

Running the account that is already your default login launches plain `claude` on that login instead of a session (a second copy of the active credential would go stale). Scripts that need the isolation guaranteed can pass `--require-session`, which refuses instead — both there and when no account is named and the directory maps to none, the other way `cswap run` reaches the default login.

A session refreshes its own copy of the account's token, so once it exits, the credential it rotated is captured back into the account's stored backup before a switch or usage check uses that backup. While a session is still running, `cswap switch` refuses to move the default login onto its account if the stored backup has already fallen behind (activating it could only fail); exit the session first, or pick another account. While a session runs, its account's usage is read with the session's own credential and never refreshed by cswap; a read the server refuses shows as token expired, and is not requested again, until the session renews the credential on its next call.

<details>
<summary>Sharing details — MCP servers & chat history</summary>

- With `--share-history`, a session started under one account shows up in `--resume` under the others, and nothing already saved is lost.
- User-scope MCP servers (`claude mcp add -s user`) are mirrored from your default profile on every launch — manage them there; changes made inside a session don't persist. Definitions are copied as-is (including inline `env`/`headers` values), but MCP OAuth logins are not — HTTP servers may ask you to authenticate once per profile via `/mcp`.
- `--no-share` turns sharing off and removes the mirrored MCP config (profiles that never mirrored are left alone).

</details>

<details>
<summary>Map accounts to directories — auto-pick per repo</summary>

Bind a directory to an account, and a bare `cswap run` there launches that account in session mode — e.g. work account in work repos, personal elsewhere:

```bash
cswap map 2 ~/work/client-app   # map a directory to account 2
cswap map user@example.com      # map the current directory
cswap map                       # list mappings
cswap unmap ~/work/client-app   # remove one (defaults to current directory)

cd ~/work/client-app/src
cswap run                       # → account 2, session mode
```

Subfolders inherit the nearest mapped ancestor. In an unmapped directory, `cswap run` just launches plain `claude` with your default login. Mappings are per-machine (not part of `cswap export`) and are cleaned up when their account is removed.

</details>

### Interactive dashboard (TUI)

Run `cswap` on its own (or `cswap tui`) for the full-screen dashboard: live usage for every account, switching, and the auto-switcher, all keyboard-driven. `cswap watch` opens it straight to the live monitor. Works on macOS, Linux, and Windows.

`cswap tui --auto` opens the auto-switch view **live**, for a TUI meant to keep running unattended — after a reboot, a deploy, or a tmux respawn it resumes switching instead of waiting in dry-run for a keypress. Only the flag does this: a bare `cswap tui` lands on the dashboard, and reaching the auto view from the menu watches without switching, so opening a view never starts moving accounts on its own. One live engine runs per machine — a second TUI still shows its dashboard but stays in dry-run and says so, since two engines decide independently and undo each other's switches.

<img src="assets/tui-watch.png" width="760" alt="cswap watch — live 5h/7d usage bars for every account, with reset times and the active account marked">

### Refresh expired tokens

If an account's token expires, log back into Claude Code with that account and re-run:

```bash
cswap add
```

This will update the stored credentials without creating a duplicate.

### Other commands

```bash
cswap run 2                     # Run an account in this terminal only (session mode)
cswap auto                      # Auto-switch when nearing rate limits (see above)
cswap config                    # Show or edit settings (see Configuration below)
cswap list                      # Show all accounts with 5h/7d usage and reset times
cswap list --token-status       # Add source-labelled OAuth token diagnostics
cswap status                    # Show current account
cswap add --slot 3              # Add account to a specific slot (prompts before overwrite)
cswap add --alias dev           # Add account and give it a short alias
cswap remove 2                  # Remove an account
cswap disable 2                 # Hold an account out of auto-rotation (keeps its login)
cswap enable 2                  # Return a disabled account to rotation
cswap alias 2 dev               # Give an account a short alias (usable anywhere NUM|EMAIL is)
cswap alias 2 --unset           # Remove an account's alias
cswap alias                     # List all aliases
cswap move 2 1                  # Assign an account to a slot (relocates to an empty slot, swaps if taken)
cswap unclaimed                 # List stashed credential entries (slot + why they were stashed)
cswap unclaimed --purge ID      # Drop one (deletes its bytes; recover with /login + `cswap add`)
cswap tui                       # Interactive dashboard (also: bare `cswap`)
cswap watch                     # Dashboard, opened on the live watch page
cswap tui --auto                # Dashboard, opened on the auto-switch view, LIVE
cswap upgrade                   # Upgrade claude-swap to the latest version
cswap purge                     # Remove all claude-swap data
```

The original flag spellings (`cswap --switch`, `cswap --list`, ...) keep working.

## Tips

- **Do you need to restart after switching?** Usually not. On **Linux and Windows**, credentials are stored in a file and Claude Code re-reads them whenever that file changes, so the new account takes effect on your next message — no restart needed. On **macOS**, credentials live in the Keychain, which Claude Code caches for about 30 seconds; a running session picks up the switch once that cache expires. Restart Claude Code (or close and reopen the VS Code extension tab) only if you want the change to apply instantly.
- **Continuing sessions after switching:** You can keep using the same Claude Code session after switching — run `cswap switch` in any terminal and carry on. If you'd prefer a clean start, close and reopen Claude Code (or the VS Code extension tab) and use `--resume` to pick your previous session. Either way, the first message on the new account may use extra usage as its conversation cache rebuilds.

## How it works

- Backs up OAuth tokens and config when you add an account
- Swaps only the account-specific Claude login when you switch accounts;
  live account-independent OAuth state (such as MCP server logins) is
  preserved instead of being overwritten by a slot's older snapshot
- Account credentials stored securely using platform-appropriate methods
- Switches (manual and automatic) hold Claude Code's own credential locks while writing, so a swap never interleaves with a token refresh
- Auto-switch freshens a target's token before activating it, and quarantines accounts whose refresh token has died (recover by re-adding it with `cswap add --slot N`, or by replacing its stored credentials from a known-good export — a plain `cswap import backup.cswap` replaces dead-token slots automatically)
- Usage numbers refresh every few minutes — faster for an account being used or close to switching, slower for idle ones — keeping cswap comfortably inside Anthropic's rate limits however many dashboards you keep open on a machine. An age note like `· 6m ago` just means the next scheduled check hasn't come yet, not that something is stuck.

## Data locations

| Platform | Credentials | Config backups |
|----------|-------------|----------------|
| Windows | File-based (inside the backup directory, under `credentials/`) | `~/.claude-swap-backup/` |
| macOS | macOS Keychain | `~/.claude-swap-backup/` |
| Linux / WSL | File-based (inside the backup directory, under `credentials/`) | `${XDG_DATA_HOME:-~/.local/share}/claude-swap/` |

Session-mode profiles (`cswap run`) live under the backup directory in `sessions/`. Tool preferences (`settings.json`) and auto-switch state (`autoswitch_state.json` — cooldown and quarantined accounts; delete it to reset) live in the backup directory root, alongside `claude-swap.log` (1 MB, 3 rotations) — where to look for a warning the terminal scrolled away, such as one printed just before `cswap run` launched Claude.

On Linux/WSL, set `XDG_DATA_HOME` to override the default location.

## Menu bar (macOS)

<details>
<summary>Optional macOS menu bar app — usage at a glance, click to switch</summary>

Needs the `menubar` extra (macOS only):

```bash
uv tool install 'claude-swap[menubar]'   # or: pipx install 'claude-swap[menubar]'
cswap menubar
```

Shows every account's 5h / 7d / spend usage and switches with a click (specific / rotate / best / next-available), plus the TUI's add / disable-enable / remove / refresh actions. Enable *Settings → Auto-switch accounts* to run the same engine as [`cswap auto`](#automatic-switching) in the background; it shares the `autoswitch.*` settings, so the menu bar and CLI stay in sync. Off until you turn it on.

**Keep it running without a terminal.** `cswap menubar` runs in the foreground, so the status item dies with the terminal that started it and does not come back after a reboot. `--install-service` hands it to launchd instead — starts at login, restarts on crash, no `.app` bundle:

```bash
cswap menubar --install-service     # start now, and at every login
cswap menubar --service-status      # installed? loaded? pid?
cswap menubar --uninstall-service   # stop it and remove the plist
```

The agent lives at `~/Library/LaunchAgents/com.cswap.menubar.plist` and logs to `~/Library/Logs/com.cswap.menubar.{log,err}`. It pins the `cswap` console script, whose path survives an upgrade — but the running process keeps the old build until it restarts, so after `cswap upgrade` either re-run `--install-service` or `launchctl kickstart -k gui/$(id -u)/com.cswap.menubar`.

</details>

## Cloud pin (Remote Control / Artifacts)

<details>
<summary>Keep Remote Control and Artifacts on one account while inference follows the swap</summary>

Needs the `pin` extra, and an account to point it at:

```bash
uv tool install 'claude-swap[pin]'   # or: pipx install 'claude-swap[pin]'
cswap list                           # the numbers come from here
cswap pin 2
```

Swapping accounts moves *everything*, including two things that are not inference:

- **Remote Control** — a session's owner is fixed at creation by whichever bearer created it, so after a swap the phone/web loses the session and ghosts pile up on the old account.
- **Artifacts** — owned by the publishing bearer, so a republish 403s and the artifact "disappears" from the account you are logged into.

The pin keeps those on one account of your choosing while `cswap switch` / [`cswap auto`](#automatic-switching) keep steering inference. `/v1/messages` is never touched, so usage still bills the account you swapped onto.

```bash
cswap pin 2          # Remote Control / artifacts → account 2
cswap pin            # show the current pin
cswap pin --clear    # remove it
cswap pin --heal     # restart a pin proxy that died, or unwire it
cswap pin --get_port # the serving port, bare digits (exit 1 if none)
cswap pin --get_certdir # the cert directory, a bare path
cswap pin --set_port N   # serve on N from the next start (0 = dynamic)
cswap pin --ensure   # repair a stale wiring before a launch, for rc hooks
```

`--get_port` exists so scripts stop reading our files: a pinned session's
`HTTPS_PROXY` names the pin's own dynamic port, and without a way to ask for it
the on-disk layout becomes a compatibility surface. It prints bare digits and
probes the port first, so a stale record cannot report a dead daemon as live.

The pinned account is re-read per request, so re-pinning takes effect without restarting anything. The one thing a re-pin cannot move is a Remote Control session that is **already open** — the server fixed its owner when the session was created, so reconnecting inside it (`/rc` → Disconnect → `/rc`) is what moves it.

If the pin's daemon dies, `.claude.json` keeps naming its dead port and every new session inherits it at boot — reach for `--heal` to restart the daemon, or, if it can't come back, remove the wiring so sessions fall back to what they had before the pin.

Implemented in [cswap-pin](https://github.com/codeslake/cswap-pin), which the extra pulls in.

</details>

## Advanced

### Configuration

Tool preferences live in `settings.json` in the backup root; `cswap config` reads and edits it with validation, so you never have to find the file or guess valid ranges.

<details>
<summary>Commands & usage</summary>

```bash
cswap config                              # list effective settings ("(default)" = not set)
cswap config get autoswitch.threshold
cswap config set autoswitch.threshold 80  # validated: rejects out-of-range values loudly
cswap config set autoswitch.model Fable   # per-model switching (see "auto"); Fable,Opus for several
cswap config set autoswitch.creditThreshold 100  # switch point for accounts with usage credits left
cswap config set creditCaps.you@example.com 500  # a setup-token account's monthly credit cap, in US dollars
cswap config set creditBalances.you@example.com 70  # the account's purchased credit balance now, in US dollars
cswap config unset autoswitch.threshold   # back to the default
cswap config path                         # where settings.json lives
```

`cswap config --help` lists every key with its valid range and default. Hand-editing the file still works — `cswap config` is just a safer front door. `list` and `get` take `--json` for scripting.

</details>

### Backup and migration

Move account data between machines or back it up:

```bash
cswap export backup.cswap                    # All accounts to a file
cswap export backup.cswap --account 2        # One account
cswap export backup.cswap --full             # Include full ~/.claude.json and credential object (same-PC backup)
cswap import backup.cswap                    # Skips accounts that already exist
cswap import backup.cswap --force            # Overwrite existing
```

The export file is plaintext JSON and, by default, carries only each account's own login — machine-shared MCP/plugin OAuth tokens and the device token stay on the source machine (`--full` keeps everything, for same-PC backups). If you need encryption, pipe through your tool of choice (e.g. `cswap export - | gpg -c > backup.gpg`).

If an imported account is the one you're currently logged in as, activate the imported credentials with `cswap switch N --force` (a plain `switch` to the current account is a safe no-op and won't touch the import).

### Share usage readings between machines

Machines that hold the same accounts can end up spending one usage-endpoint budget: when they share a login (moved between them with `export`/`import`, so the same token is live on each), or when the account's usage requests are limited per account rather than per token. Their polling then adds up. `import-usage` lets one machine poll and hand its readings to the others:

```bash
cswap list --json | ssh laptop cswap import-usage - --hold 600
```

<details>
<summary>How it works — matching, holds & when a hold ends</summary>

The input is `cswap list --json` output. Each row with `usageStatus: "ok"` is matched to a local account by email and organization, and adopted when it is newer than the reading already stored. Its age comes from `usageAgeSeconds`, so the two machines' clocks never have to agree; a script that delays the hand-over should add the delay to that field. `--hold SECONDS` keeps every collector on the receiving machine (`list`, `status`, `auto`, the dashboard, the menu bar) from fetching those accounts for that long, and the held reading stays trusted for switch decisions meanwhile. A hold never runs past the reading's earliest window reset (per-model windows included), nor past an hour after the reading was taken. Renew it with each hand-over, or lift it early with `--hold 0`; when it lapses, the machine goes back to fetching for itself.

</details>

### JSON output for scripting

Add `--json` to `list`, `status`, or `switch` to emit a single machine-readable JSON object on stdout (human-readable notices go to stderr). Useful for scripting auto-swap and quota tracking.

```bash
cswap list --json                   # all accounts with usage/quota
cswap status --json                 # current active account
cswap switch --strategy best --json # switch, then report the result
cswap switch 2 --json
```

<details>
<summary>Example output & schema notes</summary>

```json
{
  "schemaVersion": 1,
  "activeAccountNumber": 2,
  "accounts": [
    { "number": 2, "email": "you@example.com", "active": true, "usageStatus": "ok",
      "usage": { "fiveHour": { "pct": 25.0, "resetsAt": "2026-06-22T23:29:59Z" },
                 "sevenDay": { "pct": 16.0, "resetsAt": "2026-06-26T17:59:59Z" } } }
  ]
}
```

Every payload carries a `schemaVersion` (currently `1`); on a handled error stdout is `{"schemaVersion":1,"error":{...}}` with a non-zero exit code. `--switch`/`--switch-to` report `{"switched": true|false, "from": …, "to": …, "reason": …}`.

Add `--read-only` to `list` or `status` to read the store exactly as the last pass left it: no usage fetch, no login adopt, no unclaimed-stash sweep — the right flag for a statusline or monitor probe that just wants `activeAccountNumber`/`active` without spending a refresh grant.

Usage is served from a per-account cache: when the usage API is briefly unreachable, the last-known numbers are shown instead of nothing (the human view marks them with their age, e.g. `· 2m ago`). Rows with decision-trusted usage carry additive `usageFetchedAt`/`usageAgeSeconds` fields telling you how old the measurement is. Whenever `usage` is null but a last-known measurement exists — data too old to drive a decision (`usageStatus` stays `unavailable`), or a row in a non-`ok` state such as `token_expired` — additive `lastGoodUsage`/`lastGoodFetchedAt`/`lastGoodAgeSeconds` fields preserve the human display without making the account actionable. When `usage` is null and nothing else explains it (`usageStatus` is `unavailable`), an additive `usageError` names the last fetch failure by kind (e.g. `http-429`, `timeout`) and, while the cache is backing off from it, `usageRetryAt` gives the time of the next attempt. These fields apply to list rows and the managed active row from `status --json`. An account held out of rotation with `cswap disable` carries an additive `"disabled": true` on its row (absent otherwise).

A row carries an additive `loginExpiresAt` (ISO-8601 UTC) when the stored login records when its refresh token expires, which is the moment the slot will need a fresh `/login` and `cswap add --slot N`; a script can warn a few days ahead instead of discovering `relogin_required`. Absent when Claude Code recorded no such date for that login.

Every row also carries `loginKind`: `"oauth"` (a browser login, renewed by `/login`), `"setup-token"` (a one-year `claude setup-token`, renewed by minting a new token and running `cswap add-token --email <email> --slot <n>`) or `"api-key"`. A setup-token carries no expiry of its own, so `add-token` records when it was added (`tokenAddedAt`, carried by `export`/`import`) and the row's `loginExpiresAt` is that time plus 365 days. For a token added before cswap recorded this, stamp the day by hand:

```bash
cswap token-added 2 2026-10-07
```

A setup-token's scope cannot read the usage endpoint (it answers 403), so cswap never asks it for one: the account is measured from the 5h/7d rate-limit headers on its own replies while it is active, and that last reading stays decision-trusted while the account sits idle (a window whose reset has passed since reads 0%). The headers carry no per-model window, so a model window such as `autoswitch.model Fable` is unmeasured for a token account and never blocks it. A token account with no reading at all (never active since it was added, or since the usage store was reset) can be read only by switching onto it, so the auto engine probes it: under every strategy, when the active must leave and no measured account can take the sessions, and under `consume-first` also below the threshold, the same way it probes an account whose weekly reset is unknown. A probed account is not probed again for an hour. A rotation candidate that stays without any usage reading for longer than that hour while the engine is looking for a candidate logs one WARNING naming the account and how long.

Before a switch activates any account, cswap checks that the API still accepts its stored credential. A browser login is checked on the profile endpoint; a setup-token's scope cannot read that endpoint, so its check is one `POST /v1/messages/count_tokens` for a one-character message (it bills no tokens and spends nothing of the 5h or 7d window). A 200 marks the switch `validated`; a 401, or a 403 typed `authentication_error`, refuses the switch and takes the account out of rotation until its token is re-added with `cswap add-token`; any other answer leaves the switch unvalidated and logs a WARNING naming the status.

When an account has usage credits (extra usage) enabled, `usage` carries a `spend` object: `used` (dollars spent this month), `limit` (the monthly cap in dollars), `remaining`, `remainingBasis`, `pct`, `currency` and `limitReached` (the API's own verdict that the cap is hit), plus `resetsAt`/`countdown`/`clock` when the API names a reset. An uncapped account has `limit` and `pct` `null`; no `spend` key means credits are off. `remainingBasis` says what `remaining` measures. `limit`: room under the monthly spend limit (`limit - used`, `null` with no limit), which is not money, because the purchased balance can run out before the limit does. `balance`: money left, from a balance you entered with `cswap config set creditBalances.<email> <dollars>`, and the object then also carries `balanceEnteredAt` (ISO-8601 UTC). The entry records the account's dollars used against its limit at that moment (`usedAtEntry`) and the limit's next reset (`resetsAtEntry`), so `remaining` is the balance less what was spent since, never above `limit - used`, never below zero, and zero whenever the reply says `out_of_credits` or the cap is reached. Entering a balance needs a current dollar reading for the account (for a setup-token account, a `creditCaps` entry and a reply carrying its share used) and is refused naming the account without one. When the monthly limit resets after the entry (the used figure drops below `usedAtEntry`, or the clock passes `resetsAtEntry`) the spend since entry is the new month's `used`, and one INFO line notes it; a balance that computes below zero while the credits are still allowed is named at WARNING once per account, since it means the entered balance or its reading was wrong. A fraction spend's `remainingBasis` is `null`. The human `list` view and the dashboard read `$$:   1%   $70.00 left (balance)` for a balance, `$$:   1%   $591.89 of $600 limit unused` for limit room, `$$: $8.11 used, no cap`, `$$: 100%   cap reached ($600.00)`, or `out of credits` when the reply says so. `reported` says how the figure was measured: `dollars` (the usage endpoint, as above) or `fraction`. A setup-token account cannot read the usage endpoint, so its spend comes from the `anthropic-ratelimit-unified-overage-*` headers on its own replies, which give only a share of the monthly cap: `used`, `limit`, `remaining` and `currency` are `null`, `pct` is the share of the cap used (`null` when the reply sent none), `limitReached` is true when the reply says the credits are refused, and `disabledReason` names why (e.g. `out_of_credits`, else `null`). The `list` view reads `$$: credits on, 0% of cap used` or `$$: credits on, out of credits`. Only setup-token accounts are read this way; a browser-login account's spend comes from the usage endpoint alone. Because the replies never name the cap itself, configure it per account in US dollars with `cswap config set creditCaps.<email> <dollars>` (a positive number; the key is the account's whole email after `creditCaps.`, matched without case). With a cap configured, the account's spend is reported as `dollars`: `used` is the share times the cap, `limit` the cap, `remaining` the cap minus `used`, `currency` `USD`, `limitReached` as the reply said, plus `capSource: "config"` and `disabledReason`, so a reader can tell these dollars come from your configured cap and not from the API. The `list` view then reads `$$:   0%   $500.00 of $500 limit unused`, and the wind-down notice names the account's dollars. The cap is applied when the store is read, so changing it takes effect at once. A setup-token account with credits on and no cap configured stays a `fraction` spend, and a WARNING names it and the key to set, once per account per process.

Every row carries `switchThreshold`, the utilization percent the auto engine switches that account away at: `autoswitch.creditThreshold` while its stored reading has usage-credit room, else `autoswitch.threshold`, both read from `settings.json` (a `--threshold` flag given to a running `cswap auto` is not visible here).

An account row also carries an additive `alias` field once one is set with `cswap alias` (e.g. `"alias": "dev"`); accounts without one simply omit the key.

Weekly windows (`sevenDay` and per-model `scoped` entries — never `fiveHour`) additively carry pace fields once the week is ~a day old: `expectedPct` (where usage would sit if spread evenly across the week) and `aheadOfPace` (`true` when meaningfully above that — the same signal the human views show as an `(ahead)`/`(ahead of pace)` marker). `projectedExhaustionAt`/`willLastToReset` extrapolate the current rate into an ETA to 100% and a yes/no "will it last to the reset"; they stay `--json`-only since a linear projection is too rough to present as fact in the UI.

</details>

`cswap auto --json` emits an event *stream* instead — one JSON object per line (`{"schemaVersion":1,"event":"switch","ts":…, …}` with kinds like `poll`, `switch`, `no-switch`, `account-quarantined`, `all-exhausted`, `error`). The contract is additive: new kinds and fields may appear, so scripts should ignore unknown ones.

### Add an account from a raw token or API key

If you only have a long-lived setup-token (e.g., produced by `claude setup-token`)
or a managed API key (`sk-ant-api...`) and you don't want to log in via the browser
flow first — useful on headless servers or when receiving a token from another
machine — register it directly. The token type is auto-detected:

```bash
cswap add-token sk-ant-oat01-...             # OAuth setup-token
cswap add-token sk-ant-api03-...             # managed API key
cswap add-token sk-ant-oat01-... --slot 3
cswap add-token - --slot 3                   # read token from stdin
cswap add-token --email user@example.com     # optional label override
```

`--email` is optional; omitted values use `setup-token-{slot}@token.local`
(or `api-key-{slot}@token.local` for API keys). No Anthropic API calls are made.

**API-key accounts.** An `sk-ant-api...` value registers a managed API-key account
(the kind Claude Code uses after `/login` with a key) rather than an OAuth
setup-token. It switches like any other account; since API keys have no subscription
quota, they show no usage and the usage-aware `switch` strategies never skip them as
rate-limited.

## Uninstall

Remove all data:

```bash
cswap purge
```

Then uninstall the tool:

```bash
uv tool uninstall claude-swap
# or
pipx uninstall claude-swap
```

## Requirements

- Python 3.12+
- Claude Code installed and logged in

## License

MIT
