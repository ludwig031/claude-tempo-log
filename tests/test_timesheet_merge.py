import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from timesheet_common import load_config, parse_day  # noqa: E402
from timesheet_merge import Merger  # noqa: E402

DAY = "2026-10-19"


def slot(sid, start, end, task="", minutes=None, day=DAY, end_day=None, **kw):
    s = {"id": sid, "source": "claude", "session": sid, "project": "p", "cwd": "", "task": task,
         "topic": "", "task_hint": "", "start": f"{day}T{start}:00+02:00",
         "end": f"{end_day or day}T{end}:00+02:00", "prompts": []}
    h1, m1 = map(int, start.split(":"))
    h2, m2 = map(int, end.split(":"))
    s["minutes"] = minutes or ((h2 * 60 + m2) - (h1 * 60 + m1)) % 1440 or 1
    s.update(kw)
    return s


def evidence(slots=(), meetings=(), drafts=(), start=DAY, end=DAY):
    return {"period": {"start": start, "end": end, "timezone": "Europe/Zagreb"}, "warnings": [],
            "slots": list(slots), "meetings": list(meetings), "browser": [], "calendar": [],
            "commits": [], "jira": [], "recaps": [], "drafts": list(drafts)}


def run(ev, ann=None):
    cfg = load_config()
    cfg["internal_label"] = "INTERNAL"
    m = Merger(ev, ann or {}, cfg)
    slots = m.build()
    return m, [(s["task"], s["date"], s["start"], s["end"]) for s in slots]


def hm(v):
    h, m = map(int, v.split(":"))
    return h * 60 + m


class MergeRules(unittest.TestCase):
    def test_nearest_quarter_rounding(self):
        # 12:48-13:07 = 12 min in the 12:45 cell, 7 min in the 13:00 cell -> one quarter
        _, out = run(evidence([slot("C1", "12:48", "13:07", "A-1")]))
        self.assertEqual(out, [("A-1", DAY, hm("12:45"), hm("13:00"))])

    def test_short_work_never_disappears(self):
        _, out = run(evidence([slot("C1", "10:24", "10:27", "A-1")]))
        self.assertEqual(out, [("A-1", DAY, hm("10:15"), hm("10:30"))])

    def test_meeting_beats_session(self):
        ev = evidence([slot("C1", "09:00", "10:30", "A-1")],
                      meetings=[{"id": "M1", "title": "Daily", "start": f"{DAY}T09:30:00+02:00",
                                 "end": f"{DAY}T09:44:00+02:00", "calendar": ""}])
        _, out = run(ev, {"evidence": {"M1": {"task": "MEET-3", "desc": "Daily"}}})
        self.assertIn(("MEET-3", DAY, hm("09:30"), hm("09:45")), out)
        self.assertIn(("A-1", DAY, hm("09:00"), hm("09:30")), out)
        self.assertIn(("A-1", DAY, hm("09:45"), hm("10:30")), out)

    def test_longer_parallel_session_wins_and_short_one_gets_free_cell(self):
        ev = evidence([slot("C1", "14:16", "15:10", "LONG-1"), slot("C2", "14:19", "14:25", "SHORT-1")])
        m, out = run(ev)
        self.assertIn(("LONG-1", DAY, hm("14:15"), hm("15:15")), out)
        self.assertIn(("SHORT-1", DAY, hm("14:00"), hm("14:15")), out)

    def test_slack_fill_only_fills_empty_time(self):
        ev = evidence([slot("C1", "10:00", "10:30", "A-1")])
        ann = {"items": [{"date": DAY, "start": "10:15", "end": "11:00", "task": "B-2", "kind": "slack"}]}
        _, out = run(ev, ann)
        self.assertEqual(out, [("A-1", DAY, hm("10:00"), hm("10:30")), ("B-2", DAY, hm("10:30"), hm("11:00"))])

    def test_midnight_split(self):
        ev = evidence([slot("C1", "23:20", "00:20", "A-1", end_day="2026-10-20")], end="2026-10-20")
        _, out = run(ev)
        self.assertEqual(out, [("A-1", DAY, hm("23:15"), 1440), ("A-1", "2026-10-20", 0, hm("00:15"))])

    def test_entries_already_in_tempo_are_not_repeated(self):
        ev = evidence([slot("C1", "09:00", "10:30", "A-1")],
                      drafts=[{"date": DAY, "start": hm("09:30"), "end": hm("10:00"), "key": "MEET-3",
                               "desc": "Daily", "worklog_id": "1"}])
        m, out = run(ev)
        self.assertEqual(out, [("A-1", DAY, hm("09:00"), hm("09:30")), ("A-1", DAY, hm("10:00"), hm("10:30"))])
        self.assertIn("already in Tempo", m.render(m.build()))

    def test_work_already_in_tempo_gets_no_extra_cell(self):
        ev = evidence(meetings=[{"id": "M1", "title": "Daily", "start": f"{DAY}T09:29:00+02:00",
                                 "end": f"{DAY}T09:44:00+02:00", "calendar": ""}],
                      drafts=[{"date": DAY, "start": hm("09:30"), "end": hm("10:00"), "key": "MEET-3",
                               "desc": "Daily", "worklog_id": "1"}])
        _, out = run(ev, {"evidence": {"M1": {"task": "MEET-3"}}})
        self.assertEqual(out, [])

    def test_short_parallel_work_keeps_its_description(self):
        ev = evidence([slot("C1", "12:00", "12:40", "LONG-1"), slot("C2", "12:39", "12:39", "SHORT-1")])
        m, _ = run(ev, {"evidence": {"C2": {"desc": "Install tool"}}})
        short = [s for s in m.build() if s["task"] == "SHORT-1"]
        self.assertEqual(short[0]["desc"], "Install tool")

    def test_reassign_split_and_final_description(self):
        ev = evidence([slot("C1", "12:00", "13:00", "PROJ-123")])
        ann = {"evidence": {"C1": {"task": "A-1", "desc": "first job", "reason": "placeholder",
                                   "ranges": [{"from": "12:30", "to": "13:00", "task": "INTERNAL", "desc": "tool"}]}},
               "final": {f"A-1|{DAY}|12:00": "Better text"}, "jira": {"A-1": "x"}}
        m, out = run(ev, ann)
        self.assertEqual(out, [("A-1", DAY, hm("12:00"), hm("12:30")), ("INTERNAL", DAY, hm("12:30"), hm("13:00"))])
        report = m.render(m.build())
        self.assertIn("A-1 | 2026-10-19 | 12:00-12:30 | 30m | Better text", report)
        self.assertIn("PROJ-123 -> A-1", report)
        self.assertIn("Period          30m", report)  # INTERNAL is not counted

    def test_dropped_and_unresolved(self):
        ev = evidence([slot("C1", "08:00", "08:30"), slot("C2", "09:00", "09:30", "A-1")])
        m, out = run(ev, {"evidence": {"C2": {"drop": True, "reason": "not work"}}})
        self.assertEqual(out, [("???", DAY, hm("08:00"), hm("08:30"))])
        report = m.render(m.build())
        self.assertIn("Unresolved", report)
        self.assertIn("dropped: C2", report)


class Dates(unittest.TestCase):
    def test_both_date_formats(self):
        self.assertEqual(parse_day("19-10-2026").isoformat(), DAY)
        self.assertEqual(parse_day("2026-10-19").isoformat(), DAY)
        self.assertEqual(parse_day("19.10.2026.").isoformat(), DAY)


if __name__ == "__main__":
    unittest.main()
