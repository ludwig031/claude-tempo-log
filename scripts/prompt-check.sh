#!/usr/bin/env bash
# UserPromptSubmit hook: enforce the task ID.
# - Auto-captures a task key when the prompt starts with one or is exactly one
#   (e.g. the user replies "CED-2545" to Claude's question).
# - Until a task ID is recorded, injects a mandatory reminder with every prompt.
set -euo pipefail

STATE_DIR="${TEMPO_STATE_DIR:-$HOME/.claude/tempo-sessions}"
TASK_REGEX="${TEMPO_TASK_REGEX:-[A-Z][A-Z0-9]+-[0-9]+}"
# Uppercase prefixes that look like task keys but never are.
TASK_IGNORE="${TEMPO_TASK_IGNORE:-UTF|ISO|SHA|MD|RFC|CVE|HTTP|TLS|GPT}"
PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

input="$(cat)"
session_id="$(jq -r '.session_id // empty' <<<"$input")"
prompt="$(jq -r '.prompt // empty' <<<"$input")"

[ -n "$session_id" ] || exit 0

state="$STATE_DIR/$session_id.json"
task_id=""
[ -f "$state" ] && task_id="$(jq -r '.task_id // empty' "$state")"

# Task ID already known: stay silent (zero context overhead).
[ -z "$task_id" ] || exit 0

# Reporting commands are internal work: record that instead of asking.
if printf '%s' "$prompt" | grep -qE '(^[[:space:]]*|<command-name>)/claude-tempo-log:(timesheet|report)'; then
  "$PLUGIN_ROOT/scripts/tempo-task" "$session_id" task "${TEMPO_INTERNAL_TASK:-INTERNAL}" >/dev/null
  "$PLUGIN_ROOT/scripts/tempo-task" "$session_id" topic "Tempo report / timesheet" >/dev/null
  exit 0
fi

# Auto-capture only from the start of the prompt, to avoid false positives
# mid-sentence (SHA-256, ISO-8601, ...).
found="$(printf '%s' "$prompt" | grep -oE "^[[:space:]]*($TASK_REGEX)" | head -1 | tr -d '[:space:]' || true)"
if [ -n "$found" ] && ! grep -qE "^($TASK_IGNORE)-" <<<"$found"; then
  "$PLUGIN_ROOT/scripts/tempo-task" "$session_id" task "$found" >/dev/null
  jq -n --arg ctx "[claude-tempo-log] Task ID $found was auto-captured from the user's message and recorded for this session. Do not ask for it again. If it is obviously not a task ID, correct it with: $PLUGIN_ROOT/scripts/tempo-task $session_id task <TASK_ID>" \
    '{hookSpecificOutput: {hookEventName: "UserPromptSubmit", additionalContext: $ctx}}'
  exit 0
fi

jq -n --arg ctx "[claude-tempo-log] MANDATORY: no task ID is recorded for this session yet. Before executing the user's request, ask them for the task ID and record it with: $PLUGIN_ROOT/scripts/tempo-task $session_id task <TASK_ID>" \
  '{hookSpecificOutput: {hookEventName: "UserPromptSubmit", additionalContext: $ctx}}'
