# cswap-quota

`cswap-quota` shows every claude-swap account's usage on one screen: each account's 5-hour and
weekly use in the order the switcher will use them, how fast the whole pool is being spent compared
with what the plans give, when that pace runs the pool dry, and anything that needs action.

It ships with a Claude Code mod that adds `/quota` (and keeps the short form above the prompt), and
a skill for setups without mods.

## What it shows

Sample output (made-up numbers):

```
Claude usage · Thu 10:47 AM CEST

| Account | 5-hour | Week · resets CEST |
|---|---|---|
| **now** | 31.0% · 2h 12m | 64.0% · Sat 9 AM |
| next | 0.0% | 12.0% · Mon 2 PM |
| full | 8.0% · 4h 40m | 99.0% · Fri 11 PM |
| unread | | unknown: last fetch http-503 |

**Week: runs out**
- Using 61.5%/day vs plan 42.4%/day (+45.0%)
- Out from Sun 6:30 PM CEST for 1d 3h

**5-hour: lasts**
- Using 9.2%/hour vs plan 60.0%/hour (-84.7%)

Counted over 3 of 4 accounts; 1 not read now.

**Needs action**
- #3: 7d at the switcher's 99% threshold, resets in 1d 12h; its remaining 1.0% expires unused unless `! cswap switch 3`
```

- **The table** lists accounts by their place in the switcher's order, not by number or email:
  `now` is the active account, `next` and `then` can take work now, `full` is held at the
  switcher's threshold until a reset, `unread` has no current figure. With the `consume-first` or
  `dynamic` strategy the usable accounts are ordered by soonest weekly reset; with `best`, by most
  headroom (100 minus the higher of the account's two windows). Under `dynamic` the switcher
  actually moves at about 97% whatever `autoswitch.threshold` says; this tool uses the configured
  threshold for the plan and for `full`, so under `dynamic` an account can show as usable a few
  points after the switcher has left it.
- **The week and 5-hour verdicts** compare the pool's spending rate with its plan and say whether
  the pool runs dry before it renews. Only the first dry stretch is shown; the table's reset times
  say when room comes back.
- **Needs action** lists conditions such as a week stuck at the threshold that will expire unused,
  extra-usage spend near its monthly cap, a login about to expire, or an account that needs a
  fresh login.
- A figure older than 15 minutes, or one cswap no longer trusts, is shown with the time it was read
  and is never used for the pace or the conditions.

`cswap-quota --lines` prints a short plain-text form, one line per account (`*` marks the active
one), then the pace, then the conditions.

All times are in the machine's local time zone.

## Install

Requires Python 3.10 or newer and `cswap` on `PATH`. No extra packages.

```sh
cp cswap_quota.py ~/.local/bin/cswap-quota
chmod +x ~/.local/bin/cswap-quota
cswap-quota   # check it runs
```

Then pick one way to get `/quota` inside Claude Code:

- **Mod** (adds `/quota`, prints the table under an answer every 30 minutes, and keeps the short
  form above the prompt): copy `mod/` somewhere, for example `~/.claude/mods/quota`, and add that
  directory to `CLAUDE_CODE_PLUGIN_DIRS`. The interval is the mod's `intervalMinutes` setting.
  Run its tests with `cd mod && claude plugin test .`.
- **Skill** (on demand only): copy `skill/` to `~/.claude/skills/quota/`.

## How the rate and run-out are worked out

A point is one percent of one account's window.

- **The rate.** Each account's figure only grows until its window resets, so the rise between two
  readings of the same window is what was spent between them. Each account's current window, from
  its opening to now, is one measurement of what the whole pool spent over that span; the
  measurements are combined weighted by their length. On a first run there are no earlier readings,
  so each account's current figure counts as spent since its window opened, and the rate is usable
  from the start; more readings make it sharper.
- **The plan.** What the pool renews at: each account's weekly window up to the switcher's
  threshold per 7 days, and each account's full 5-hour window per 5 hours. Every account counts the
  same, because cswap reports percentages of each account's own plan.
- **The run-out.** The rate is run forward over each account's remaining room, refilling an account
  at its reset. The first time no account has room left is when the pool runs dry; it stays dry
  until the next reset refills an account. When it never runs dry, the room that expires unspent
  at the resets is shown instead.

## Where history is kept

Each run appends one short line per account with a current figure (the account email, each
window's reset time and percentage, and when it was read) to:

1. `$CSWAP_QUOTA_HISTORY` if set, else
2. `$XDG_STATE_HOME/cswap-quota/history.jsonl` if `XDG_STATE_HOME` is set, else
3. `~/.local/state/cswap-quota/history.jsonl`

Lines older than 8 days are dropped on each write. Deleting the file only resets the rate to the
first-run estimate.

## Network and failure behaviour

`cswap-quota` makes no network calls of its own: it only runs `cswap list --json` and
`cswap config get` for the threshold and strategy. If `cswap` is missing or fails, its error is
printed to stderr and the exit status is non-zero; no stale or made-up output is printed.

## Tests

```sh
pytest test_cswap_quota.py
```
