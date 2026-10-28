#!/usr/bin/env python3
"""Collect local evidence for a timesheet period.

  timesheet_collect.py collect [DAY | --date D | --week [D] | --from D --to D | --today]
  timesheet_collect.py detail --run DIR ID [ID ...]

`collect` sweeps the work log, then writes evidence.json (full) and brief.md
(compact, for the agent) into ~/.claude/tempo-timesheet/<period>/. Sources:
Claude and Codex session slots (same 30-min gap rule as the work log) with
the prompts typed in each slot, Netgen Time Logger signals (meetings from
window focus, calendar, commits, Jira, recaps, drafts already in Tempo) and
browser history. A missing source is reported, never fatal.

`detail` prints all prompts of the given slot IDs from evidence.json.
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from session_end import build_slots, is_user_prompt, parse_ts  # noqa: E402
from sweep import load_log, read_deduped, session_meta  # noqa: E402
from timesheet_common import (add_period_args, expand, host_matches, load_config,  # noqa: E402
                              resolve_period, run_dir_for, tz_of)

TASK_RE = re.compile(r"\b([A-Z][A-Z0-9]+-[0-9]+)\b")
TASK_IGNORE = re.compile(r"^(UTF|ISO|SHA|MD|RFC|CVE|HTTP|TLS|GPT)-")
SKIP_PROMPT_PREFIXES = (
    "This session is being continued", "Another Claude session sent a message",
    "<task-notification", "Caveat:", "[Request interrupted",
)
CHROME_EPOCH_OFFSET = 11644473600


def iso(dt: datetime, tz) -> str:
    return dt.astimezone(tz).isoformat(timespec="seconds")


def clean_text(text: str) -> str:
    text = re.sub(r"<pasted_content[^>]*>.*?</pasted_content>", "[paste]", text, flags=re.S)
    text = re.sub(r"</?(task|user_query|instructions)[^>]*>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def task_hint(texts, cwd=""):
    for text in list(texts) + [cwd]:
        for key in TASK_RE.findall(text or ""):
            if not TASK_IGNORE.match(key):
                return key
    return ""


def is_dropped(cwd: str, cfg) -> bool:
    cwd = os.path.realpath(os.path.expanduser(cwd)) if cwd else ""
    return any(cwd.startswith(os.path.realpath(os.path.expanduser(p))) for p in cfg["drop_cwd_prefixes"])


def excerpts(prompts, limit):
    """First, last and evenly spaced prompts in between."""
    if len(prompts) <= limit:
        return prompts
    if limit < 2:
        return prompts[:limit]
    idx = {0, len(prompts) - 1}
    step = (len(prompts) - 1) / (limit - 1)
    idx.update(round(i * step) for i in range(1, limit - 1))
    return [prompts[i] for i in sorted(idx)]


def slot_record(sid, source, session, meta, start, end, prompts, tz):
    texts = [t for _, t in prompts]
    return {
        "id": sid, "source": source, "session": session,
        "project": meta.get("project", ""), "cwd": meta.get("cwd", ""),
        "task": meta.get("task_id", ""), "topic": meta.get("topic", ""),
        "task_hint": "" if meta.get("task_id") else task_hint(texts, meta.get("cwd", "")),
        "start": iso(start, tz), "end": iso(end, tz),
        "minutes": max(1, round((end - start).total_seconds() / 60)),
        "prompts": [[p.astimezone(tz).strftime("%H:%M"), t[:500]] for p, t in prompts],
    }


# --- Claude -----------------------------------------------------------------

def claude_prompts(path: Path):
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not entry.get("timestamp") or not is_user_prompt(entry):
                continue
            content = entry["message"]["content"]
            if isinstance(content, list):
                content = " ".join(b.get("text", "") for b in content
                                   if isinstance(b, dict) and b.get("type") == "text")
            text = clean_text(content)
            if text and not text.startswith(SKIP_PROMPT_PREFIXES):
                out.append((parse_ts(entry["timestamp"]), text))
    return out


def collect_claude(cfg, t0, t1, tz, warnings):
    root = expand(cfg["paths"]["claude_projects"])
    if not root.exists():
        warnings.append(f"Claude transcripts not found ({root})")
        return []
    by_session, _ = load_log()
    cutoff = t0.timestamp() - 86400
    seen, slots = set(), []
    for path in sorted(root.glob("*/*.jsonl")):
        if path.stat().st_mtime < cutoff:
            continue
        timestamps, prompt_count = read_deduped(path, seen)
        if not prompt_count or not timestamps or timestamps[-1] < t0 or timestamps[0] >= t1:
            continue
        meta = session_meta(path.stem, by_session.get(path.stem, []), path)
        if is_dropped(meta["cwd"], cfg):
            continue
        prompts = claude_prompts(path)
        for start, end in build_slots(timestamps):
            if end < t0 or start >= t1:
                continue
            inside = [p for p in prompts if start - timedelta(minutes=1) <= p[0] <= end + timedelta(minutes=1)]
            slots.append(slot_record("", "claude", path.stem, meta, start, end, inside, tz))
    return slots


# --- Codex ------------------------------------------------------------------

def codex_prompt_texts(payload):
    texts = []
    for block in payload.get("content") or []:
        text = block.get("text", "") if isinstance(block, dict) else ""
        stripped = text.lstrip()
        if not stripped or stripped.startswith(("<environment_context>", "<user_instructions>",
                                                "# AGENTS.md", "<permissions", ">>> APPROVAL",
                                                "The following is the Codex agent history")):
            continue
        texts.append(clean_text(text))
    return [t for t in texts if t]


def collect_codex(cfg, t0, t1, tz, warnings):
    root = expand(cfg["paths"]["codex_sessions"])
    if not root.exists():
        return []
    slots = []
    day = (t0.astimezone(tz) - timedelta(days=1)).date()
    last = t1.astimezone(tz).date()
    while day <= last:
        for path in sorted((root / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}").glob("*.jsonl")):
            timestamps, prompts, meta = [], [], {}
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        entry = json.loads(line)
                        ts = parse_ts(entry["timestamp"])
                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue
                    timestamps.append(ts)
                    payload = entry.get("payload") or {}
                    if entry.get("type") == "session_meta":
                        meta = payload
                    elif payload.get("type") == "message" and payload.get("role") == "user":
                        prompts.extend((ts, t) for t in codex_prompt_texts(payload))
            cwd = meta.get("cwd", "")
            if not prompts or is_dropped(cwd, cfg):
                continue
            timestamps.sort()
            originator = meta.get("originator", "")
            info = {"cwd": cwd, "project": os.path.basename(cwd) if cwd else "codex",
                    "topic": f"codex ({originator or 'cli'})"}
            delegated = originator in cfg["codex_delegated_originators"]
            for start, end in build_slots(timestamps):
                if end < t0 or start >= t1:
                    continue
                inside = [p for p in prompts if start - timedelta(minutes=1) <= p[0] <= end + timedelta(minutes=1)]
                if inside:
                    rec = slot_record("", "codex", path.stem[-36:], info, start, end, inside, tz)
                    rec["delegated"] = delegated
                    slots.append(rec)
        day += timedelta(days=1)
    return slots


def inherit_delegated_tasks(slots):
    """A delegated agent run belongs to the Claude slot that started it (overlap within 5 min)."""
    claude = [s for s in slots if s["source"] == "claude" and s["task"]]
    pad = timedelta(minutes=5)
    for s in slots:
        if not s.get("delegated") or s["task"]:
            continue
        a, b = datetime.fromisoformat(s["start"]), datetime.fromisoformat(s["end"])
        owner = [c for c in claude if datetime.fromisoformat(c["start"]) - pad <= b
                 and datetime.fromisoformat(c["end"]) + pad >= a]
        same_cwd = [c for c in owner if c["cwd"] == s["cwd"]]
        if same_cwd or owner:
            s["task_hint"] = (same_cwd or owner)[0]["task"]


# --- Netgen Time Logger -----------------------------------------------------

def ro_connect(path: Path, immutable=False):
    uri = f"file:{quote(str(path))}?mode=ro" + ("&immutable=1" if immutable else "")
    con = sqlite3.connect(uri, uri=True, timeout=5)
    con.execute("PRAGMA query_only=ON")
    return con


def ms_to_dt(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc)


def collect_time_logger(cfg, t0, t1, tz, start_day, end_day, warnings):
    out = {"meetings": [], "calendar": [], "commits": [], "jira": [], "recaps": [], "drafts": []}
    db = expand(cfg["paths"]["time_logger_db"])
    if not db.exists():
        warnings.append("Netgen Time Logger DB not found: no meetings, commits or Tempo duplicate check")
        return out
    try:
        con = ro_connect(db)
        rows = con.execute(
            "select source, kind, start, end, data from signals where start >= ? and start < ? "
            "and (source in ('cal','git','jira','checkin') or (source='window' and kind='focus')) "
            "order by start", (t0.timestamp() * 1000 - 3600_000, t1.timestamp() * 1000)).fetchall()
        drafts = con.execute(
            "select day, run_id, start_minutes, end_minutes, issue_key, description, worklog_id "
            "from drafts where day between ? and ? order by day, start_minutes",
            (start_day.isoformat(), end_day.isoformat())).fetchall()
    except sqlite3.Error as exc:
        warnings.append(f"Netgen Time Logger DB unreadable: {exc}")
        return out

    meeting_re = [re.compile(p) for p in cfg["meeting_title_patterns"]]
    focus = defaultdict(list)
    for source, kind, start, end, data in rows:
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            continue
        s = ms_to_dt(start)
        e = ms_to_dt(end) if end else s
        if source == "window":
            title = (data.get("title") or "").strip()
            if title and any(r.search(title) for r in meeting_re):
                focus[title].append((s, max(e, s)))
        elif source == "cal":
            out["calendar"].append({"title": data.get("title", ""), "start": iso(s, tz), "end": iso(e, tz),
                                    "response": data.get("response", ""),
                                    "attendees": len(data.get("attendees") or [])})
        elif source == "git":
            out["commits"].append({"time": iso(s, tz), "message": (data.get("message") or "").splitlines()[0][:200]})
        elif source == "jira":
            what = (f"{data.get('fromStatus')} -> {data.get('toStatus')}" if data.get("toStatus")
                    else "comment: " + clean_text(data.get("body") or "")[:160])
            out["jira"].append({"time": iso(s, tz), "key": data.get("issueKey", ""),
                                "summary": data.get("summary", ""), "what": what})
        elif source == "checkin":
            out["recaps"].append({"start": iso(s, tz), "end": iso(e, tz), "text": data.get("text", "")})

    for title, spans in focus.items():
        spans.sort()
        cluster = [spans[0][0], spans[0][1]]
        for s, e in spans[1:] + [(None, None)]:
            if s is not None and (s - cluster[1]).total_seconds() <= 20 * 60:
                cluster[1] = max(cluster[1], e)
                continue
            m_start, m_end = cluster[0], cluster[1] + timedelta(minutes=2)
            if m_end > t0 and m_start < t1:
                cal = best_calendar(out["calendar"], m_start, m_end)
                out["meetings"].append({"title": title, "start": iso(m_start, tz), "end": iso(m_end, tz),
                                        "calendar": cal})
            if s is not None:
                cluster = [s, e]
    out["meetings"].sort(key=lambda m: m["start"])
    out["calendar"] = [c for c in out["calendar"] if c["end"] > iso(t0, tz) and c["start"] < iso(t1, tz)]

    latest = {}
    for day, run_id, *_ in drafts:
        latest[day] = max(latest.get(day, 0), run_id)
    seen_worklogs = set()
    for day, run_id, s, e, key, desc, worklog in drafts:
        if worklog:
            if worklog in seen_worklogs:
                continue
            seen_worklogs.add(worklog)
        elif run_id != latest[day]:
            continue
        out["drafts"].append({"date": day, "start": s, "end": e, "key": key or "",
                              "desc": desc, "worklog_id": worklog or ""})
    return out


def best_calendar(calendar, start, end):
    best, overlap = "", 0
    for c in calendar:
        cs, ce = datetime.fromisoformat(c["start"]), datetime.fromisoformat(c["end"])
        ov = (min(ce, end) - max(cs, start)).total_seconds()
        if ov > overlap:
            best, overlap = f"{c['title']} ({cs:%H:%M}-{ce:%H:%M}, {c['response']})", ov
    return best


# --- Browser ----------------------------------------------------------------

def host_class(host, bcfg):
    if host_matches(host, bcfg.get("project_hosts", {})):
        return "project"
    for cls in ("dev", "preprod", "tool"):
        if host_matches(host, bcfg[f"{cls}_hosts"]):
            return cls
    if host_matches(host, bcfg["ignore_hosts"]):
        return "ignore"
    return "other"


def collect_browser(cfg, t0, t1, tz, warnings):
    bcfg = cfg["browser"]
    groups = defaultdict(list)
    found = False
    for hist in cfg["paths"]["browser_history"]:
        path = expand(hist)
        if not path.exists():
            continue
        found = True
        try:
            con = ro_connect(path, immutable=True)
            rows = con.execute(
                "select v.visit_time, v.visit_duration, u.url, u.title from visits v "
                "join urls u on u.id = v.url where v.visit_time >= ? and v.visit_time < ?",
                ((t0.timestamp() + CHROME_EPOCH_OFFSET) * 1e6, (t1.timestamp() + CHROME_EPOCH_OFFSET) * 1e6)).fetchall()
        except sqlite3.Error as exc:
            warnings.append(f"Browser history unreadable ({path}): {exc}")
            continue
        for vt, duration, url, title in rows:
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                continue
            host = parts.hostname.lower() + (f":{parts.port}" if parts.port else "")
            cls = host_class(parts.hostname.lower(), bcfg)
            if cls == "ignore":
                continue
            start = datetime.fromtimestamp(vt / 1e6 - CHROME_EPOCH_OFFSET, timezone.utc)
            end = start + timedelta(seconds=min((duration or 0) / 1e6, 1800))
            path_prefix = "/" + "/".join(p for p in parts.path.split("/")[1:3] if p)
            groups[(start.astimezone(tz).date().isoformat(), host, cls)].append(
                (start, end, path_prefix, (title or "").strip()[:70]))
    if not found:
        warnings.append("Browser history not found")

    items = []
    for (day, host, cls), visits in groups.items():
        visits.sort()
        ranges = []
        for s, e, *_ in visits:
            if ranges and (s - ranges[-1][1]).total_seconds() <= 30 * 60:
                ranges[-1][1] = max(ranges[-1][1], e, s)
            else:
                ranges.append([s, max(e, s)])
        titles = list(dict.fromkeys(t for *_, t in visits if t))[:3]
        paths = list(dict.fromkeys(p for _, _, p, _ in visits if p != "/"))[:3]
        hint = next((v for k, v in bcfg.get("project_hosts", {}).items()
                     if host_matches(host.split(":")[0], [k])), "")
        items.append({"date": day, "host": host, "class": cls, "hint": hint, "visits": len(visits),
                      "ranges": [[iso(s, tz), iso(e, tz)] for s, e in ranges],
                      "titles": titles, "paths": paths})
    items.sort(key=lambda b: (b["date"], b["ranges"][0][0]))
    limit = bcfg["max_other_hosts_per_day"]
    per_day = defaultdict(list)
    for b in items:
        if b["class"] == "other":
            per_day[b["date"]].append(b)
    keep_other = {id(b) for bs in per_day.values()
                  for b in sorted(bs, key=lambda b: -b["visits"])[:limit]}
    return [b for b in items if b["class"] != "other" or id(b) in keep_other]


# --- Output -----------------------------------------------------------------

def assign_ids(evidence):
    prefixes = {"slots": None, "meetings": "M", "calendar": "E", "commits": "G", "jira": "J",
                "recaps": "R", "drafts": "D", "browser": "B"}
    for key, prefix in prefixes.items():
        evidence[key].sort(key=lambda x: (x.get("date", ""), str(x.get("start") or x.get("time") or ""),
                                          str(x.get("ranges", ""))))
        counters = defaultdict(int)
        for item in evidence[key]:
            p = prefix or ("C" if item["source"] == "claude" else "X")
            counters[p] += 1
            item["id"] = f"{p}{counters[p]}"


def local_day(value: str) -> str:
    return value[:10]


def hm(value: str) -> str:
    return value[11:16]


def write_brief(evidence, cfg, path: Path):
    n = cfg["prompt_excerpts_per_slot"]
    chars = cfg["prompt_excerpt_chars"]
    p = evidence["period"]
    lines = [f"# Timesheet evidence {p['start']} .. {p['end']} ({p['timezone']})",
             f"Run dir: {path.parent}", ""]
    src = evidence["sources"]
    lines.append("Sources: " + ", ".join(f"{k}={v}" for k, v in src.items()))
    for w in evidence["warnings"]:
        lines.append(f"WARNING: {w}")
    if cfg.get("buckets"):
        lines.append("Generic buckets: " + "; ".join(f"{k} = {v}" for k, v in cfg["buckets"].items()))
    if cfg.get("jira_cloud_id"):
        lines.append(f"Jira cloudId: {cfg['jira_cloud_id']}")
    lines.append("Times are local. Slot prompts are excerpts; run `detail` for all prompts of a slot.")

    days = sorted({local_day(s["start"]) for s in evidence["slots"]}
                  | {local_day(m["start"]) for m in evidence["meetings"]}
                  | {d["date"] for d in evidence["drafts"]}
                  | {b["date"] for b in evidence["browser"]})
    days = [d for d in days if p["start"] <= d <= p["end"]] or [p["start"]]
    prev_day = (datetime.fromisoformat(p["start"]) - timedelta(days=1)).date().isoformat()
    for day in ([prev_day] if any(local_day(s["start"]) == prev_day for s in evidence["slots"]) else []) + days:
        wd = datetime.fromisoformat(day).strftime("%a")
        lines += ["", f"## {wd} {day}"]

        tempo = [d for d in evidence["drafts"] if d["date"] == day and d["worklog_id"]]
        if tempo:
            lines.append("### Already in Tempo (do not enter again)")
            lines += [f"{d['id']} {d['start'] // 60:02d}:{d['start'] % 60:02d}-{d['end'] // 60:02d}:{d['end'] % 60:02d} "
                      f"{d['key']} {d['desc']} (worklog {d['worklog_id']})" for d in tempo]

        meets = [m for m in evidence["meetings"] if local_day(m["start"]) == day]
        if meets:
            lines.append("### Meetings (window focus, real attendance)")
            lines += [f"{m['id']} {hm(m['start'])}-{hm(m['end'])} {m['title']}"
                      + (f" | cal: {m['calendar']}" if m["calendar"] else "") for m in meets]
        cal = [c for c in evidence["calendar"] if local_day(c["start"]) == day]
        if cal:
            lines.append("### Calendar")
            lines += [f"{c['id']} {hm(c['start'])}-{hm(c['end'])} {c['title']} ({c['response']})" for c in cal]

        slots = [s for s in evidence["slots"] if local_day(s["start"]) == day]
        if slots:
            lines.append("### Claude (C) / Codex (X) slots")
            for s in slots:
                key = s["task"] or (f"??? hint {s['task_hint']}" if s["task_hint"] else "???")
                lines.append(f"{s['id']} {hm(s['start'])}-{hm(s['end'])} ({s['minutes']}m) {key} | "
                             f"{s['project']} [{s['session'][:8]}]" + (f" | topic: {s['topic']}" if s["topic"] else ""))
                if s.get("delegated"):
                    lines[-1] += " | delegated by an agent (fill only)"
                    limit = 1
                else:
                    limit = n if s["minutes"] >= 10 else 2
                shown = excerpts(s["prompts"], limit)
                lines += [f"    {t} {text[:chars]}" for t, text in shown]
                if len(s["prompts"]) > len(shown):
                    lines.append(f"    (+{len(s['prompts']) - len(shown)} more prompts)")

        for key, title, fmt_ in (
            ("commits", "Commits", lambda c: f"{c['id']} {hm(c['time'])} {c['message']}"),
            ("jira", "Jira activity", lambda j: f"{j['id']} {hm(j['time'])} {j['key']} {j['what']}"),
            ("recaps", "Recaps typed in Time Logger", lambda r: f"{r['id']} {hm(r['start'])}-{hm(r['end'])} {r['text']}"),
        ):
            items = [x for x in evidence[key] if local_day(x.get("time") or x.get("start")) == day]
            if items:
                lines.append(f"### {title}")
                lines += [fmt_(x) for x in items]

        drafts = [d for d in evidence["drafts"] if d["date"] == day and not d["worklog_id"]]
        if drafts:
            lines.append("### Time Logger draft (suggestion only)")
            lines += [f"{d['start'] // 60:02d}:{d['start'] % 60:02d}-{d['end'] // 60:02d}:{d['end'] % 60:02d} "
                      f"{d['key'] or '-'} {d['desc']}" for d in drafts]

        browser = [b for b in evidence["browser"] if b["date"] == day]
        if browser:
            lines.append("### Browser (dev/preprod/tool hosts in detail, other hosts for classification only)")
            for b in browser:
                rng = ",".join(f"{hm(s)}-{hm(e)}" for s, e in b["ranges"])
                if b["class"] == "other":
                    continue
                extra = " ".join(b["paths"]) + (" | " + " / ".join(b["titles"]) if b["titles"] else "")
                cls = f"{b['class']} {b['hint']}" if b.get("hint") else b["class"]
                lines.append(f"{b['id']} [{cls}] {b['host']} {rng} ({b['visits']}) {extra}")
            other = [b for b in browser if b["class"] == "other"]
            if other:
                lines.append("other: " + "; ".join(
                    f"{b['id']} {b['host']} {','.join(hm(s) for s, _ in b['ranges'])} ({b['visits']})"
                    + (f" \"{b['titles'][0][:40]}\"" if b["titles"] else "") for b in other))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_collect(args):
    cfg = load_config(args.config)
    tz = tz_of(cfg)
    start_day, end_day = resolve_period(args, datetime.now(tz).date())
    t0 = datetime.combine(start_day, time(0), tz).astimezone(timezone.utc)
    t1 = datetime.combine(end_day + timedelta(days=1), time(0), tz).astimezone(timezone.utc)

    sweep = Path(__file__).resolve().parent / "sweep.py"
    subprocess.run([sys.executable, str(sweep)], check=False, stdout=subprocess.DEVNULL)

    warnings = []
    slots = collect_claude(cfg, t0, t1, tz, warnings) + collect_codex(cfg, t0, t1, tz, warnings)
    inherit_delegated_tasks(slots)
    logger = collect_time_logger(cfg, t0, t1, tz, start_day, end_day, warnings)
    browser = collect_browser(cfg, t0, t1, tz, warnings)
    evidence = {
        "version": 1,
        "period": {"start": start_day.isoformat(), "end": end_day.isoformat(), "timezone": str(tz)},
        "generated_at": datetime.now(tz).isoformat(timespec="seconds"),
        "warnings": warnings,
        "slots": slots, "browser": browser, **logger,
    }
    evidence["sources"] = {
        "claude_slots": sum(s["source"] == "claude" for s in slots),
        "codex_slots": sum(s["source"] == "codex" for s in slots),
        "meetings": len(logger["meetings"]), "commits": len(logger["commits"]),
        "in_tempo": sum(1 for d in logger["drafts"] if d["worklog_id"]),
        "browser_hosts": len(browser),
    }
    assign_ids(evidence)

    run = Path(args.run_dir) if args.run_dir else run_dir_for(start_day, end_day)
    run.mkdir(parents=True, exist_ok=True)
    os.chmod(run, 0o700)
    for stale in ("annotations.json", "report.txt", "slots.json"):
        if (run / stale).exists():
            (run / stale).replace(run / f"{stale}.prev")
    (run / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
    write_brief(evidence, cfg, run / "brief.md")
    for f in run.iterdir():
        os.chmod(f, 0o600)

    print(f"RUN_DIR={run}")
    print(f"period {start_day} .. {end_day}; " + ", ".join(f"{k}={v}" for k, v in evidence["sources"].items()))
    for w in warnings:
        print(f"warning: {w}")
    print(f"Next: read {run / 'brief.md'}")


def cmd_detail(args):
    evidence = json.loads((Path(args.run) / "evidence.json").read_text(encoding="utf-8"))
    wanted = set(args.ids)
    for s in evidence["slots"]:
        if s["id"] in wanted:
            print(f"{s['id']} {s['start']} .. {s['end']} {s['task'] or '???'} {s['project']} [{s['session']}]")
            for t, text in s["prompts"]:
                print(f"  {t} {text}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    add_period_args(c)
    c.add_argument("--config", help="extra config JSON")
    c.add_argument("--run-dir", help="output directory (default ~/.claude/tempo-timesheet/<period>)")
    d = sub.add_parser("detail")
    d.add_argument("--run", required=True)
    d.add_argument("ids", nargs="+")
    args = ap.parse_args()
    cmd_collect(args) if args.cmd == "collect" else cmd_detail(args)


if __name__ == "__main__":
    main()
