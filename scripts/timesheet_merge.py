#!/usr/bin/env python3
"""Merge evidence + agent annotations into non-overlapping Tempo slots.

  timesheet_merge.py --run DIR

Reads DIR/evidence.json (timesheet_collect.py) and DIR/annotations.json
(written by the agent), prints the timesheet and writes report.txt,
slots.json and logger-notes.txt into DIR.

Rules:
- every minute belongs to one source: meeting (priority 1) > Claude/Codex
  slot (2, the longer slot wins between two) > fill from Slack, browser or
  delegated agent runs (3); annotations may set another priority
- the day is cut into 15-minute cells; a cell is worked when it holds at
  least `cell_min_active_minutes` (8 = nearest-quarter rounding) and goes to
  the task with most minutes in it
- a task with work that day but no cell still gets one free cell next to its
  work, so short work never disappears (same as the report's 15-min minimum)
- cells overlapping entries already in Tempo are left out
- adjacent cells of one task form a slot; slots never cross midnight

annotations.json:
{
  "evidence": {"C12": {"task": "CED-14", "desc": "...", "reason": "...",
                       "ranges": [{"from": "12:05", "to": "12:20", "task": "...", "desc": "..."}]},
               "C13": {"drop": true, "reason": "..."},
               "M2":  {"task": "SGDL-3", "desc": "SGDL/MIG daily status meeting"},
               "B13": {"task": "CED-2572", "desc": "..."}},
  "items": [{"date": "2026-10-19", "start": "13:45", "end": "14:10", "task": "SGDL-69",
             "desc": "...", "kind": "slack", "source": "Slack #channel 13:47-14:07"}],
  "final": {"CED-2572|2026-10-20|12:30": "description for that final slot"},
  "jira": {"CED-2572": "BE * SPAR-NI * Backoffice ... [Done]"},
  "notes": ["free-text lines for the check list"]
}
Priorities by kind: meeting 1, work 2, slack/browser/fill 3.
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from timesheet_common import fmt, hhmm, load_config, tz_of  # noqa: E402

BAR = "█"
LABEL_W = 14
KIND_PRIORITY = {"meeting": 1, "work": 2, "slack": 3, "browser": 3, "fill": 3}
UNRESOLVED = "???"
IN_TEMPO = "IN TEMPO"


class Merger:
    def __init__(self, evidence, annotations, cfg):
        self.ev = evidence
        self.ann = annotations
        self.cfg = cfg
        self.tz = tz_of(cfg)
        self.cell = int(cfg["cell_minutes"])
        self.min_active = int(cfg["cell_min_active_minutes"])
        self.internal = cfg["internal_label"]
        p = evidence["period"]
        self.first, self.last = date.fromisoformat(p["start"]), date.fromisoformat(p["end"])
        # (day, minute) -> (priority, task, desc, evidence id)
        self.minutes = {}
        self.raw = defaultdict(set)  # (day, task) -> minutes claimed by any evidence
        self.raw_desc = defaultdict(list)  # (day, task) -> [(minute, desc)]
        self.reserved = defaultdict(set)  # day -> minutes already in Tempo
        self.in_tempo = []
        self.key_changes, self.dropped, self.notes = [], [], list(annotations.get("notes") or [])

    # --- input ------------------------------------------------------------

    def local_dt(self, day: str, hm: str) -> datetime:
        h, m = (int(x) for x in hm.split(":"))
        base = datetime.combine(date.fromisoformat(day), time(0), self.tz)
        return (base.astimezone(timezone.utc) + timedelta(hours=h, minutes=m))

    def span(self, start: datetime, end: datetime):
        """Local (day, minute) keys of a UTC interval, at least one minute."""
        cur = start.astimezone(timezone.utc).replace(second=0, microsecond=0)
        end = max(end.astimezone(timezone.utc), cur + timedelta(minutes=1))
        while cur < end:
            loc = cur.astimezone(self.tz)
            day = loc.date()
            if self.first <= day <= self.last:
                yield day.isoformat(), loc.hour * 60 + loc.minute
            cur += timedelta(minutes=1)

    def put(self, start, end, priority, task, desc, eid):
        for key in self.span(start, end):
            self.raw[(key[0], task)].add(key[1])
            if desc:
                self.raw_desc[(key[0], task)].append((key[1], desc))
            cur = self.minutes.get(key)
            if cur is None or priority < cur[0]:
                self.minutes[key] = (priority, task, desc, eid)

    def range_bounds(self, rng, default_day):
        f, t = rng["from"], rng["to"]
        fd, fh = (f.split(" ") if " " in f else (default_day, f))
        td, th = (t.split(" ") if " " in t else (fd, t))
        start, end = self.local_dt(fd, fh), self.local_dt(td, th)
        if end <= start:
            end += timedelta(days=1)
        return start, end

    def load(self):
        ann_ev = self.ann.get("evidence") or {}
        for s in self.ev["slots"]:
            a = ann_ev.get(s["id"], {})
            if a.get("drop"):
                self.dropped.append(f"{s['id']} {s['start'][5:16]} {s['project']}: {a.get('reason', 'dropped')}")
                continue
            task = a.get("task") or s["task"] or (s.get("delegated") and s.get("task_hint")) or UNRESOLVED
            if a.get("task") and s["task"] and a["task"] != s["task"]:
                self.key_changes.append(f"{s['id']} {s['start'][:10]} {s['start'][11:16]}: "
                                        f"{s['task']} -> {a['task']}" + (f" ({a['reason']})" if a.get("reason") else ""))
            default = 3 if s.get("delegated") else 2
            prio = a.get("priority", default) + 1 / (1 + s["minutes"])
            start, end = datetime.fromisoformat(s["start"]), datetime.fromisoformat(s["end"])
            self.put(start, end, prio, task, a.get("desc") or "", s["id"])
            for rng in a.get("ranges") or []:
                rs, re_ = self.range_bounds(rng, s["start"][:10])
                self.put(rs, re_, prio - 0.5, rng.get("task") or task, rng.get("desc") or a.get("desc") or "", s["id"])

        for m in self.ev["meetings"]:
            a = ann_ev.get(m["id"])
            if not a or a.get("drop") or not a.get("task"):
                continue
            start, end = datetime.fromisoformat(m["start"]), datetime.fromisoformat(m["end"])
            if a.get("from"):
                start, end = self.range_bounds({"from": a["from"], "to": a["to"]}, m["start"][:10])
            self.put(start, end, a.get("priority", 1), a["task"], a.get("desc") or m["title"], m["id"])

        for b in self.ev["browser"]:
            a = ann_ev.get(b["id"])
            if not a or a.get("drop") or not a.get("task"):
                continue
            for s, e in b["ranges"]:
                self.put(datetime.fromisoformat(s), datetime.fromisoformat(e), a.get("priority", 3),
                         a["task"], a.get("desc") or "", b["id"])

        for i, it in enumerate(self.ann.get("items") or []):
            start, end = self.range_bounds({"from": it["start"], "to": it["end"]}, it["date"])
            prio = it.get("priority", KIND_PRIORITY.get(it.get("kind", "fill"), 3))
            self.put(start, end, prio, it["task"], it.get("desc") or "", f"I{i + 1}")

        for d in self.ev["drafts"]:
            if not d["worklog_id"] or not (self.first.isoformat() <= d["date"] <= self.last.isoformat()):
                continue
            self.reserved[d["date"]].update(range(d["start"], d["end"]))
            self.in_tempo.append(d)
        for (day, task), mins in list(self.raw.items()):
            left = mins - self.reserved[day]
            # the rest of work that is already in Tempo is only an edge, not new work
            if not left or (len(left) < len(mins) and len(left) < self.min_active):
                del self.raw[(day, task)]
            else:
                self.raw[(day, task)] = left

    # --- allocation -------------------------------------------------------

    def allocate(self):
        per_day = defaultdict(dict)
        for (day, minute), val in self.minutes.items():
            per_day[day][minute] = val
        cells = {}  # (day, cell_start) -> task
        for day, mins in per_day.items():
            for c in range(0, 1440, self.cell):
                if any(x in self.reserved[day] for x in range(c, c + self.cell)):
                    continue
                count, best_prio = defaultdict(int), {}
                for x in range(c, c + self.cell):
                    if x in mins:
                        prio, task = mins[x][0], mins[x][1]
                        count[task] += 1
                        best_prio[task] = min(best_prio.get(task, 9), prio)
                if count and sum(count.values()) >= self.min_active:
                    cells[(day, c)] = max(count, key=lambda t: (count[t], -best_prio[t], t))

        # short work never disappears: give it a free cell next to its minutes
        for (day, task), mins in sorted(self.raw.items()):
            if any(d == day and t == task for (d, _), t in cells.items()):
                continue
            by_cell = defaultdict(int)
            for m in mins:
                by_cell[m - m % self.cell] += 1
            candidates = sorted(by_cell, key=lambda c: -by_cell[c])
            candidates += [c + d for c in candidates for d in (-self.cell, self.cell)]
            for c in candidates:
                free = 0 <= c < 1440 and (day, c) not in cells and \
                    not any(x in self.reserved[day] for x in range(c, c + self.cell))
                if free:
                    cells[(day, c)] = task
                    break
        return cells, per_day

    def build(self):
        if hasattr(self, "_slots"):
            return self._slots
        self.load()
        cells, per_day = self.allocate()
        slots = []
        for (day, c) in sorted(cells):
            task = cells[(day, c)]
            if slots and slots[-1]["date"] == day and slots[-1]["task"] == task and slots[-1]["end"] == c:
                slots[-1]["end"] = c + self.cell
            else:
                slots.append({"date": day, "task": task, "start": c, "end": c + self.cell})

        final = self.ann.get("final") or {}
        for s in slots:
            descs, sources = [], []
            for x in range(s["start"], s["end"]):
                v = per_day[s["date"]].get(x)
                if v and v[1] == s["task"]:
                    if v[2] and v[2] not in descs:
                        descs.append(v[2])
                    if v[3] not in sources:
                        sources.append(v[3])
            if not descs:
                near = sorted(self.raw_desc[(s["date"], s["task"])],
                              key=lambda md: abs(md[0] - (s["start"] + s["end"]) / 2))
                descs = near[:1] and [near[0][1]]
            key = f"{s['task']}|{s['date']}|{hhmm(s['start'])}"
            s["desc"] = final.get(key) or shorten("; ".join(descs), 220)
            s["sources"] = sources
            s["minutes"] = s["end"] - s["start"]
        self._slots = slots

        entered = defaultdict(int)
        for (day, _), task in cells.items():
            entered[(day, task)] += self.cell
        self.lost = {k: len(m) - entered[k] for k, m in self.raw.items()
                     if len(m) - entered[k] >= self.min_active}
        return slots

    # --- output -----------------------------------------------------------

    def render(self, slots):
        p = self.ev["period"]
        work = [s for s in slots if s["task"] not in (self.internal, UNRESOLVED)]
        internal = [s for s in slots if s["task"] == self.internal]
        unresolved = [s for s in slots if s["task"] == UNRESOLVED]
        line = lambda s: f"{s['task']} | {s['date']} | {hhmm(s['start'])}-{hhmm(s['end'])} | {fmt(s['minutes'])} | {s['desc'] or '(no description)'}"

        out = [f"Timesheet {p['start']} .. {p['end']} ({p['timezone']})", ""]
        out += [line(s) for s in work] or ["(no entry-ready slots)"]
        if internal:
            out += ["", f"{self.internal} (no task key, not in totals)"] + [line(s) for s in internal]
        if unresolved:
            out += ["", "Unresolved (??? — not in totals)"] + [line(s) for s in unresolved]

        out += ["", "Totals"]
        per_day = defaultdict(int)
        for s in work:
            per_day[s["date"]] += s["minutes"]
        for day in sorted(per_day):
            out.append(f"{datetime.fromisoformat(day):%a} {day}  {fmt(per_day[day])}")
        out.append(f"Period          {fmt(sum(per_day.values()))}")

        out += self.timeline(slots)
        out += self.checks(slots, work)
        return "\n".join(out) + "\n"

    def timeline(self, slots):
        out = []
        days = sorted({s["date"] for s in slots} | {d["date"] for d in self.in_tempo})
        for day in days:
            rows = defaultdict(list)
            for s in slots:
                if s["date"] == day:
                    rows[s["task"]].append((s["start"], s["end"]))
            for d in self.in_tempo:
                if d["date"] == day:
                    rows[IN_TEMPO].append((d["start"], d["end"]))
            first = min(a for spans in rows.values() for a, _ in spans)
            last = max(b for spans in rows.values() for _, b in spans)
            h0, h1 = first // 60, min(24, (last + 59) // 60)
            out += ["", f"{datetime.fromisoformat(day):%a} {day} (one cell = 15 min)",
                    " " * LABEL_W + "".join(f"{h:02d}  " for h in range(h0, h1))]
            special = (self.internal, UNRESOLVED, IN_TEMPO)
            order = sorted(t for t in rows if t not in special) + [t for t in special if t in rows]
            for task in order:
                cells = ["·"] * (h1 - h0) * 4
                for a, b in rows[task]:
                    for i in range((a - h0 * 60) // 15, -(-(b - h0 * 60) // 15)):
                        if 0 <= i < len(cells):
                            cells[i] = BAR
                out.append(f"{task:<{LABEL_W}}" + "".join(cells))
        return out

    def checks(self, slots, work):
        out = ["", "Check before entry"]
        jira = self.ann.get("jira") or {}
        keys = sorted({s["task"] for s in work})
        unverified = [k for k in keys if k not in jira]
        items = []
        for d in self.in_tempo:
            items.append(f"already in Tempo, not repeated: {d['key']} {d['date']} "
                         f"{hhmm(d['start'])}-{hhmm(d['end'])} {d['desc']}")
        items += [f"key changed: {c}" for c in self.key_changes]
        if unverified:
            items.append("keys not verified in Jira: " + ", ".join(unverified))
        for (day, task), m in sorted(self.lost.items()):
            items.append(f"{fmt(m)} of {task} on {day} lost to overlap or rounding")
        items += [f"dropped: {d}" for d in self.dropped]
        if any(s["task"] == UNRESOLVED for s in slots):
            items.append("some slots have no task key (???) — assign them before entry")
        items += [f"source missing: {w}" for w in self.ev.get("warnings") or []]
        items += self.dst_notes()
        items += self.notes
        return out + [f"- {i}" for i in items] if items else out + ["- nothing to check"]

    def dst_notes(self):
        notes, day = [], self.first
        while day <= self.last:
            a = datetime.combine(day, time(0), self.tz).utcoffset()
            b = datetime.combine(day, time(23, 59), self.tz).utcoffset()
            if a != b:
                notes.append(f"{day} has a clock change; work between 02:00 and 03:00 that night may be merged")
            day += timedelta(days=1)
        return notes

    def logger_notes(self, slots):
        out = []
        for day in sorted({s["date"] for s in slots}):
            out += [f"{day}"]
            out += [f"{hhmm(s['start'])}-{hhmm(s['end'])} {s['task']} {s['desc']}"
                    for s in slots if s["date"] == day]
            out.append("")
        return "\n".join(out)


def shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(";,")
    return cut + "…"


def main():
    ap = argparse.ArgumentParser(description="Merge timesheet evidence into Tempo slots")
    ap.add_argument("--run", required=True, help="run directory from timesheet_collect.py")
    ap.add_argument("--annotations", help="annotations JSON (default RUN/annotations.json)")
    ap.add_argument("--config", help="extra config JSON")
    args = ap.parse_args()

    run = Path(args.run)
    evidence = json.loads((run / "evidence.json").read_text(encoding="utf-8"))
    ann_path = Path(args.annotations) if args.annotations else run / "annotations.json"
    annotations = json.loads(ann_path.read_text(encoding="utf-8")) if ann_path.exists() else {}
    merger = Merger(evidence, annotations, load_config(args.config))
    slots = merger.build()
    report = merger.render(slots)

    (run / "report.txt").write_text(report, encoding="utf-8")
    (run / "slots.json").write_text(json.dumps(slots, ensure_ascii=False, indent=1), encoding="utf-8")
    (run / "logger-notes.txt").write_text(merger.logger_notes(slots), encoding="utf-8")
    print(report, end="")
    print(f"\n(files: {run}/report.txt, logger-notes.txt)")


if __name__ == "__main__":
    main()
