---
description: Tempo-ready timesheet for a day or week from sessions, Slack, meetings and browser history
argument-hint: "[DAY | --week [DATE] | --from DATE --to DATE]"
---

Build the Tempo timesheet for: `$ARGUMENTS` (empty = current week; a bare date = that day; dates may be YYYY-MM-DD or DD-MM-YYYY; words like "last week" or "jučer" → turn them into `--week DATE` or a date yourself). Do not ask for a task ID or for more explanation. Never write to Tempo or Jira, never post to Slack. Talk to the user in their language; slot descriptions are English.

1. **Collect.** Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/timesheet_collect.py" collect $ARGUMENTS` and read the `brief.md` it names (`RUN_DIR=` line). For a slot whose excerpts are not enough, run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/timesheet_collect.py" detail --run <RUN_DIR> <ID> ...`.

2. **Slack.** If the Slack MCP is connected: search with `filters: "from:<@MY_USER_ID> after:<day before start> before:<day after end>"` (your user ID is in the Slack search tool description), `sort: timestamp`, `sort_dir: asc`, `response_format: concise`, `limit: 20`, and follow every cursor to the end. Keep only work messages inside the period (local time). Map channel/DM + content to a project and task. Ignore private chat. If Slack is not connected, add a note and continue.

3. **Decide, per day.** Use only evidence, never guess durations:
   - Claude/Codex slots: describe what was done **in that slot** from its prompts plus commits and Slack in the same time range (never from the session topic alone; one session can cover different jobs on different days). Fix wrong or missing keys (`???`, placeholders like PROJ-123, unrelated keys) from content; split a slot with `ranges` when it covers two jobs. Drop slots that are not work.
   - Meetings (M): assign the right key (calendar title, generic buckets from the brief). The window-focus time is the real attendance.
   - Slack: consecutive work messages on one topic with gaps of at most 30 minutes form one interval (first to last message). A lone message adds no time unless it clearly shows a work block (then at most 15 min). Slack, browser and delegated agent runs only fill empty time.
   - Browser: you may read every host to tell dev/preprod/prod apart and to find project work, but only project-related hosts may become evidence or text. Never mention personal browsing.
   - Entries listed under "Already in Tempo" must not be repeated.
   - Internal tooling work without a task key gets task `INTERNAL`.

4. **Validate keys.** With the Jira MCP (`searchJiraIssuesUsingJql`, `key in (...)`, fields `summary,status`; cloudId from the brief config or `getAccessibleAtlassianResources`) check every key you will output, plus the keys you replaced. Record them in `annotations.json` → `jira`.

5. **Write `<RUN_DIR>/annotations.json`** (format in the docstring of `scripts/timesheet_merge.py`): `evidence` (per ID: task, desc, reason, ranges, drop), `items` (Slack and other extra intervals with `kind` meeting/work/slack/browser), `jira`, `notes` (anything the user must check, e.g. a key you were not sure about). Descriptions: short, concrete English (what was built, fixed, decided), no names of private people, max ~180 chars.

6. **Merge.** Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/timesheet_merge.py" --run <RUN_DIR>`. Read the result. If a final slot's description is a clumsy join of fragments, add a better one under `final` (`"TASK|DATE|HH:MM"` = slot start) and run the merge again. Do not change times by hand; change evidence or items instead.

7. **Answer** with the content of `report.txt` verbatim in one code block, then at most three short lines: open questions (e.g. `???` slots) and the path of `logger-notes.txt` (ready to paste into the Netgen Time Logger chat).
