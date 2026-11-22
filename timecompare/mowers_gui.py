"""'Mowers & Oil' tab: check-in import / Google Sheet sync, oil-change status, history."""

from __future__ import annotations

import threading
import tkinter as tk
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from .importer import ImportError_
from .mowers import DEFAULT_WARN_H, Mowers, fetch_checkins, load_checkins

RED = "#d62828"
ORANGE = "#c77700"
GREY = "#777777"


def fmt_h(h) -> str:
    return "" if h is None else f"{h:,.1f}"


class MowersTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.m = Mowers(app.db)
        self.show_retired = tk.BooleanVar(value=False)
        self.warn_h = tk.DoubleVar(value=float(app.db.get_setting("oil_warn_h", str(DEFAULT_WARN_H))))
        self.banner = tk.StringVar()
        self.selected: int | None = None
        self._syncing = False
        self._build()
        self.refresh()

    # ------------------------------------------------------------ layout
    def _build(self):
        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(fill="x")
        ttk.Button(bar, text="Import Check-ins…", command=self.import_file).pack(side="left")
        self.sync_btn = ttk.Button(bar, text="Sync from Google Sheet", command=self.sync_sheet)
        self.sync_btn.pack(side="left", padx=4)
        ttk.Button(bar, text="Sheet link…", command=self.set_sheet_link).pack(side="left")
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Button(bar, text="Record Oil Change…", command=self.oil_change).pack(side="left")
        ttk.Button(bar, text="Oil Interval…", command=self.edit_interval).pack(side="left", padx=4)
        ttk.Button(bar, text="Add Mower…", command=self.add_mower).pack(side="left")
        ttk.Button(bar, text="Rename…", command=self.rename).pack(side="left", padx=4)
        ttk.Button(bar, text="Retire / Restore", command=self.toggle_retired).pack(side="left")

        opts = ttk.Frame(self, padding=(8, 0))
        opts.pack(fill="x")
        ttk.Label(opts, text="Warn when an oil change is within").pack(side="left")
        sp = ttk.Spinbox(opts, from_=0, to=100, increment=1, width=4, textvariable=self.warn_h,
                         command=self._warn_changed)
        sp.pack(side="left", padx=3)
        sp.bind("<Return>", lambda e: self._warn_changed())
        sp.bind("<FocusOut>", lambda e: self._warn_changed())
        ttk.Label(opts, text="hours").pack(side="left")
        ttk.Checkbutton(opts, text="Show retired mowers", variable=self.show_retired,
                        command=self.refresh).pack(side="left", padx=(20, 0))

        self.banner_lbl = tk.Label(self, textvariable=self.banner, anchor="w", padx=10, pady=4,
                                   font=("Segoe UI", 10, "bold"))
        self.banner_lbl.pack(fill="x", padx=8)

        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, padx=8, pady=(2, 0))

        top = ttk.Frame(paned)
        cols = ("name", "cur", "last", "next", "left", "status", "seen")
        heads = ("Mower", "Hour meter now", "Last oil change", "Next change at",
                 "Hours left", "Status", "Last check-in")
        widths = (160, 115, 240, 110, 90, 120, 240)
        self.tree = ttk.Treeview(top, columns=cols, show="headings", height=8, selectmode="browse")
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w" if c in ("name", "last", "seen", "status") else "center")
        self.tree.tag_configure("over", foreground=RED)
        self.tree.tag_configure("soon", foreground=ORANGE)
        self.tree.tag_configure("none", foreground=GREY)
        self.tree.tag_configure("retired", foreground=GREY)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())
        self.tree.bind("<Double-1>", lambda e: self.oil_change())
        self.tree.pack(fill="both", expand=True)
        paned.add(top, weight=2)

        bottom = ttk.Frame(paned)
        hb = ttk.Frame(bottom)
        hb.pack(fill="x", pady=(6, 2))
        self.hist_title = ttk.Label(hb, text="Select a mower to see its check-ins and oil changes.",
                                    font=("Segoe UI", 9, "bold"))
        self.hist_title.pack(side="left")
        ttk.Button(hb, text="Delete selected entry", command=self.delete_entry).pack(side="right")
        hcols = ("when", "what", "who", "hours", "note")
        hheads = ("Date / time", "Event", "Who", "Hour meter", "Note")
        hw = (150, 100, 170, 100, 420)
        self.hist = ttk.Treeview(bottom, columns=hcols, show="headings", height=8)
        for c, h, w in zip(hcols, hheads, hw):
            self.hist.heading(c, text=h)
            self.hist.column(c, width=w, anchor="center" if c in ("hours",) else "w")
        self.hist.tag_configure("oil", foreground="#1a7a30")
        self.hist.tag_configure("bad", foreground=RED)
        vs = ttk.Scrollbar(bottom, orient="vertical", command=self.hist.yview)
        self.hist.configure(yscrollcommand=vs.set)
        vs.pack(side="right", fill="y")
        self.hist.pack(fill="both", expand=True)
        paned.add(bottom, weight=3)

    # ------------------------------------------------------------ data
    def refresh(self):
        keep = self.selected
        self.tree.delete(*self.tree.get_children())
        statuses = self.m.status(include_inactive=self.show_retired.get())
        active = {r["id"] for r in self.app.db.con.execute("SELECT id FROM mowers WHERE active=1")}
        due = []
        for s in statuses:
            if s.id not in active:
                tag, status = "retired", "Retired"
            else:
                status = s.status
                tag = {"OVERDUE": "over", "Due soon": "soon", "No readings yet": "none"}.get(status, "")
                if status in ("OVERDUE", "Due soon"):
                    due.append(s)
            if s.last_change_on:
                last = f"{fmt_h(s.last_change_h)} h on {date.fromisoformat(s.last_change_on):%m/%d/%Y}"
            elif s.last_change_h is not None:
                last = f"not logged (counting from {fmt_h(s.last_change_h)} h)"
            else:
                last = "not logged"
            left = s.hours_left
            seen = (f"{s.last_checkin_at:%a %m/%d %I:%M %p} · {s.last_checkin_by}"
                    if s.last_checkin_at else "")
            self.tree.insert("", "end", iid=str(s.id), tags=(tag,), values=(
                s.name, fmt_h(s.current_h), last, fmt_h(s.next_due_h),
                "" if left is None else (f"{left:,.1f}" if left > 0 else f"{-left:,.1f} over"),
                status, seen))
        if keep is not None and self.tree.exists(str(keep)):
            self.tree.selection_set(str(keep))
        self._show_history()

        if due:
            parts = [f"{s.name} is OVERDUE ({fmt_h(-s.hours_left)} h over)" if s.status == "OVERDUE"
                     else f"{s.name} is due soon ({fmt_h(s.hours_left)} h left)" for s in due]
            self.banner.set("Oil change needed:  " + "   ·   ".join(parts))
            self.banner_lbl.configure(fg="white", bg=RED if any(s.status == "OVERDUE" for s in due) else ORANGE)
        elif statuses:
            self.banner.set("All mowers are OK for oil.")
            self.banner_lbl.configure(fg="#1a7a30", bg=self.app.cget("bg"))
        else:
            self.banner.set("No mowers yet. Import check-ins from the phone form, or Add Mower.")
            self.banner_lbl.configure(fg=GREY, bg=self.app.cget("bg"))
        self.app.on_mowers_changed(len(due))

    def _on_select(self):
        sel = self.tree.selection()
        self.selected = int(sel[0]) if sel else None
        self._show_history()

    def _show_history(self):
        self.hist.delete(*self.hist.get_children())
        if self.selected is None:
            return
        name = self.tree.set(str(self.selected), "name") if self.tree.exists(str(self.selected)) else ""
        self.hist_title.configure(text=f"History — {name}")
        self._hist_rows = self.m.history(self.selected)
        for i, h in enumerate(self._hist_rows):
            when = (f"{h['when']:%m/%d/%Y}" if h["what"] == "Oil change"
                    else f"{h['when']:%m/%d/%Y %I:%M %p}")
            tag = "oil" if h["what"] == "Oil change" else ("bad" if h["note"] else "")
            self.hist.insert("", "end", iid=str(i), tags=(tag,),
                             values=(when, h["what"], h["who"], fmt_h(h["hours"]), h["note"]))

    def _need_mower(self) -> int | None:
        if self.selected is None:
            messagebox.showinfo("Select a mower", "Click a mower in the list first.")
        return self.selected

    def _warn_changed(self):
        try:
            v = float(self.warn_h.get())
        except (tk.TclError, ValueError):
            v = DEFAULT_WARN_H
            self.warn_h.set(v)
        self.app.db.set_setting("oil_warn_h", v)
        self.refresh()

    # ------------------------------------------------------------ check-ins
    def _apply(self, recs, warns, source):
        added, notes = self.m.import_checkins(recs, source)
        self.refresh()
        self.app.refresh()
        msg = f"{source}: {added} new check-in(s)" + (
            f", {len(recs) - added} already imported" if len(recs) - added else "") + "."
        self.app.status.set(msg)
        extra = notes + warns[:15] + ([f"…and {len(warns) - 15} more"] if len(warns) > 15 else [])
        if extra:
            messagebox.showinfo("Check-ins imported", msg + "\n\n" + "\n\n".join(extra))

    def import_file(self):
        paths = filedialog.askopenfilenames(
            title="Select check-in file(s) (Google Form responses as CSV)",
            filetypes=[("Spreadsheet / CSV", "*.csv *.tsv *.txt *.xlsx"), ("All files", "*.*")])
        for p in paths:
            try:
                recs, warns = load_checkins(p)
            except (ImportError_, ValueError, OSError) as e:
                messagebox.showerror("Import failed", f"{Path(p).name}:\n{e}")
                continue
            self._apply(recs, [f"{Path(p).name} {w}" for w in warns], Path(p).name)

    def set_sheet_link(self):
        cur = self.app.db.get_setting("checkin_sheet_url", "")
        url = simpledialog.askstring(
            "Google Sheet link",
            "Paste the link of the Google Sheet that collects the check-in form responses.\n\n"
            "In Google Sheets: Share → General access → 'Anyone with the link' (Viewer),\n"
            "then Copy link. Leave blank to turn syncing off.",
            initialvalue=cur, parent=self)
        if url is None:
            return
        self.app.db.set_setting("checkin_sheet_url", url.strip())
        if url.strip():
            self.sync_sheet()

    def sync_sheet(self, quiet: bool = False):
        url = self.app.db.get_setting("checkin_sheet_url", "")
        if not url:
            if not quiet:
                self.set_sheet_link()
            return
        if self._syncing:
            return
        self._syncing = True
        self.sync_btn.configure(state="disabled", text="Syncing…")
        self.app.status.set("Syncing check-ins from Google Sheet…")

        def work():
            try:
                result = ("ok", fetch_checkins(url))
            except Exception as e:  # network, auth, parse — report in the UI thread
                result = ("err", e)
            self.after(0, lambda: self._sync_done(result, quiet))

        threading.Thread(target=work, daemon=True).start()

    def _sync_done(self, result, quiet):
        self._syncing = False
        self.sync_btn.configure(state="normal", text="Sync from Google Sheet")
        kind, val = result
        if kind == "err":
            self.app.status.set(f"Sync failed: {val}")
            if not quiet:
                messagebox.showerror("Sync failed", str(val))
            return
        recs, warns = val
        if quiet:
            added, notes = self.m.import_checkins(recs, "Google Sheet")
            self.refresh()
            self.app.refresh()
            self.app.status.set(f"Synced: {added} new mower check-in(s) from Google Sheet.")
        else:
            self._apply(recs, warns, "Google Sheet")

    # ------------------------------------------------------------ mower edits
    def add_mower(self):
        name = simpledialog.askstring("Add mower", "Mower name (exactly as on the phone form):",
                                      parent=self)
        if not name or not name.strip():
            return
        interval = simpledialog.askfloat("Oil interval", "Change oil every how many hours?",
                                         initialvalue=50, minvalue=1, parent=self)
        if interval is None:
            return
        self.selected = self.m.add(name, interval)
        self.refresh()
        if messagebox.askyesno("Last oil change", "Do you know the hour meter at its last oil change?"):
            self.oil_change()

    def rename(self):
        mid = self._need_mower()
        if mid is None:
            return
        cur = self.tree.set(str(mid), "name")
        name = simpledialog.askstring("Rename mower", "New name (must match the phone form):",
                                      initialvalue=cur, parent=self)
        if name and name.strip() and name.strip() != cur:
            try:
                self.m.rename(mid, name)
            except Exception:
                messagebox.showerror("Rename", f"A mower named '{name}' already exists.")
            self.refresh()

    def toggle_retired(self):
        mid = self._need_mower()
        if mid is None:
            return
        active = self.app.db.con.execute("SELECT active FROM mowers WHERE id=?", (mid,)).fetchone()[0]
        self.m.set_active(mid, not active)
        if active and not self.show_retired.get():
            self.selected = None
        self.refresh()

    def edit_interval(self):
        mid = self._need_mower()
        if mid is None:
            return
        cur = self.app.db.con.execute("SELECT oil_interval_h FROM mowers WHERE id=?", (mid,)).fetchone()[0]
        v = simpledialog.askfloat("Oil interval", "Change oil every how many hours?",
                                  initialvalue=cur, minvalue=1, parent=self)
        if v:
            self.m.set_interval(mid, v)
            self.refresh()

    def oil_change(self):
        mid = self._need_mower()
        if mid is None:
            return
        name = self.tree.set(str(mid), "name")
        cur = self.tree.set(str(mid), "cur").replace(",", "")
        OilDialog(self, name, cur, lambda d, h, n: (self.m.record_oil_change(mid, d, h, n),
                                                    self.refresh()))

    def delete_entry(self):
        sel = self.hist.selection()
        if self.selected is None or not sel:
            messagebox.showinfo("Delete", "Select a line in the history first.")
            return
        h = self._hist_rows[int(sel[0])]
        if messagebox.askyesno("Delete entry",
                               f"Delete this {h['what'].lower()} ({fmt_h(h['hours'])} h)?\n\n"
                               "Tip: a check-in also lives in the Google Sheet; delete it there "
                               "too or it will come back on the next sync."):
            self.m.delete_checkin_or_change(h["what"], self.selected, h["when"], h["hours"])
            self.refresh()


