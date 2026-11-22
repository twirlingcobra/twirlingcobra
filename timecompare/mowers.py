"""Mower check-ins (from a phone form) and oil-change tracking.

A check-in = who, which mower, hour-meter reading, when. Google Forms
responses (downloaded as CSV, or fetched from the linked Google Sheet) are the
expected source, but any CSV with similar columns works.
"""

from __future__ import annotations

import csv
import io
import re
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .db import Database, _dt, _ts
from .importer import ImportError_, _norm, _read_rows, parse_datetime

DEFAULT_INTERVAL_H = 50.0
DEFAULT_WARN_H = 10.0


# ---------------------------------------------------------------- reading files
def _pick(headers: list[str]) -> dict[str, str]:
    """Find timestamp / name / mower / reading columns by keywords in the header."""
    cols: dict[str, str] = {}
    normed = [(h, _norm(h)) for h in headers if h]
    for h, n in normed:
        if "reading" not in cols and any(k in n for k in ("hourmeter", "meter", "reading",
                                                           "enginehours", "hours")):
            cols["reading"] = h
    for h, n in normed:
        if h in cols.values():
            continue
        if "timestamp" not in cols and n in ("timestamp", "datetime", "checkedin",
                                             "checkintime", "time", "date"):
            cols["timestamp"] = h
        elif "mower" not in cols and any(k in n for k in ("mower", "equipment", "machine",
                                                         "unit")):
            cols["mower"] = h
        elif "name" not in cols and any(k in n for k in ("name", "employee", "worker",
                                                        "crew", "badge")):
            cols["name"] = h
    return cols


def parse_checkins(rows: list[dict], label: str) -> tuple[list[dict], list[str]]:
    if not rows:
        raise ImportError_(f"{label}: no data rows found.")
    cols = _pick(list(rows[0].keys()))
    missing = [k for k in ("timestamp", "name", "mower", "reading") if k not in cols]
    if missing:
        raise ImportError_(f"{label}: couldn't find column(s) for {', '.join(missing)}. "
                           f"Headers seen: {', '.join(rows[0].keys())}")
    out, warnings = [], []
    for n, row in enumerate(rows, start=2):
        try:
            at = parse_datetime(row.get(cols["timestamp"]), None)
            name = str(row.get(cols["name"]) or "").strip()
            mower = str(row.get(cols["mower"]) or "").strip()
            raw = str(row.get(cols["reading"]) or "").strip()
            m = re.search(r"\d+(?:\.\d+)?", raw.replace(",", ""))
            if not (at and name and mower and m):
                raise ValueError("missing time, name, mower or hour-meter reading")
            hours = float(m.group(0))
            out.append({"at": at, "name": name, "mower": mower, "hours": hours})
        except ValueError as e:
            warnings.append(f"line {n}: skipped ({e})")
    return out, warnings


def load_checkins(path: str | Path) -> tuple[list[dict], list[str]]:
    path = Path(path)
    return parse_checkins(_read_rows(path), path.name)


def sheet_csv_url(url: str) -> str:
    """Turn a Google Sheets link into its CSV download link; other URLs pass through."""
    url = url.strip()
    m = re.match(r"https://docs\.google\.com/spreadsheets/d/([\w-]+)", url)
    if m and "/pub" not in url and "export" not in url:
        gid = re.search(r"[#&?]gid=(\d+)", url)
        return (f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv"
                + (f"&gid={gid.group(1)}" if gid else ""))
    return url


