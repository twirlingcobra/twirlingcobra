"""Read schedule / timesheet exports (CSV, TSV, or XLSX) into normalized records.

Column headers are matched loosely so FileMaker, payroll, or time-clock exports
usually work without editing. Each record becomes
    {"employee_id", "name", "start": datetime, "end": datetime | None}
"""

from __future__ import annotations

import csv
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path

DATE_FORMATS = (
    "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%d-%b-%Y", "%d-%b-%y",
    "%b %d, %Y", "%B %d, %Y", "%Y/%m/%d", "%a %m/%d/%Y",
)
TIME_FORMATS = (
    "%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M:%S %p", "%I:%M%p", "%I %p", "%I%p",
    "%H%M",
)

# normalized header -> field
ALIASES = {
    "employee_id": ["employeeid", "empid", "id", "employee", "employeenumber",
                    "employeeno", "empno", "badge", "badgeid", "badgenumber",
                    "payrollid", "staffid", "clockid"],
    "name": ["name", "employeename", "fullname", "empname", "staffname", "worker"],
    "first": ["firstname", "first", "fname"],
    "last": ["lastname", "last", "lname", "surname"],
    "date": ["date", "workdate", "shiftdate", "scheduledate", "day", "punchdate"],
    "start": ["start", "starttime", "scheduledstart", "schedstart", "shiftstart",
              "in", "timein", "clockin", "punchin", "clockintime", "startdatetime"],
    "end": ["end", "endtime", "scheduledend", "schedend", "shiftend", "out",
            "timeout", "clockout", "punchout", "clockouttime", "enddatetime"],
    "end_date": ["enddate", "outdate", "clockoutdate"],
    "mower": ["mower", "mowerid", "mowernumber", "assignedmower", "equipment", "machine",
              "unit"],
}


class ImportError_(Exception):
    """Raised when a file can't be understood."""


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def parse_date(s) -> date | None:
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    s = (s or "").strip()
    if not s:
        return None
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    raise ValueError(f"Unrecognized date: {s!r}")


def parse_time(s) -> time | None:
    if isinstance(s, datetime):
        return s.time()
    if isinstance(s, time):
        return s
    s = (s or "").strip().upper().replace(".", "")
    if not s:
        return None
    s = re.sub(r"\s+", " ", s)
    for f in TIME_FORMATS:
        try:
            return datetime.strptime(s, f).time()
        except ValueError:
            pass
    raise ValueError(f"Unrecognized time: {s!r}")


def parse_datetime(s, default_date: date | None) -> datetime | None:
    """Accept a full timestamp, or a bare time combined with default_date."""
    if isinstance(s, datetime):
        return s
    if isinstance(s, time):
        return datetime.combine(default_date, s) if default_date else None
    s = (s or "").strip()
    if not s:
        return None
    # Try "date time" combos by splitting at the first space that yields a date.
    parts = s.split(" ")
    for i in range(1, len(parts)):
        d_part, t_part = " ".join(parts[:i]), " ".join(parts[i:])
        try:
            d = parse_date(d_part)
            t = parse_time(t_part)
            return datetime.combine(d, t)
        except ValueError:
            continue
    for f in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(s, f)
        except ValueError:
            pass
    if default_date is None:
        raise ValueError(f"No date for time {s!r}")
    return datetime.combine(default_date, parse_time(s))


def _read_rows(path: Path) -> list[dict]:
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            import openpyxl  # optional
        except ImportError as e:
            raise ImportError_("Reading .xlsx needs 'openpyxl' (pip install openpyxl), "
                               "or export the file as CSV instead.") from e
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []
        headers = [str(h or "") for h in rows[0]]
        return [dict(zip(headers, r)) for r in rows[1:] if any(c not in (None, "") for c in r)]

    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(text.splitlines(), dialect=dialect)
    return [r for r in reader if any((v or "").strip() for v in r.values() if isinstance(v, str))]


def _map_columns(headers: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    normed = {_norm(h): h for h in headers if h}
    for field, aliases in ALIASES.items():
        for a in aliases:
            if a in normed and normed[a] not in mapping.values():
                mapping[field] = normed[a]
                break
    return mapping


def load_records(path: str | Path) -> tuple[list[dict], list[str]]:
    """Return (records, warnings)."""
    path = Path(path)
    rows = _read_rows(path)
    if not rows:
        raise ImportError_(f"{path.name}: no data rows found.")
    cols = _map_columns(list(rows[0].keys()))
    if "start" not in cols:
        raise ImportError_(
            f"{path.name}: couldn't find a start / clock-in column. "
            f"Headers seen: {', '.join(rows[0].keys())}")
    if not ({"employee_id", "name", "first", "last"} & cols.keys()):
        raise ImportError_(f"{path.name}: couldn't find an employee ID or name column.")

    out, warnings = [], []
    for n, row in enumerate(rows, start=2):  # header is line 1
        g = lambda f: row.get(cols[f]) if f in cols else None  # noqa: E731
        try:
            name = (str(g("name") or "")).strip()
            if not name and ("first" in cols or "last" in cols):
                name = f"{(g('first') or '').strip()} {(g('last') or '').strip()}".strip()
            emp = str(g("employee_id") or "").strip() or name
            if isinstance(g("employee_id"), float) and g("employee_id").is_integer():
                emp = str(int(g("employee_id")))
            if not emp:
                raise ValueError("missing employee")
            d = parse_date(g("date")) if "date" in cols and g("date") not in (None, "") else None
            start = parse_datetime(g("start"), d)
            if start is None:
                raise ValueError("missing start / clock-in")
            end_d = parse_date(g("end_date")) if "end_date" in cols and g("end_date") else start.date()
            end = parse_datetime(g("end"), end_d) if "end" in cols else None
            if end is not None and end <= start:
                end += timedelta(days=1)  # overnight shift
            mower = str(g("mower") or "").strip() if "mower" in cols else ""
            out.append({"employee_id": emp, "name": name or emp, "start": start, "end": end,
                        "mower": mower})
        except ValueError as e:
            warnings.append(f"line {n}: skipped ({e})")
    return out, warnings
