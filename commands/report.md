---
description: Izvještaj odrađenih sati po danu i task ID-u za unos u Tempo
---

Run this command with the Bash tool and show the user its output as a readable table (do not reformat the numbers):

```
"${CLAUDE_PLUGIN_ROOT}/scripts/report.sh" $ARGUMENTS
```

Supported flags, if the user asked for a period in words map it to these:
- (no flags) — today
- `--date YYYY-MM-DD` — a specific day
- `--week` — last 7 days
- `--all` — entire log
- `--slots` — additionally list each individual time slot

The "Tempo" column is rounded to 15 minutes and is what the user enters into Tempo; "Točno" is the exact measured time.
