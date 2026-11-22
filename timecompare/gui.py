"""Tkinter desktop app: weekly hourly grid of schedule (blue) vs. clocked time (green)."""

from __future__ import annotations

import csv
import math
import sys
import tkinter as tk
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .compare import Result, compare
from .db import Database
from .importer import ImportError_, load_records

BLUE = "#4a8fe0"
BLUE_EDGE = "#2c6cb8"
GREEN = "#27a844"
GREEN_EDGE = "#1a7a30"
RED = "#d62828"
ORANGE = "#e08a00"
GRID = "#d9dde3"
GRID_HOUR6 = "#aab2bd"
ROW_ALT = "#f6f8fa"
LABEL_W = 190
ROW_H = 36
HEADER_H = 26
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
ALL = "— All employees —"


def fmt_t(d: datetime | None) -> str:
    if d is None:
        return ""
    h = d.hour % 12 or 12
    return f"{h}:{d.minute:02d}{'a' if d.hour < 12 else 'p'}"


def fmt_delta(m: int | None) -> str:
    if m is None:
        return ""
    return "0" if m == 0 else f"{m:+d}m"


def app_dir() -> Path:
    if getattr(sys, "frozen", False):  # PyInstaller .exe
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


class App(tk.Tk):
    def __init__(self, db_path: Path):
        super().__init__()
        self.title(f"Schedule vs. Timesheet  v{__version__}  —  {db_path.name}")
        self.geometry("1300x820")
        self.minsize(900, 560)
        self.db = Database(db_path)
        self.results: list[Result] = []
        self.hover_text: dict[int, str] = {}

        self.threshold = tk.IntVar(value=int(self.db.get_setting("threshold_min", "15")))
        self.week_day = tk.StringVar(value=self.db.get_setting("week_start", "Mon"))
        self.employee = tk.StringVar(value=ALL)
        self.week_label = tk.StringVar()
        self.status = tk.StringVar(value="Import a schedule and a timesheet to begin.")
        self.totals = tk.StringVar()

        self._build_ui()
        dates = self.db.weeks_with_data()
        self.week_start = self._week_of(max(dates) if dates else date.today())
        self.refresh()

    # ---------------- layout ----------------
    def _build_ui(self):
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Treeview", rowheight=22)

        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(fill="x")
        ttk.Button(bar, text="Import Schedule…", command=lambda: self.do_import("schedule")).pack(side="left")
        ttk.Button(bar, text="Import Timesheet…", command=lambda: self.do_import("timesheet")).pack(side="left", padx=(4, 12))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(bar, text="◀", width=3, command=lambda: self.shift_week(-1)).pack(side="left")
        ttk.Label(bar, textvariable=self.week_label, width=26, anchor="center",
                  font=("Segoe UI", 10, "bold")).pack(side="left")
        ttk.Button(bar, text="▶", width=3, command=lambda: self.shift_week(1)).pack(side="left")
        ttk.Button(bar, text="This week", command=self.goto_today).pack(side="left", padx=4)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Label(bar, text="Employee:").pack(side="left")
        self.emp_box = ttk.Combobox(bar, textvariable=self.employee, state="readonly", width=26)
        self.emp_box.pack(side="left", padx=4)
        self.emp_box.bind("<<ComboboxSelected>>", lambda e: self.refresh())

        ttk.Button(bar, text="Export CSV…", command=self.export_csv).pack(side="right")
        # legend + options row
        row2 = ttk.Frame(self, padding=(8, 0))
        row2.pack(fill="x")
        leg = tk.Canvas(row2, height=22, width=700, highlightthickness=0,
                        bg=style.lookup("TFrame", "background") or self["bg"])
        leg.pack(side="left")
        leg.create_rectangle(4, 5, 30, 18, fill=BLUE, outline=BLUE_EDGE)
        leg.create_text(36, 11, text="Scheduled", anchor="w")
        leg.create_rectangle(120, 8, 146, 15, fill=GREEN, outline=GREEN_EDGE)
        leg.create_text(152, 11, text="Clocked in", anchor="w")
        self._flag(leg, 245, 3, 19, "left")
        self.legend_flag = leg.create_text(252, 11, anchor="w")
        leg.create_text(520, 11, text="?  = missing clock-out", anchor="w", fill=ORANGE)
        self.legend = leg
        opts = ttk.Frame(row2)
        opts.pack(side="right")
        ttk.Label(opts, text="Flag if >").pack(side="left")
        sp = ttk.Spinbox(opts, from_=0, to=120, width=4, textvariable=self.threshold,
                         command=self._settings_changed)
        sp.pack(side="left")
        sp.bind("<Return>", lambda e: self._settings_changed())
        sp.bind("<FocusOut>", lambda e: self._settings_changed())
        ttk.Label(opts, text="min   Week starts").pack(side="left")
        wk = ttk.Combobox(opts, textvariable=self.week_day, values=["Sun", "Mon"],
                          state="readonly", width=4)
        wk.pack(side="left", padx=(4, 0))
        wk.bind("<<ComboboxSelected>>", lambda e: self._settings_changed())


        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, padx=8, pady=(2, 0))

        grid_frame = ttk.Frame(paned)
        self.header = tk.Canvas(grid_frame, height=HEADER_H, bg="white", highlightthickness=0)
        self.header.pack(fill="x")
        body = ttk.Frame(grid_frame)
        body.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(body, bg="white", highlightthickness=0)
        vs = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vs.set)
        vs.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self.draw())
        self.canvas.bind("<Motion>", self._on_hover)
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))
        self.canvas.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))
        paned.add(grid_frame, weight=3)

        tree_frame = ttk.Frame(paned)
        cols = ("emp", "date", "sched", "actual", "ind", "outd", "sh", "ah", "status")
        heads = ("Employee", "Date", "Scheduled", "Clocked", "In Δ", "Out Δ",
                 "Sched h", "Actual h", "Status")
        widths = (170, 95, 130, 130, 60, 60, 65, 65, 160)
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings", height=8)
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w" if c in ("emp", "status") else "center")
        self.tree.tag_configure("flag", foreground=RED)
        self.tree.tag_configure("warn", foreground=ORANGE)
        ts = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ts.set)
        ts.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        paned.add(tree_frame, weight=2)

        foot = ttk.Frame(self, padding=(8, 3))
        foot.pack(fill="x")
        ttk.Label(foot, textvariable=self.status).pack(side="left")
        ttk.Label(foot, textvariable=self.totals, font=("Segoe UI", 9, "bold")).pack(side="right")

    # ---------------- helpers ----------------
    def _week_of(self, d: date) -> date:
        first = 6 if self.week_day.get() == "Sun" else 0
        return d - timedelta(days=(d.weekday() - first) % 7)

    def _emp_id(self) -> str | None:
        v = self.employee.get()
        if v == ALL or not v:
            return None
        return v.rsplit("[", 1)[-1].rstrip("]")

    def _settings_changed(self):
        try:
            t = int(self.threshold.get())
        except (tk.TclError, ValueError):
            t = 15
            self.threshold.set(t)
        self.db.set_setting("threshold_min", t)
        self.db.set_setting("week_start", self.week_day.get())
        self.week_start = self._week_of(self.week_start)
        self.refresh()

    def shift_week(self, n: int):
        self.week_start += timedelta(days=7 * n)
        self.refresh()

    def goto_today(self):
        self.week_start = self._week_of(date.today())
        self.refresh()

    # ---------------- actions ----------------
    def do_import(self, kind: str):
        paths = filedialog.askopenfilenames(
            title=f"Select {kind} file(s)",
            filetypes=[("Spreadsheet / CSV", "*.csv *.tsv *.txt *.tab *.xlsx"), ("All files", "*.*")])
        if not paths:
            return
        total, warnings, first_date = 0, [], None
        for p in paths:
            try:
                recs, warns = load_records(p)
                n = (self.db.import_shifts if kind == "schedule" else self.db.import_punches)(
                    recs, Path(p).name)
            except (ImportError_, ValueError, OSError) as e:
                messagebox.showerror("Import failed", f"{Path(p).name}:\n{e}")
                continue
            total += n
            warnings += [f"{Path(p).name} {w}" for w in warns]
            if recs and first_date is None:
                first_date = min(r["start"] for r in recs).date()
        if first_date:
            self.week_start = self._week_of(first_date)
        self.refresh()
        msg = f"Imported {total} {kind} row(s)."
        self.status.set(msg)
        if warnings:
            more = f"\n…and {len(warnings) - 20} more" if len(warnings) > 20 else ""
            messagebox.showwarning("Some rows were skipped",
                                   msg + "\n\n" + "\n".join(warnings[:20]) + more)

    def export_csv(self):
        if not self.results:
            messagebox.showinfo("Export", "Nothing to export for this week.")
            return
        default = f"comparison_{self.week_start.isoformat()}.csv"
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=default,
                                            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        names = dict(self.db.employees())
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Employee ID", "Employee", "Date", "Sched Start", "Sched End",
                        "Clock In", "Clock Out", "In Delta (min)", "Out Delta (min)",
                        "Early In Flag", "Late Out Flag", "Sched Hours", "Actual Hours", "Status"])
            for r in self.results:
                w.writerow([r.employee_id, names.get(r.employee_id, r.employee_id),
                            r.day.isoformat(),
                            r.shift.start.strftime("%Y-%m-%d %H:%M") if r.shift else "",
                            r.shift.end.strftime("%Y-%m-%d %H:%M") if r.shift else "",
                            r.actual_in.strftime("%Y-%m-%d %H:%M") if r.actual_in else "",
                            r.actual_out.strftime("%Y-%m-%d %H:%M") if r.actual_out else "",
                            r.in_delta_min if r.in_delta_min is not None else "",
                            r.out_delta_min if r.out_delta_min is not None else "",
                            "Y" if r.early_in else "", "Y" if r.late_out else "",
                            f"{r.sched_hours:.2f}", f"{r.actual_hours:.2f}", r.status])
        self.status.set(f"Exported {len(self.results)} rows to {path}")

    # ---------------- data ----------------
    def refresh(self):
        emps = self.db.employees()
        self.names = dict(emps)
        self.emp_box["values"] = [ALL] + [f"{n}  [{i}]" for i, n in emps]
        we = self.week_start + timedelta(days=6)
        self.week_label.set(f"{self.week_start:%b %d} – {we:%b %d, %Y}")
        thr = int(self.threshold.get())
        self.legend.itemconfigure(self.legend_flag,
                                  text=f"In > {thr} min early  /  out > {thr} min late")

        emp = self._emp_id()
        shifts, punches = self.db.load_week(self.week_start, emp)
        res = compare(shifts, punches, thr)
        end = self.week_start + timedelta(days=7)
        self.results = [r for r in res if self.week_start <= r.day < end]
        self.db.save_comparisons(self.week_start, self.results, emp)
        self._fill_tree()
        self.draw()

    def _fill_tree(self):
        self.tree.delete(*self.tree.get_children())
        sh = ah = 0.0
        flags = 0
        for r in sorted(self.results, key=lambda r: (self.names.get(r.employee_id, ""), r.day)):
            sched = f"{fmt_t(r.shift.start)} – {fmt_t(r.shift.end)}" if r.shift else ""
            act = f"{fmt_t(r.actual_in)} – {fmt_t(r.actual_out) or '?'}" if r.punches else ""
            if len(r.punches) > 1:
                act += f"  ({len(r.punches)} punches)"
            tag = "flag" if (r.early_in or r.late_out) else (
                "warn" if r.status not in ("OK",) else "")
            flags += r.early_in + r.late_out
            sh += r.sched_hours
            ah += r.actual_hours
            self.tree.insert("", "end", tags=(tag,), values=(
                self.names.get(r.employee_id, r.employee_id), f"{r.day:%a %m/%d}", sched, act,
                fmt_delta(r.in_delta_min), fmt_delta(r.out_delta_min),
                f"{r.sched_hours:.2f}" if r.shift else "", f"{r.actual_hours:.2f}", r.status))
        self.totals.set(f"Week: scheduled {sh:.2f} h · clocked {ah:.2f} h · "
                        f"diff {ah - sh:+.2f} h · {flags} red flag(s)")

    # ---------------- drawing ----------------
    @staticmethod
    def _flag(c: tk.Canvas, x, y0, y1, direction: str, tags=()):
        c.create_line(x, y0, x, y1, fill=RED, width=2, tags=tags)
        d = -11 if direction == "left" else 11
        c.create_polygon(x, y0, x + d, y0 + 4, x, y0 + 8, fill=RED, outline=RED, tags=tags)

    def _rows(self) -> list[tuple[str, date]]:
        days = [self.week_start + timedelta(days=i) for i in range(7)]
        emp = self._emp_id()
        if emp is not None:
            return [(emp, d) for d in days]
        present: dict[str, set[date]] = {}
        for r in self.results:
            for iv in self._intervals(r):
                d = iv[0].date()
                while d <= (iv[1] - timedelta(seconds=1)).date():
                    present.setdefault(r.employee_id, set()).add(d)
                    d += timedelta(days=1)
        rows = []
        for e in sorted(present, key=lambda e: self.names.get(e, e)):
            rows += [(e, d) for d in days if d in present[e]]
        return rows

    @staticmethod
    def _intervals(r: Result):
        out = []
        if r.shift:
            out.append((r.shift.start, r.shift.end))
        for p in r.punches:
            out.append((p.clock_in, p.clock_out or p.clock_in + timedelta(minutes=1)))
        return out

    def _hour_range(self, rows) -> tuple[int, int]:
        row_days = {d for _, d in rows}
        lo, hi = 24.0, 0.0
        for r in self.results:
            for a, b in self._intervals(r):
                for d in row_days:
                    d0 = datetime.combine(d, datetime.min.time())
                    sa, sb = max(a, d0), min(b, d0 + timedelta(days=1))
                    if sa < sb:
                        lo = min(lo, (sa - d0).total_seconds() / 3600)
                        hi = max(hi, (sb - d0).total_seconds() / 3600)
        if lo >= hi:
            return 6, 20
        s, e = max(0, math.floor(lo) - 1), min(24, math.ceil(hi) + 1)
        while e - s < 10:
            s, e = max(0, s - 1), min(24, e + 1)
        return s, e

    def draw(self):
        c, hd = self.canvas, self.header
        c.delete("all")
        hd.delete("all")
        self.hover_text.clear()
        width = max(c.winfo_width(), 600)
        rows = self._rows()
        h0, h1 = self._hour_range(rows)
        px_h = (width - LABEL_W - 10) / (h1 - h0)

        def x_of(dt: datetime, day: date) -> float:
            hrs = (dt - datetime.combine(day, datetime.min.time())).total_seconds() / 3600
            return LABEL_W + (hrs - h0) * px_h

        # header (hour labels)
        for h in range(h0, h1 + 1):
            x = LABEL_W + (h - h0) * px_h
            lbl = "12a" if h % 24 == 0 else ("12p" if h == 12 else f"{h % 12}{'a' if h < 12 else 'p'}")
            hd.create_line(x, HEADER_H - 6, x, HEADER_H, fill=GRID_HOUR6)
            if h < h1:
                hd.create_text(x + 2, HEADER_H / 2 - 2, text=lbl, anchor="w",
                               fill="#555", font=("Segoe UI", 8))
        hd.create_text(8, HEADER_H / 2, text="Employee / Day", anchor="w",
                       font=("Segoe UI", 9, "bold"))

        if not rows:
            c.create_text(width / 2, 60, text="No schedule or timesheet data for this week.",
                          fill="#777", font=("Segoe UI", 11))
            c.configure(scrollregion=(0, 0, width, 120))
            return

        total_h = len(rows) * ROW_H
        prev_emp = None
        row_index = {}
        for i, (emp, d) in enumerate(rows):
            y = i * ROW_H
            row_index[(emp, d)] = y
            if i % 2:
                c.create_rectangle(0, y, width, y + ROW_H, fill=ROW_ALT, outline="")
            if emp != prev_emp and i:
                c.create_line(0, y, width, y, fill="#7d8794", width=2)
            label = f"{d:%a %m/%d}"
            if self._emp_id() is None:
                label = f"{self.names.get(emp, emp)[:20]}  ·  {label}" if emp != prev_emp else f"      {label}"
            c.create_text(8, y + ROW_H / 2, text=label, anchor="w", font=("Segoe UI", 9))
            prev_emp = emp
        for h in range(h0, h1 + 1):
            x = LABEL_W + (h - h0) * px_h
            c.create_line(x, 0, x, total_h, fill=GRID_HOUR6 if h % 6 == 0 else GRID)
        c.create_line(LABEL_W, 0, LABEL_W, total_h, fill="#7d8794")

        for r in self.results:
            name = self.names.get(r.employee_id, r.employee_id)
            info = (f"{name} {r.day:%a %m/%d} — "
                    + (f"scheduled {fmt_t(r.shift.start)}–{fmt_t(r.shift.end)}" if r.shift else "unscheduled")
                    + (f" | clocked {fmt_t(r.actual_in)}–{fmt_t(r.actual_out) or '?'}" if r.punches else " | no punches")
                    + (f" | in {fmt_delta(r.in_delta_min)}, out {fmt_delta(r.out_delta_min)}" if r.shift and r.punches else "")
                    + f" | {r.status}")
            # blue scheduled bar(s), split across midnight
            if r.shift:
                self._draw_span(c, r.shift.start, r.shift.end, r.employee_id, row_index, x_of,
                                4, ROW_H - 4, BLUE, BLUE_EDGE, info)
            for p in r.punches:
                end = p.clock_out or p.clock_in + timedelta(minutes=1)
                self._draw_span(c, p.clock_in, end, r.employee_id, row_index, x_of,
                                12, ROW_H - 12, GREEN, GREEN_EDGE, info)
                if p.clock_out is None:
                    y = row_index.get((r.employee_id, p.clock_in.date()))
                    if y is not None:
                        x = x_of(p.clock_in, p.clock_in.date())
                        t = c.create_text(x + 8, y + ROW_H / 2, text="?", fill=ORANGE,
                                          font=("Segoe UI", 12, "bold"))
                        self.hover_text[t] = info
            # red flags
            if r.early_in:
                self._draw_flag(c, r.actual_in, "left", r.employee_id, row_index, x_of,
                                fmt_delta(r.in_delta_min), info)
            if r.late_out:
                self._draw_flag(c, r.actual_out, "right", r.employee_id, row_index, x_of,
                                fmt_delta(r.out_delta_min), info)
        c.configure(scrollregion=(0, 0, width, total_h + 4))

    def _draw_span(self, c, a, b, emp, row_index, x_of, top, bot, fill, edge, info):
        d = a.date()
        while d <= (b - timedelta(seconds=1)).date():
            y = row_index.get((emp, d))
            d0 = datetime.combine(d, datetime.min.time())
            sa, sb = max(a, d0), min(b, d0 + timedelta(days=1))
            if y is not None and sa < sb:
                item = c.create_rectangle(x_of(sa, d), y + top, max(x_of(sb, d), x_of(sa, d) + 2),
                                          y + bot, fill=fill, outline=edge)
                self.hover_text[item] = info
            d += timedelta(days=1)

    def _draw_flag(self, c, when, direction, emp, row_index, x_of, text, info):
        d = when.date()
        y = row_index.get((emp, d))
        if y is None and when.time() == datetime.min.time():  # exactly midnight → prev row end
            d -= timedelta(days=1)
            y = row_index.get((emp, d))
        if y is None:
            return
        x = x_of(when, d)
        tag = f"flag{id(when)}{direction}"
        self._flag(c, x, y + 2, y + ROW_H - 2, direction, tags=(tag,))
        anchor = "e" if direction == "left" else "w"
        c.create_text(x + (-13 if direction == "left" else 13), y + ROW_H - 9, text=text,
                      anchor=anchor, fill=RED, font=("Segoe UI", 8, "bold"), tags=(tag,))
        for item in c.find_withtag(tag):
            self.hover_text[item] = info

    def _on_hover(self, e):
        items = self.canvas.find_overlapping(self.canvas.canvasx(e.x), self.canvas.canvasy(e.y),
                                             self.canvas.canvasx(e.x), self.canvas.canvasy(e.y))
        for it in reversed(items):
            if it in self.hover_text:
                self.status.set(self.hover_text[it])
                return


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Compare schedules to timesheets.")
    ap.add_argument("--db", type=Path, default=app_dir() / "timecompare.db",
                    help="SQLite database file (created if missing)")
    args = ap.parse_args(argv)
    App(args.db).mainloop()


if __name__ == "__main__":
    main()