def fetch_checkins(url: str, timeout: float = 20) -> tuple[list[dict], list[str]]:
    req = urllib.request.Request(sheet_csv_url(url), headers={"User-Agent": "TimeCompare"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        ctype = resp.headers.get("Content-Type", "")
    text = body.decode("utf-8-sig", errors="replace")
    if "html" in ctype or text.lstrip().lower().startswith("<!doctype html"):
        raise ImportError_("The link returned a web page instead of a spreadsheet. In Google "
                           "Sheets use Share → 'Anyone with the link' (Viewer), or File → "
                           "Share → Publish to web → CSV, and paste that link.")
    rows = list(csv.DictReader(io.StringIO(text)))
    return parse_checkins(rows, "Google Sheet")


# ---------------------------------------------------------------- database ops
@dataclass
class MowerStatus:
    id: int
    name: str
    interval_h: float
    current_h: float | None
    last_change_h: float | None
    last_change_on: str | None
    baseline_assumed: bool
    last_checkin_at: datetime | None
    last_checkin_by: str | None
    warn_h: float

    @property
    def next_due_h(self):
        return None if self.last_change_h is None else self.last_change_h + self.interval_h

    @property
    def hours_left(self):
        if self.next_due_h is None or self.current_h is None:
            return None
        return self.next_due_h - self.current_h

    @property
    def status(self) -> str:
        left = self.hours_left
        if left is None:
            return "No readings yet"
        if left <= 0:
            return "OVERDUE"
        if left <= self.warn_h:
            return "Due soon"
        return "OK"


class Mowers:
    def __init__(self, db: Database):
        self.db = db
        self.con = db.con

    # mowers
    def add(self, name: str, interval_h: float = DEFAULT_INTERVAL_H) -> int:
        name = name.strip()
        with self.con:
            self.con.execute("INSERT OR IGNORE INTO mowers(name, oil_interval_h) VALUES(?,?)",
                             (name, interval_h))
        return self.con.execute("SELECT id FROM mowers WHERE name=?", (name,)).fetchone()["id"]

    def set_interval(self, mower_id: int, interval_h: float) -> None:
        with self.con:
            self.con.execute("UPDATE mowers SET oil_interval_h=? WHERE id=?", (interval_h, mower_id))

    def rename(self, mower_id: int, name: str) -> None:
        with self.con:
            self.con.execute("UPDATE mowers SET name=? WHERE id=?", (name.strip(), mower_id))

    def set_active(self, mower_id: int, active: bool) -> None:
        with self.con:
            self.con.execute("UPDATE mowers SET active=? WHERE id=?", (int(active), mower_id))

    def record_oil_change(self, mower_id: int, on: date, hours: float, notes: str = "") -> None:
        with self.con:
            self.con.execute("INSERT INTO oil_changes(mower_id,changed_on,meter_hours,notes) "
                             "VALUES(?,?,?,?)", (mower_id, on.isoformat(), hours, notes))

    # check-ins
    def import_checkins(self, records: list[dict], source: str) -> tuple[int, list[str]]:
        """Insert new check-ins. Returns (added, notes about new mowers / unknown names)."""
        emps = self.db.employees()
        by_key = {}
        for eid, name in emps:
            by_key[eid.lower()] = eid
            by_key.setdefault(name.lower(), eid)
        known_mowers = {r["name"].lower() for r in self.con.execute("SELECT name FROM mowers")}
        notes, new_mowers, unknown = [], set(), set()
        now = _ts(datetime.now())
        added = 0
        with self.con:
            for r in records:
                if r["mower"].lower() not in known_mowers:
                    new_mowers.add(r["mower"])
                    known_mowers.add(r["mower"].lower())
                mid = self.add(r["mower"])
                eid = by_key.get(r["name"].lower())
                if eid is None:
                    unknown.add(r["name"])
                cur = self.con.execute(
                    "INSERT OR IGNORE INTO checkins(checked_in_at,employee_id,employee_name,"
                    "mower_id,meter_hours,source_file,imported_at) VALUES(?,?,?,?,?,?,?)",
                    (_ts(r["at"]), eid, r["name"], mid, r["hours"], source, now))
                added += cur.rowcount
        if new_mowers:
            notes.append(f"New mower(s) added with a {DEFAULT_INTERVAL_H:g}-hour oil interval: "
                         + ", ".join(sorted(new_mowers))
                         + ". Set the right interval and last oil change on the Mowers tab.")
        if unknown:
            notes.append("Name(s) not matching an employee (kept as typed): "
                         + ", ".join(sorted(unknown)))
        return added, notes

    def status(self, include_inactive: bool = False) -> list[MowerStatus]:
        warn = float(self.db.get_setting("oil_warn_h", str(DEFAULT_WARN_H)))
        out = []
        q = "SELECT * FROM mowers" + ("" if include_inactive else " WHERE active=1")
        for m in self.con.execute(q + " ORDER BY name"):
            last = self.con.execute(
                "SELECT checked_in_at, employee_name, meter_hours FROM checkins "
                "WHERE mower_id=? ORDER BY checked_in_at DESC LIMIT 1", (m["id"],)).fetchone()
            hi = self.con.execute(
                "SELECT MAX(h) h FROM (SELECT meter_hours h FROM checkins WHERE mower_id=? "
                "UNION ALL SELECT meter_hours FROM oil_changes WHERE mower_id=?)",
                (m["id"], m["id"])).fetchone()["h"]
            oc = self.con.execute(
                "SELECT changed_on, meter_hours FROM oil_changes WHERE mower_id=? "
                "ORDER BY meter_hours DESC, changed_on DESC LIMIT 1", (m["id"],)).fetchone()
            baseline = False
            if oc:
                lc_h, lc_on = oc["meter_hours"], oc["changed_on"]
            else:
                first = self.con.execute(
                    "SELECT meter_hours FROM checkins WHERE mower_id=? "
                    "ORDER BY checked_in_at LIMIT 1", (m["id"],)).fetchone()
                lc_h, lc_on, baseline = (first["meter_hours"] if first else None), None, True
            # current = highest reading seen, so one typo'd low value can't hide a due change
            out.append(MowerStatus(m["id"], m["name"], m["oil_interval_h"], hi, lc_h, lc_on,
                                   baseline, _dt(last["checked_in_at"]) if last else None,
                                   last["employee_name"] if last else None, warn))
        return out

    def history(self, mower_id: int) -> list[dict]:
        """Check-ins and oil changes for one mower, newest first, with sanity notes."""
        rows = [{"when": _dt(r["checked_in_at"]), "what": "Check-in", "who": r["employee_name"],
                 "hours": r["meter_hours"], "note": ""}
                for r in self.con.execute("SELECT * FROM checkins WHERE mower_id=?", (mower_id,))]
        rows += [{"when": datetime.fromisoformat(r["changed_on"]), "what": "Oil change", "who": "",
                  "hours": r["meter_hours"], "note": r["notes"] or ""}
                 for r in self.con.execute("SELECT * FROM oil_changes WHERE mower_id=?", (mower_id,))]
        rows.sort(key=lambda r: (r["when"], r["what"] == "Check-in"))
        prev = None
        for r in rows:
            if r["what"] == "Check-in":
                if prev is not None and r["hours"] < prev - 0.05:
                    r["note"] = f"Lower than previous reading ({prev:g}) — typo?"
                elif prev is not None and r["hours"] - prev > 100:
                    r["note"] = f"Jumped {r['hours'] - prev:g} h since previous — typo?"
                prev = r["hours"] if prev is None else max(prev, r["hours"])
        return list(reversed(rows))

    def checkins_between(self, start: date, end: date) -> list[dict]:
        lo = _ts(datetime.combine(start, datetime.min.time()))
        hi = _ts(datetime.combine(end, datetime.min.time()))
        return [{"at": _dt(r["checked_in_at"]), "employee_id": r["employee_id"],
                 "name": r["employee_name"], "mower": r["mower"], "hours": r["meter_hours"]}
                for r in self.con.execute(
                    "SELECT c.*, m.name mower FROM checkins c JOIN mowers m ON m.id=c.mower_id "
                    "WHERE checked_in_at>=? AND checked_in_at<? ORDER BY checked_in_at", (lo, hi))]

    def delete_checkin_or_change(self, kind: str, mower_id: int, when: datetime, hours: float):
        with self.con:
            if kind == "Check-in":
                self.con.execute("DELETE FROM checkins WHERE mower_id=? AND checked_in_at=? "
                                 "AND meter_hours=?", (mower_id, _ts(when), hours))
            else:
                self.con.execute("DELETE FROM oil_changes WHERE mower_id=? AND changed_on=? "
                                 "AND meter_hours=?", (mower_id, when.date().isoformat(), hours))


def week_checkin_lookup(checkins: list[dict]) -> dict[tuple[str, date], list[dict]]:
    """Index check-ins by (employee id or typed name, day) for the weekly table."""
    idx: dict[tuple[str, date], list[dict]] = {}
    for c in checkins:
        key = c["employee_id"] or c["name"]
        idx.setdefault((key, c["at"].date()), []).append(c)
    return idx


__all__ = ["Mowers", "MowerStatus", "load_checkins", "fetch_checkins", "sheet_csv_url",
           "week_checkin_lookup", "DEFAULT_INTERVAL_H"]
