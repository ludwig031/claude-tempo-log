#!/usr/bin/env python3
"""Summarize the work log for Tempo entry.

Period selection:
  report.py                     current calendar week (Mon-Sun)
  report.py --today             today
  report.py --date 2026-07-08   a specific day
  report.py --week 2026-07-01   the week containing that date
  report.py --from 2026-07-01 --to 2026-07-04   arbitrary range (inclusive)
  report.py --all               entire log

Output options:
  --slots      also list each individual time slot
  --no-chart   plain tables only

Per day and task: sum of slot minutes, rounded to the nearest 15 min for
Tempo (exact minutes shown in parentheses; anything > 0 rounds to at
least 15 min so no work disappears). A single-day report draws a timeline
of the day per task; a multi-day report draws hour bars per day and per
task.
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

LOG_FILE = Path(os.environ.get("TEMPO_LOG_FILE", Path.home() / ".claude" / "work-log.jsonl"))

BAR = "█"
LABEL_W = 14


def fmt(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    if h and m:
        return f"{h}h {m:02d}m"
    return f"{h}h" if h else f"{m}m"


def round15(minutes: int) -> int:
    if minutes <= 0:
        return 0
    return max(15, round(minutes / 15) * 15)


def parse_day(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        sys.exit(f"Invalid date '{value}' (expected YYYY-MM-DD)")


def hhmm_to_min(value: str) -> int:
    h, m = value.split(":")
    return int(h) * 60 + int(m)


def select_period(args):
    """Return (predicate over date-string, human label)."""
    today = date.today()
    if args.all:
        return (lambda d: True), "entire log"
    if getattr(args, "from"):
        start = parse_day(getattr(args, "from"))
        end = parse_day(args.to) if args.to else today
        return (lambda d: start.isoformat() <= d <= end.isoformat()), f"{start} to {end}"
    if args.today:
        return (lambda d: d == today.isoformat()), today.isoformat()
    if args.date:
        target = parse_day(args.date).isoformat()
        return (lambda d: d == target), target
    # default (or explicit --week): current calendar week, Mon-Sun
    anchor = parse_day(args.week) if args.week else today
    monday = anchor - timedelta(days=anchor.weekday())
    sunday = monday + timedelta(days=6)
    return (lambda d: monday.isoformat() <= d <= sunday.isoformat()), f"week {monday} to {sunday}"


def load_rows(keep):
    rows = []
    with open(LOG_FILE, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if keep(row.get("date", "")):
                rows.append(row)
    rows.sort(key=lambda r: (r.get("date", ""), r.get("slot_start", "")))
    return rows


def slot_bounds_min(row):
    """Slot start/end as minutes since midnight; midnight-crossing ends clamp to 24:00."""
    start = hhmm_to_min(row.get("slot_start", "00:00"))
    end = hhmm_to_min(row.get("slot_end", "00:00"))
    if end < start:
        end = 24 * 60
    return start, end


def day_timeline(rows, title="Day timeline (one cell = 15 min)"):
    """One line per task: quarter-hour cells across the day's active hours."""
    by_task = defaultdict(list)
    for row in rows:
        by_task[row.get("task_id") or "???"].append(slot_bounds_min(row))

    first = min(s for spans in by_task.values() for s, _ in spans)
    last = max(e for spans in by_task.values() for _, e in spans)
    h0, h1 = first // 60, min(24, (last + 59) // 60)

    header = " " * LABEL_W + "".join(f"{h:02d}  " for h in range(h0, h1))
    print(f"\n{title}")
    print(header)
    for task in sorted(by_task):
        cells = ["·"] * (h1 - h0) * 4
        for s, e in by_task[task]:
            a = max(0, (s - h0 * 60) // 15)
            b = min(len(cells), max(a + 1, -(-(e - h0 * 60) // 15)))
            for i in range(a, b):
                cells[i] = BAR
        print(f"{task:<{LABEL_W}}" + "".join(cells))


def hbar(minutes: int, unit: int) -> str:
    return BAR * max(1, round(minutes / unit)) if minutes > 0 else ""


def period_charts(rows):
    """Multi-day view: hours per day and hours per task."""
    per_day = defaultdict(int)
    per_task = defaultdict(int)
    for row in rows:
        per_day[row.get("date", "?")] += row.get("duration_min", 0)
        per_task[row.get("task_id") or "???"] += row.get("duration_min", 0)

    peak = max(list(per_day.values()) + list(per_task.values()))
    unit = 15
    while peak / unit > 44:  # keep bars within ~44 chars
        unit *= 2

    print(f"\nHours per day (one {BAR} = {fmt(unit)})")
    for day, minutes in sorted(per_day.items()):
        weekday = datetime.strptime(day, "%Y-%m-%d").strftime("%a")
        print(f"{weekday} {day}  {hbar(minutes, unit)} {fmt(minutes)}")

    print(f"\nHours per task (one {BAR} = {fmt(unit)})")
    for task, minutes in sorted(per_task.items(), key=lambda kv: -kv[1]):
        print(f"{task:<{LABEL_W}}{hbar(minutes, unit)} {fmt(minutes)}")


def main():
    ap = argparse.ArgumentParser(description="Tempo work-log report")
    ap.add_argument("--today", action="store_true", help="today only")
    ap.add_argument("--date", metavar="DATE", help="a specific day, YYYY-MM-DD")
    ap.add_argument("--week", nargs="?", const="", metavar="DATE",
                    help="calendar week Mon-Sun (the default); optional DATE picks the week containing it")
    ap.add_argument("--from", dest="from", metavar="DATE", help="range start, YYYY-MM-DD")
    ap.add_argument("--to", metavar="DATE", help="range end, YYYY-MM-DD (default: today)")
    ap.add_argument("--all", action="store_true", help="entire log")
    ap.add_argument("--slots", action="store_true", help="list individual slots")
    ap.add_argument("--timeline", action="store_true",
                    help="per-day task timelines in multi-day reports")
    ap.add_argument("--no-chart", action="store_true", help="tables only, no charts")
    args = ap.parse_args()

    if not LOG_FILE.exists():
        print(f"No work log yet ({LOG_FILE}).")
        return

    keep, label = select_period(args)
    rows = load_rows(keep)
    if not rows:
        print(f"No logged slots for {label}.")
        return

    groups = defaultdict(lambda: {"min": 0, "slots": [], "topics": [], "projects": set()})
    for row in rows:
        g = groups[(row.get("date", ""), row.get("task_id") or "???")]
        g["min"] += row.get("duration_min", 0)
        g["slots"].append(row)
        topic = (row.get("topic") or "").strip()
        if topic and topic not in g["topics"]:
            g["topics"].append(topic)
        if row.get("project"):
            g["projects"].add(row["project"])

    day_total = defaultdict(int)
    print(f"Work log — {label}")
    print(f"{'Date':<12}{'Task':<{LABEL_W}}{'Tempo':>8}{'Exact':>12}  {'Slots':<6}Project / topic")
    print("-" * 96)
    for (day, task), g in sorted(groups.items()):
        rounded = round15(g["min"])
        day_total[day] += rounded
        info = ", ".join(sorted(g["projects"]))
        if g["topics"]:
            info += " — " + "; ".join(g["topics"])
        print(f"{day:<12}{task:<{LABEL_W}}{fmt(rounded):>8}{'(' + fmt(g['min']) + ')':>12}  {len(g['slots']):<6}{info}")
        if args.slots:
            for s in g["slots"]:
                print(f"{'':<26}{s['slot_start']}–{s['slot_end']}  {fmt(s['duration_min']):>8}")
    print("-" * 96)
    for day, total in sorted(day_total.items()):
        print(f"{day:<12}{'TOTAL':<{LABEL_W}}{fmt(total):>8}")

    if not args.no_chart:
        days = {d for d, _ in groups}
        if len(days) == 1:
            day_timeline(rows)
        else:
            period_charts(rows)
            if args.timeline:
                for day in sorted(days):
                    weekday = datetime.strptime(day, "%Y-%m-%d").strftime("%a")
                    day_timeline([r for r in rows if r.get("date") == day],
                                 title=f"{weekday} {day} (one cell = 15 min)")

    if any(t == "???" for _, t in groups):
        print(f"\n⚠ Some slots have no task ID — fill them in manually in {LOG_FILE}")


if __name__ == "__main__":
    main()