class OilDialog(tk.Toplevel):
    def __init__(self, parent, mower: str, hours: str, on_save):
        super().__init__(parent)
        self.title(f"Oil change — {mower}")
        self.resizable(False, False)
        self.transient(parent.winfo_toplevel())
        self.on_save = on_save
        f = ttk.Frame(self, padding=14)
        f.pack()
        self.d = tk.StringVar(value=date.today().strftime("%m/%d/%Y"))
        self.h = tk.StringVar(value=hours)
        self.n = tk.StringVar()
        for r, (lbl, var, w) in enumerate((("Date (MM/DD/YYYY)", self.d, 14),
                                           ("Hour meter at change", self.h, 14),
                                           ("Notes (filter, oil type…)", self.n, 34))):
            ttk.Label(f, text=lbl).grid(row=r, column=0, sticky="w", pady=3)
            e = ttk.Entry(f, textvariable=var, width=w)
            e.grid(row=r, column=1, sticky="w", pady=3, padx=(8, 0))
            if r == 1:
                e.focus_set()
                e.select_range(0, "end")
        b = ttk.Frame(f)
        b.grid(row=3, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(b, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(b, text="Save", command=self.save).pack(side="right", padx=6)
        self.bind("<Return>", lambda e: self.save())
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()

    def save(self):
        try:
            d = datetime.strptime(self.d.get().strip(), "%m/%d/%Y").date()
        except ValueError:
            messagebox.showerror("Date", "Enter the date as MM/DD/YYYY.", parent=self)
            return
        try:
            h = float(self.h.get().replace(",", ""))
        except ValueError:
            messagebox.showerror("Hours", "Enter the hour meter reading as a number.", parent=self)
            return
        self.on_save(d, h, self.n.get().strip())
        self.destroy()
