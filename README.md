# claude-tempo-log

A Claude Code plugin for agency time tracking. Every Claude Code session is automatically logged with its **task ID, conversation topic and real working time** — ready to enter into [Tempo](https://www.tempo.io/) (or any other time-tracking tool).

## What it does

- **Enforces a task ID**: at the start of a session Claude asks you for the task ID and keeps reminding you with every prompt until you provide one. If a prompt starts with a task key (e.g. `CED-2545: fix the login form`), it is captured automatically without asking.
- **Records the topic**: while working, Claude writes a one-line topic of the session and updates it if the focus changes.
- **Measures real time, not wall-clock**: a session open from 10:00 to 22:00 is not 12 hours of work. Transcript activity is cut into **time slots**: a silence longer than 30 minutes opens a new slot, shorter gaps count as work. Three blocks of work during a day = three slots with realistic minutes.
- **Log**: `~/.claude/work-log.jsonl` — one line per slot (date, task ID, topic, project, start/end, minutes). All data stays local and per-user; the plugin never sends anything anywhere.
- **Never loses a session**: every report first runs a **sweep** that rebuilds the slots of *all* sessions straight from the raw transcripts. Sessions that are still running, or that died without a clean exit (closed tab, killed terminal, dropped SSH), are therefore always included — nothing depends on the SessionEnd hook firing.

## Installation

Prerequisites: `jq` and `python3` (standard on Linux/macOS).

### From GitHub

In Claude Code:

```
/plugin marketplace add <github-user>/claude-tempo-log
/plugin install claude-tempo-log@tempo-tools
```

Pull newer versions with `/plugin marketplace update tempo-tools`.

### From a local directory (development)

```
/plugin marketplace add /path/to/claude-tempo-log
/plugin install claude-tempo-log@tempo-tools
```

After installing, open a new session — Claude will immediately ask for the task ID.

## Reports

```
/claude-tempo-log:report                          # current calendar week (Mon–Sun)
/claude-tempo-log:report --today                  # today only
/claude-tempo-log:report --date 2026-07-08        # a specific day
/claude-tempo-log:report --week 2026-07-01        # the week containing that date
/claude-tempo-log:report --from 2026-07-01 --to 2026-07-04   # arbitrary range
/claude-tempo-log:report --all                    # entire log
/claude-tempo-log:report --slots                  # + individual slots
/claude-tempo-log:report --timeline               # + per-day timelines in multi-day reports
/claude-tempo-log:report --no-chart               # tables only
```

or directly in a shell: `scripts/report.sh --week`

The **Tempo** column is the daily sum per task rounded to 15 minutes (that is what you enter into Tempo); **Exact** is the measured time for reference. Work shorter than 15 minutes rounds up to 15 so it never disappears.

A single-day report draws a per-task timeline of the day; a multi-day report draws hour bars per day and per task:

```
Day timeline (one cell = 15 min)
              09  10  11  12  13  14  15  16
CED-2545      ████████········████
IRB-12        ············████····████████

Hours per day (one █ = 15m)
Mon 2026-07-06  █████████████ 3h 15m
Tue 2026-07-07  ████████ 2h

Hours per task (one █ = 15m)
CED-2545      ██████████ 2h 30m
IRB-12        ███████ 1h 45m
```

Note: parallel sessions on multiple projects are each counted in full (slots may overlap in time) — every task gets its complete time.

## Slot rules

1. A slot opens with activity after a silence longer than 30 minutes.
2. A gap ≤ 30 minutes between any activity (your prompt, Claude's response, a tool call) stays inside the slot and counts as work.
3. Slot duration = last activity − first activity of the slot.
4. Long autonomous work by Claude stays one slot (the transcript fills continuously).
5. A session without a single user prompt is not logged; `/clear` and resume never duplicate entries (a session's slots are always recomputed and replaced).
6. The sweep (`scripts/sweep.py`, run automatically before every report) recomputes every transcript with the same rules, so live and abruptly-killed sessions are logged too. Task IDs and topics already recorded are preserved.

## Configuration (environment variables)

| Variable | Default | Description |
|---|---|---|
| `TEMPO_GAP_MIN` | `30` | Silence (minutes) that opens a new slot |
| `TEMPO_TASK_REGEX` | `[A-Z][A-Z0-9]+-[0-9]+` | Task key format (JIRA style) |
| `TEMPO_TASK_IGNORE` | `UTF\|ISO\|SHA\|MD\|RFC\|CVE\|HTTP\|TLS\|GPT` | Prefixes never captured as a task ID |
| `TEMPO_LOG_FILE` | `~/.claude/work-log.jsonl` | Log location |
| `TEMPO_STATE_DIR` | `~/.claude/tempo-sessions` | Temporary per-session state |

## Layout

```
.claude-plugin/plugin.json       # plugin manifest
.claude-plugin/marketplace.json  # the repo doubles as the "tempo-tools" marketplace
hooks/hooks.json                 # SessionStart / UserPromptSubmit / SessionEnd
scripts/session-start.sh         # state + instructions to Claude (task ID, topic)
scripts/prompt-check.sh          # task ID enforcement + auto-capture
scripts/tempo-task               # helper Claude uses to record task/topic
scripts/session-end.sh|.py       # transcript → time slots → work-log.jsonl
scripts/sweep.py                 # rebuild all sessions from transcripts (live/killed too)
scripts/report.sh|.py            # sweep + Tempo report with timeline/bar charts
commands/report.md               # /claude-tempo-log:report
```
