#!/usr/bin/env python3
"""Summarize the work log for Tempo entry.

Usage:
  report.py                 today
  report.py --date 2026-07-08
  report.py --week          last 7 days (including today)
  report.py --all           everything
  report.py --slots         also list individual slots

Per day and task: sum of slot minutes, rounded to the nearest 15 min for
Tempo (exact minutes shown in parentheses; anything > 0 rounds to at
least 15 min so no work disappears).
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

LOG_FILE = Path(os.environ.get("TEMPO_LOG_FILE", Path.home() / ".claude" / "work-log.jsonl"))


def fmt(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    if h and m:
        return f"{h}h {m:02d}m"
    return f"{h}h" if h else f"{m}m"


def round15(minutes: int) -> int:
    if minutes <= 0:
        return 0
    return max(15, round(minutes / 15) * 15)


def main():
    ap = argparse.ArgumentParser(description="Tempo work-log report")
    ap.add_argument("--date", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--week", action="store_true", help="last 7 days")
    ap.add_argument("--all", action="store_true", help="entire log")
    ap.add_argument("--slots", action="store_true", help="list individual slots")
    args = ap.parse_args()

    if not LOG_FILE.exists():
        print(f"No work log yet ({LOG_FILE}).")
        return

    today = date.today()
    if args.all:
        keep = lambda d: True
    elif args.week:
        since = (today - timedelta(days=6)).isoformat()
        keep = lambda d: d >= since
    else:
        target = args.date or today.isoformat()
        keep = lambda d: d == target

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

    if not rows:
        print("No logged slots for the selected period.")
        return

    rows.sort(key=lambda r: (r.get("date", ""), r.get("slot_start", "")))

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
    print(f"{'Datum':<12}{'Task':<14}{'Tempo':>8}{'Točno':>12}  {'Slotova':<8}Projekt / tema")
    print("-" * 96)
    for (day, task), g in sorted(groups.items()):
        rounded = round15(g["min"])
        day_total[day] += rounded
        info = ", ".join(sorted(g["projects"]))
        if g["topics"]:
            info += " — " + "; ".join(g["topics"])
        print(f"{day:<12}{task:<14}{fmt(rounded):>8}{'(' + fmt(g['min']) + ')':>12}  {len(g['slots']):<8}{info}")
        if args.slots:
            for s in g["slots"]:
                print(f"{'':<26}{s['slot_start']}–{s['slot_end']}  {fmt(s['duration_min']):>8}")
    print("-" * 96)
    for day, total in sorted(day_total.items()):
        print(f"{day:<12}{'UKUPNO':<14}{fmt(total):>8}")

    missing = sorted({(d, t) for (d, t) in groups if t == "???"})
    if missing:
        print("\n⚠ Slotovi bez task ID-a — dopuni ručno u", LOG_FILE)


if __name__ == "__main__":
    main()
