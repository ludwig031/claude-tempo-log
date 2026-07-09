#!/usr/bin/env python3
"""SessionEnd hook body: turn a session transcript into work-time slots.

Slot rules (the whole point of this plugin):
- a slot opens with activity after a silence longer than GAP_MIN minutes
- a gap of <= GAP_MIN between any activity (user prompt, Claude response,
  tool use) and the next one stays inside the slot and counts as work time
- slot duration = last activity - first activity of the slot

Rows are keyed by the transcript file's UUID: on every SessionEnd all rows
for that transcript are recomputed and replaced, so a session that is
paused/resumed or gets its task ID late never produces duplicates.
"""
import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

GAP_MIN = int(os.environ.get("TEMPO_GAP_MIN", "30"))
STATE_DIR = Path(os.environ.get("TEMPO_STATE_DIR", Path.home() / ".claude" / "tempo-sessions"))
LOG_FILE = Path(os.environ.get("TEMPO_LOG_FILE", Path.home() / ".claude" / "work-log.jsonl"))


def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def is_user_prompt(entry: dict) -> bool:
    if entry.get("type") != "user" or entry.get("isSidechain") or entry.get("isMeta"):
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") == "text" for b in content)
    return False


def read_transcript(path: Path):
    """Return (sorted activity timestamps, number of real user prompts)."""
    timestamps, prompts = [], 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = entry.get("timestamp")
            if not ts:
                continue
            try:
                timestamps.append(parse_ts(ts))
            except ValueError:
                continue
            if is_user_prompt(entry):
                prompts += 1
    timestamps.sort()
    return timestamps, prompts


def build_slots(timestamps):
    """Cluster activity timestamps into slots separated by > GAP_MIN silence."""
    slots = []
    for ts in timestamps:
        if slots and (ts - slots[-1][1]).total_seconds() <= GAP_MIN * 60:
            slots[-1][1] = ts
        else:
            slots.append([ts, ts])
    return slots


def rewrite_log(session_key: str, rows: list):
    """Replace all rows of this transcript in the log, atomically and locked."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_path = LOG_FILE.with_suffix(LOG_FILE.suffix + ".lock")
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        kept = []
        if LOG_FILE.exists():
            with open(LOG_FILE, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        if json.loads(line).get("session") == session_key:
                            continue
                    except json.JSONDecodeError:
                        pass
                    kept.append(line)
        kept.extend(json.dumps(r, ensure_ascii=False) for r in rows)
        tmp = LOG_FILE.with_suffix(".tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        tmp.replace(LOG_FILE)


def main():
    hook_input = json.load(sys.stdin)
    session_id = hook_input.get("session_id") or ""
    transcript = hook_input.get("transcript_path") or ""
    reason = hook_input.get("reason") or ""
    cwd = hook_input.get("cwd") or ""

    state_path = STATE_DIR / f"{session_id}.json"
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            state = {}

    def cleanup():
        # Keep state while the session may continue later.
        if reason != "resume" and state_path.exists():
            state_path.unlink(missing_ok=True)

    if not transcript or not Path(transcript).exists():
        cleanup()
        return

    timestamps, prompts = read_transcript(Path(transcript))
    if prompts == 0 or not timestamps:
        cleanup()
        return

    cwd = state.get("cwd") or cwd
    project = state.get("project") or (os.path.basename(cwd) if cwd else "unknown")
    session_key = Path(transcript).stem

    rows = []
    for start, end in build_slots(timestamps):
        local_start = start.astimezone()
        local_end = end.astimezone()
        rows.append({
            "date": local_start.strftime("%Y-%m-%d"),
            "task_id": state.get("task_id") or "",
            "topic": state.get("topic") or "",
            "project": project,
            "slot_start": local_start.strftime("%H:%M"),
            "slot_end": local_end.strftime("%H:%M"),
            "duration_min": max(1, round((end - start).total_seconds() / 60)),
            "session": session_key,
            "session_id": session_id,
            "cwd": cwd,
        })

    rewrite_log(session_key, rows)
    cleanup()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # a logging failure must never break the session
        print(f"[claude-tempo-log] session-end failed: {exc}", file=sys.stderr)
        sys.exit(0)
