import tempfile
import unittest
from datetime import date
from pathlib import Path

from timecompare.db import Database
from timecompare.importer import load_records
from timecompare.mowers import Mowers, fetch_checkins, load_checkins, sheet_csv_url

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "sample_data/mower_checkins_sample.csv"


class MowerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        s, _ = load_records(ROOT / "sample_data/schedule_sample.csv")
        self.db.import_shifts(s, "s.csv")
        self.m = Mowers(self.db)
        recs, warns = load_checkins(SAMPLE)
        self.assertEqual(warns, [])
        self.recs = recs
        self.added, self.notes = self.m.import_checkins(recs, "form.csv")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def st(self, name):
        return next(s for s in self.m.status() if s.name == name)

    def test_import_creates_mowers_and_links_employees(self):
        self.assertEqual(self.added, 12)
        self.assertEqual(len(self.m.status()), 3)
        self.assertIn("New mower(s)", self.notes[0])
        linked = self.db.con.execute(
            "SELECT COUNT(*) FROM checkins WHERE employee_id IS NOT NULL").fetchone()[0]
        self.assertEqual(linked, 12)
        self.assertEqual(self.recs[-1]["hours"], 104.9)  # "104.9 hrs"

    def test_reimport_no_duplicates(self):
        added, _ = self.m.import_checkins(self.recs, "again.csv")
        self.assertEqual(added, 0)

    def test_baseline_without_oil_change(self):
        s = self.st("Mower 1")
        self.assertTrue(s.baseline_assumed)
        self.assertEqual(s.last_change_h, 230.0)
        self.assertEqual(s.current_h, 259.0)
        self.assertAlmostEqual(s.hours_left, 21.0)
        self.assertEqual(s.status, "OK")

    def test_due_soon_and_overdue(self):
        m2 = self.st("Mower 2")
        self.m.record_oil_change(m2.id, date(2026, 10, 30), 372.0)
        self.assertEqual(self.st("Mower 2").status, "Due soon")  # due 422, at 419.6
        self.m.set_interval(m2.id, 40)
        self.assertEqual(self.st("Mower 2").status, "OVERDUE")  # due 412
        self.m.record_oil_change(m2.id, date(2026, 11, 20), 419.6)
        s = self.st("Mower 2")
        self.assertEqual((s.status, s.next_due_h), ("OK", 459.6))

    def test_typo_reading_flagged_and_ignored_for_current(self):
        m2 = self.st("Mower 2")
        self.assertEqual(m2.current_h, 419.6)
        notes = [h["note"] for h in self.m.history(m2.id) if h["note"]]
        self.assertEqual(len(notes), 1)
        self.assertIn("Lower than previous", notes[0])

    def test_schedule_mower_column(self):
        shifts, _ = self.db.load_week(date(2026, 11, 16), "101")
        self.assertEqual({s.mower for s in shifts}, {"Mower 1"})

    def test_sheet_url_conversion(self):
        self.assertEqual(
            sheet_csv_url("https://docs.google.com/spreadsheets/d/AbC-123/edit#gid=456"),
            "https://docs.google.com/spreadsheets/d/AbC-123/export?format=csv&gid=456")
        pub = "https://docs.google.com/spreadsheets/d/e/2PACX/pub?output=csv"
        self.assertEqual(sheet_csv_url(pub), pub)

    def test_fetch_from_url(self):
        recs, _ = fetch_checkins(SAMPLE.as_uri())
        self.assertEqual(len(recs), 12)


if __name__ == "__main__":
    unittest.main()
