# TimeCompare: Schedule vs. Timesheet

A free Windows desktop app (it also runs on Mac and Linux) that compares employee **schedules** with **clocked time**. It shows each week as an hourly grid and saves everything to a local SQLite database. There are no licenses or subscriptions.

![screenshot](docs/screenshot.png)

| On the grid | Meaning |
|---|---|
| **Blue bar** | Scheduled shift |
| **Green bar** (drawn over the blue one) | Clock-in to clock-out. A split shift or lunch break shows as several green bars |
| **Red flag at the start** | Clocked in **more than 15 minutes before** the scheduled start |
| **Red flag at the end** | Clocked out **more than 15 minutes after** the scheduled end |
| **Orange ?** | Clock-in with no clock-out |

The table under the grid has one row per shift. Its columns are scheduled time, clocked time, in/out difference in minutes, scheduled hours, actual hours and status (OK, Early in, Late out, Missing clock-out, No punches, Unscheduled). The footer shows the week's totals. Hover over a bar to see its details in the status bar.

You can change the 15-minute threshold and the first day of the week (Sun or Mon) in the app. Both settings are saved.

---

## Install (one time)

**Option A: run with Python (simplest)**
1. Install Python 3.10 or newer from <https://www.python.org/downloads/>. Tick **"Add python.exe to PATH"** during setup.
2. Copy this folder somewhere, for example `C:\TimeCompare`.
3. Double-click **`run.bat`**.

**Option B: build a standalone `TimeCompare.exe`**
1. Do step 1 above.
2. Double-click **`build_exe.bat`**. It creates `dist\TimeCompare.exe`.
3. Copy that .exe wherever you like. The database `timecompare.db` is created next to it.

The app needs nothing beyond Python's standard library. The build script also installs `openpyxl` so the .exe can read `.xlsx` files.

## Weekly workflow

1. **Import Schedule…** and pick the schedule export.
2. **Import Timesheet…** and pick the time-clock or timesheet export. You can select several files at once.
3. The app jumps to the imported week. Use ◀ ▶ to change weeks and **Employee** to look at one person.
4. **Export CSV…** saves the week's comparison for payroll or HR files.

Importing the same file again is safe. Records are matched on employee + start time, so a corrected file updates the existing rows (for example, a missing clock-out that has since been filled in) instead of duplicating them.

## File format

CSV, tab-delimited, or `.xlsx`. The column names are matched loosely, so most exports work as they are:

| Needed | Accepted column names (any case or spacing) |
|---|---|
| Employee | `Employee ID`, `Emp ID`, `Badge`, `ID` and/or `Name`, `Employee Name`, `First Name` + `Last Name` |
| Date | `Date`, `Work Date`, `Shift Date` (optional if start/end already include the date) |
| Start | `Start`, `Start Time`, `Shift Start`, `Clock In`, `Time In`, `Punch In` |
| End | `End`, `End Time`, `Shift End`, `Clock Out`, `Time Out`, `Punch Out` |

Accepted dates include `11/16/2026`, `2026-11-16`, `16-Nov-2026`. Accepted times include `8:00 AM`, `8:00a.m.`, `20:00`, `11/16/2026 8:00 PM`. Overnight shifts (end earlier than start) roll over to the next day automatically. Rows the app can't read are skipped and listed in a warning.

See `sample_data/` for examples you can import to try the app.

### Getting the data out of FileMaker (last time you'll need it)
In each layout: **File → Export Records…**, choose *Comma-Separated Values (.csv)*, and include the employee ID, name, date, start and end fields. To move your history over, export all past records once and import them here.

## The database

Everything lives in `timecompare.db`, a single SQLite file next to the app. To back up, copy that file. You can open it with the free [DB Browser for SQLite](https://sqlitebrowser.org/), Excel (via ODBC), or Power BI.

| Table | Contents |
|---|---|
| `employees` | id, name |
| `shifts` | scheduled shifts (employee_id, start, end, source_file, imported_at) |
| `punches` | clock records (employee_id, clock_in, clock_out, source_file, imported_at) |
| `comparisons` | saved results per week: deltas, `early_in_flag`, `late_out_flag`, hours, status, threshold used |
| `settings` | threshold and week start |

To use a different database file (for example, one per location): `python app.py --db "D:\HR\store2.db"`.

## Developer notes

```
timecompare/importer.py   file reading + flexible column/date parsing
timecompare/compare.py    matches punches to shifts, computes flags
timecompare/db.py         SQLite schema + import/upsert/save
timecompare/gui.py        Tkinter window, hourly grid, table
tests/                    python -m unittest discover -s tests -t .
```

Punches are assigned to the shift they overlap most. If a punch doesn't overlap any shift, it goes to the shift starting nearest to it, within 3 hours. A punch with no shift in range is reported as *Unscheduled*.
