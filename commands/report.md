---
description: Report of worked hours per day and task ID, ready for Tempo entry
---

Run this command with the Bash tool and show the user its output verbatim in a code block (the tables and charts are pre-formatted — do not reformat them):

```
"${CLAUDE_PLUGIN_ROOT}/scripts/report.sh" $ARGUMENTS
```

If the user asked for a period in words, map it to these flags:
- (no flags) — the current calendar week (Mon–Sun)
- `--today` — today only
- `--date YYYY-MM-DD` — a specific day
- `--week YYYY-MM-DD` — the week containing that date
- `--from YYYY-MM-DD --to YYYY-MM-DD` — an arbitrary range (--to defaults to today)
- `--all` — the entire log
- `--slots` — additionally list each individual time slot
- `--timeline` — per-day task timelines in multi-day reports
- `--no-chart` — tables only, without the visual timeline/bars

The "Tempo" column is rounded to 15 minutes and is what the user enters into Tempo; "Exact" is the measured time. Single-day reports include a per-task day timeline; multi-day reports include hours-per-day and hours-per-task bars.
