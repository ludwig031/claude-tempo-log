#!/usr/bin/env bash
# SessionStart hook: create per-session state and instruct Claude to
# collect the task ID and maintain the session topic.
set -euo pipefail

STATE_DIR="${TEMPO_STATE_DIR:-$HOME/.claude/tempo-sessions}"
PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

input="$(cat)"
session_id="$(jq -r '.session_id // empty' <<<"$input")"
cwd="$(jq -r '.cwd // empty' <<<"$input")"

[ -n "$session_id" ] || exit 0

mkdir -p "$STATE_DIR"
state="$STATE_DIR/$session_id.json"

# On resume/compact the state from the original start must survive.
if [ ! -f "$state" ]; then
  jq -n \
    --arg start "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg cwd "$cwd" \
    --arg project "$(basename "${cwd:-unknown}")" \
    '{start: $start, cwd: $cwd, project: $project, task_id: "", topic: ""}' \
    > "$state"
fi

task_id="$(jq -r '.task_id // empty' "$state")"
tempo_task="$PLUGIN_ROOT/scripts/tempo-task"

if [ -n "$task_id" ]; then
  task_instruction="1. TASK ID: already recorded for this session ($task_id) — do not ask again unless the user says the task changed; then re-record it with:
   $tempo_task $session_id task <TASK_ID>"
else
  task_instruction="1. TASK ID (mandatory, first thing): before doing any work, ask the user which task ID this session is for (e.g. ABC-123) — unless their message already contains one, in which case it is captured automatically and you will be notified. As soon as you know it, record it:
   $tempo_task $session_id task <TASK_ID>"
fi

context="[claude-tempo-log] Work-hours logging is active for this session (session_id: $session_id).
$task_instruction
2. TOPIC: once the first substantive request is clear, record a one-line topic in the user's language:
   $tempo_task $session_id topic \"<short description of what is being worked on>\"
   If the focus of the session changes later, update the topic the same way.
Run these via the Bash tool as part of your normal work; keep it low-key (a short confirmation to the user is enough)."

jq -n --arg ctx "$context" \
  '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $ctx}}'
