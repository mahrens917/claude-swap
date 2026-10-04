---
name: quota
description: Show every claude-swap account's usage, the pool's pace and where it runs out, by running cswap-quota and printing its output unchanged.
allowed-tools: Bash(cswap-quota)
---

Run `cswap-quota` with the Bash tool and no arguments.

- If it exits 0, reply with its standard output exactly as printed: no preface, no summary, no
  comment, and no code fence around it (it is markdown and should render as such).
- If it exits non-zero, reply with its standard error exactly as printed, and nothing else.
