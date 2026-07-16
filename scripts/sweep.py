#!/usr/bin/env python3
"""Rebuild work-log slots for every session directly from the transcripts.

The SessionEnd hook only fires on a graceful exit, so a killed terminal or a
still-running session never reaches the log. This sweep removes that
dependency: it recomputes the slots of every transcript (same 30-min-gap
clustering as session_end.py) and rewrites the log, so the log always
reflects all sessions — live, killed or finished.

Task ID / topic per session come from the session state file if it still
exists, otherwise from the rows already in the log (a sweep never erases a
task ID that was recorded earlier). Project/cwd fall back to the transcript's
own cwd entries.

Usage:
  sweep.py            sweep all transcripts, print a one-line summary
  sweep.py -v         also list the sessions that changed
"""
import json
import os
import sys
from pathlib import Path

from session_end import STATE_DIR, LOG_FILE, is_user_prompt, make_rows, parse_ts

PROJECTS_DIR = Path(os.environ.get("CLAUDE_PROJECTS_DIR", Path.home() / ".claude" / "projects"))


def load_log():
    """Existing log rows grouped by session key; unparseable/foreign lines kept as-is."""
    by_session, other_lines = {}, []
    if LOG_FILE.exists():
        with open(LOG_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    key = row.get("session")
                except json.JSONDecodeError:
                    row, key = None, None
                if key:
                    by_session.setdefault(key, []).append(row)
                else:
                    other_lines.append(line)
    return by_session, other_lines


def read_deduped(path: Path, seen_uuids: set):
    """Like session_end.read_transcript, but skips entries already seen in an
    earlier transcript: a forked conversation duplicates the shared history
    (same entry uuids, same timestamps), which must not count twice."""
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
            uuid = entry.get("uuid")
            if uuid:
                if uuid in seen_uuids:
                    continue
                seen_uuids.add(uuid)
            try:
                timestamps.append(parse_ts(ts))
            except ValueError:
                continue
            if is_user_prompt(entry):
                prompts += 1
    timestamps.sort()
    return timestamps, prompts


def transcript_cwd(path: Path) -> str:
    """First cwd recorded inside the transcript itself."""
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                cwd = json.loads(line).get("cwd")
            except json.JSONDecodeError:
                continue
            if cwd:
                return cwd
    return ""


def session_meta(session_key: str, existing_rows: list, path: Path) -> dict:
    """task_id/topic/project/cwd: state file wins, then existing log rows, then transcript."""
    meta = {"task_id": "", "topic": "", "project": "", "cwd": ""}
    for row in existing_rows:
        for field in meta:
            if not meta[field] and row.get(field):
                meta[field] = row[field]
    state_path = STATE_DIR / f"{session_key}.json"
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            for field in meta:
                if state.get(field):
                    meta[field] = state[field]
        except (json.JSONDecodeError, OSError):
            pass
    if not meta["cwd"]:
        meta["cwd"] = transcript_cwd(path)
    if not meta["project"]:
        meta["project"] = os.path.basename(meta["cwd"]) if meta["cwd"] else "unknown"
    return meta


def main():
    verbose = "-v" in sys.argv[1:]
    by_session, _ = load_log()

    all_rows, swept_keys, changed = [], set(), []
    seen_uuids = set()
    for path in sorted(PROJECTS_DIR.glob("*/*.jsonl")):
        session_key = path.stem
        timestamps, prompts = read_deduped(path, seen_uuids)
        rows = []
        if prompts > 0 and timestamps:
            meta = session_meta(session_key, by_session.get(session_key, []), path)
            rows = make_rows(timestamps, meta["task_id"], meta["topic"], meta["project"],
                             session_key, session_key, meta["cwd"])
        # swept even when empty: stale rows of prompt-less sessions get removed
        swept_keys.add(session_key)
        all_rows.extend(rows)
        if rows != by_session.get(session_key, []):
            changed.append((session_key, rows))

    if changed:
        # One locked rewrite for all swept sessions; rows of transcripts that
        # no longer exist (or live elsewhere) are left untouched.
        from session_end import rewrite_log
        rewrite_log(swept_keys, all_rows)

    print(f"[claude-tempo-log] sweep: {len(swept_keys)} sessions checked, "
          f"{len(changed)} updated in {LOG_FILE}")
    if verbose:
        for key, rows in changed:
            if not rows:
                print(f"  {key[:8]}  (no real prompts — rows removed)")
                continue
            days = sorted({r['date'] for r in rows})
            total = sum(r["duration_min"] for r in rows)
            print(f"  {key[:8]}  {rows[0]['project']:<20} {days[0]}..{days[-1]}  "
                  f"{len(rows)} slots, {total} min")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # a sweep failure must never break anything
        print(f"[claude-tempo-log] sweep failed: {exc}", file=sys.stderr)
        sys.exit(0)
