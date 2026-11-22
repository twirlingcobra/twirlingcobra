import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from timecompare.compare import compare
from timecompare.db import Database
from timecompare.importer import load_records, parse_datetime, parse_time

ROOT = Path(__file__).resolve().parent.parent
WEEK = date(2026, 11, 16)


class ParseTests(unittest.TestCase):
    def test_times(self):
        self.assertEqual(parse_time("8:00 AM").hour, 8)
        self.assertEqual(parse_time("8:00 p.m.").hour, 20)
        self.assertEqual(parse_time("21:30").minute, 30)

    def test_full_timestamp(self):
        self.assertEqual(parse_datetime("11/16/2026 9:05 PM", None),
                         datetime(2026, 11, 16, 21, 5))
        self.assertEqual(parse_datetime("2026-11-16 07:00:00", None),
                         datetime(2026, 11, 16, 7, 0))


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        s, _ = load_records(ROOT / "sample_data/schedule_sample.csv")
        p, _ = load_records(ROOT / "sample_data/timesheet_sample.csv")
        self.db.import_shifts(s, "s.csv")
        self.db.import_punches(p, "p.csv")
        shifts, punches = self.db.load_week(WEEK)
        self.res = compare(shifts, punches, 15)
        self.by = {(r.employee_id, r.day): r for r in self.res}

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def get(self, emp, d):
        return self.by[(emp, date(2026, 11, d))]

    def test_within_threshold_ok(self):
        self.assertEqual(self.get("101", 16).status, "OK")

    def test_early_in_flag(self):
        r = self.get("101", 17)  # 7:30 vs 8:00, with lunch punches
        self.assertTrue(r.early_in)
        self.assertEqual(r.in_delta_min, -30)
        self.assertEqual(len(r.punches), 2)
        self.assertFalse(r.late_out)  # 4:35 vs 4:30 is within 15

    def test_late_out_flag(self):
        r = self.get("101", 18)
        self.assertTrue(r.late_out)
        self.assertEqual(r.out_delta_min, 35)

    def test_under_threshold_not_flagged(self):
        r = self.get("102", 20)  # 11:50 vs 12:00 → -10
        self.assertFalse(r.early_in)

    def test_overnight(self):
        r = self.get("103", 18)  # 10p-6a, out 6:40a next day
        self.assertTrue(r.late_out)
        self.assertEqual(r.out_delta_min, 40)
        self.assertAlmostEqual(r.sched_hours, 8.0)

    def test_missing_out_no_show_unscheduled(self):
        self.assertEqual(self.get("102", 19).status, "Missing clock-out")
        self.assertEqual(self.get("102", 21).status, "No punches")
        self.assertEqual(self.get("102", 22).status, "Unscheduled")

    def test_reimport_is_idempotent_and_saves(self):
        s, _ = load_records(ROOT / "sample_data/schedule_sample.csv")
        self.db.import_shifts(s, "again.csv")
        n = self.db.con.execute("SELECT COUNT(*) FROM shifts").fetchone()[0]
        self.assertEqual(n, len(s))
        self.db.save_comparisons(WEEK, self.res)
        flags = self.db.con.execute(
            "SELECT SUM(early_in_flag)+SUM(late_out_flag) FROM comparisons").fetchone()[0]
        self.assertEqual(flags, sum(r.early_in + r.late_out for r in self.res))


if __name__ == "__main__":
    unittest.main()
