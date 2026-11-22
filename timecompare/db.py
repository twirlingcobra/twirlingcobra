"""SQLite storage. One file (timecompare.db) holds everything.

Tables
  employees    id, name
  shifts       scheduled shifts (blue)
  punches      clock-in / clock-out records (green)
  comparisons  saved results per week: deltas, flags, hours, status
  settings     key/value (threshold, week start day)
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from .compare import Punch, Result, Shift

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS employees (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shifts (
    id          INTEGER PRIMARY KEY,
    employee_id TEXT NOT NULL REFERENCES employees(id),
    start       TEXT NOT NULL,
    "end"       TEXT NOT NULL,
    source_file TEXT,
    imported_at TEXT,
    UNIQUE (employee_id, start)
);
CREATE TABLE IF NOT EXISTS punches (
    id          INTEGER PRIMARY KEY,
    employee_id TEXT NOT NULL REFERENCES employees(id),
    clock_in    TEXT NOT NULL,
    clock_out   TEXT,
    source_file TEXT,
    imported_at TEXT,
    UNIQUE (employee_id, clock_in)
);
CREATE TABLE IF NOT EXISTS comparisons (
    id              INTEGER PRIMARY KEY,
    week_start      TEXT NOT NULL,
    employee_id     TEXT NOT NULL,
    employee_name   TEXT,
    work_date       TEXT NOT NULL,
    shift_id        INTEGER,
    sched_start     TEXT,
    sched_end       TEXT,
    actual_in       TEXT,
    actual_out      TEXT,
    in_delta_min    INTEGER,
    out_delta_min   INTEGER,
    early_in_flag   INTEGER NOT NULL DEFAULT 0,
    late_out_flag   INTEGER NOT NULL DEFAULT 0,
    sched_hours     REAL,
    actual_hours    REAL,
    status          TEXT,
    threshold_min   INTEGER,
    computed_at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_shifts_start   ON shifts(start);
CREATE INDEX IF NOT EXISTS ix_punches_in     ON punches(clock_in);
CREATE INDEX IF NOT EXISTS ix_cmp_week       ON comparisons(week_start, employee_id);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

ISO = "%Y-%m-%d %H:%M:%S"


def _ts(d: datetime | None) -> str | None:
    return d.strftime(ISO) if d else None


def _dt(s: str | None) -> datetime | None:
    return datetime.strptime(s, ISO) if s else None


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.con = sqlite3.connect(self.path)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)

    # ---------- settings ----------
    def get_setting(self, key: str, default: str) -> str:
        r = self.con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_setting(self, key: str, value) -> None:
        with self.con:
            self.con.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (key, str(value)))

    # ---------- import ----------
    def _upsert_employees(self, records):
        self.con.executemany(
            "INSERT INTO employees(id,name) VALUES(?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=CASE WHEN excluded.name<>excluded.id "
            "THEN excluded.name ELSE employees.name END",
            {(r["employee_id"], r["name"]) for r in records})

    def import_shifts(self, records, source: str) -> int:
        now = _ts(datetime.now())
        bad = [r for r in records if r["end"] is None]
        if bad:
            raise ValueError(f"{len(bad)} schedule rows have no end time.")
        with self.con:
            self._upsert_employees(records)
            before = self.con.total_changes
            self.con.executemany(
                'INSERT INTO shifts(employee_id,start,"end",source_file,imported_at) '
                "VALUES(?,?,?,?,?) ON CONFLICT(employee_id,start) DO UPDATE SET "
                '"end"=excluded."end", source_file=excluded.source_file, '
                "imported_at=excluded.imported_at",
                [(r["employee_id"], _ts(r["start"]), _ts(r["end"]), source, now)
                 for r in records])
            return self.con.total_changes - before

    def import_punches(self, records, source: str) -> int:
        now = _ts(datetime.now())
        with self.con:
            self._upsert_employees(records)
            before = self.con.total_changes
            self.con.executemany(
                "INSERT INTO punches(employee_id,clock_in,clock_out,source_file,imported_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(employee_id,clock_in) DO UPDATE SET "
                "clock_out=COALESCE(excluded.clock_out, punches.clock_out), "
                "source_file=excluded.source_file, imported_at=excluded.imported_at",
                [(r["employee_id"], _ts(r["start"]), _ts(r["end"]), source, now)
                 for r in records])
            return self.con.total_changes - before

    # ---------- queries ----------
    def employees(self) -> list[tuple[str, str]]:
        return [(r["id"], r["name"]) for r in
                self.con.execute("SELECT id,name FROM employees ORDER BY name")]

    def weeks_with_data(self) -> list[date]:
        rows = self.con.execute("SELECT MIN(start) a, MAX(start) b FROM shifts "
                                "UNION ALL SELECT MIN(clock_in), MAX(clock_in) FROM punches")
        ds = [_dt(v).date() for r in rows for v in (r["a"], r["b"]) if v]
        return ds

    def load_week(self, week_start: date, employee_id: str | None = None):
        """Shifts/punches touching [week_start-1d, week_start+8d) so edges match."""
        lo = _ts(datetime.combine(week_start - timedelta(days=1), datetime.min.time()))
        hi = _ts(datetime.combine(week_start + timedelta(days=8), datetime.min.time()))
        emp_sql, args = ("", ()) if employee_id is None else (" AND employee_id=?", (employee_id,))
        shifts = [Shift(r["id"], r["employee_id"], _dt(r["start"]), _dt(r["end"]))
                  for r in self.con.execute(
                      f'SELECT id,employee_id,start,"end" FROM shifts '
                      f"WHERE start>=? AND start<?{emp_sql}", (lo, hi, *args))]
        punches = [Punch(r["id"], r["employee_id"], _dt(r["clock_in"]), _dt(r["clock_out"]))
                   for r in self.con.execute(
                       f"SELECT id,employee_id,clock_in,clock_out FROM punches "
                       f"WHERE clock_in>=? AND clock_in<?{emp_sql}", (lo, hi, *args))]
        return shifts, punches

    # ---------- results ----------
    def save_comparisons(self, week_start: date, results: list[Result],
                         employee_id: str | None = None) -> None:
        names = dict(self.employees())
        now = _ts(datetime.now())
        ws = week_start.isoformat()
        with self.con:
            if employee_id is None:
                self.con.execute("DELETE FROM comparisons WHERE week_start=?", (ws,))
            else:
                self.con.execute("DELETE FROM comparisons WHERE week_start=? AND employee_id=?",
                                 (ws, employee_id))
            self.con.executemany(
                "INSERT INTO comparisons(week_start,employee_id,employee_name,work_date,shift_id,"
                "sched_start,sched_end,actual_in,actual_out,in_delta_min,out_delta_min,"
                "early_in_flag,late_out_flag,sched_hours,actual_hours,status,threshold_min,"
                "computed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(ws, r.employee_id, names.get(r.employee_id, r.employee_id),
                  r.day.isoformat(), r.shift.id if r.shift else None,
                  _ts(r.shift.start) if r.shift else None,
                  _ts(r.shift.end) if r.shift else None,
                  _ts(r.actual_in), _ts(r.actual_out), r.in_delta_min, r.out_delta_min,
                  int(r.early_in), int(r.late_out), round(r.sched_hours, 2),
                  round(r.actual_hours, 2), r.status, r.threshold_min, now)
                 for r in results])

    def close(self):
        self.con.close()
