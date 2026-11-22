"""Match clock punches to scheduled shifts and compute flags."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

# A punch with no overlap is still tied to a shift if it starts this close.
NEAR_WINDOW = timedelta(hours=3)


@dataclass
class Shift:
    id: int
    employee_id: str
    start: datetime
    end: datetime


@dataclass
class Punch:
    id: int
    employee_id: str
    clock_in: datetime
    clock_out: datetime | None


@dataclass
class Result:
    employee_id: str
    shift: Shift | None
    punches: list[Punch] = field(default_factory=list)
    threshold_min: int = 15

    @property
    def actual_in(self):
        return min(p.clock_in for p in self.punches) if self.punches else None

    @property
    def actual_out(self):
        if not self.punches or any(p.clock_out is None for p in self.punches):
            return None
        return max(p.clock_out for p in self.punches)

    @property
    def in_delta_min(self):
        """Minutes clock-in was relative to scheduled start (negative = early)."""
        if self.shift and self.actual_in:
            return round((self.actual_in - self.shift.start).total_seconds() / 60)
        return None

    @property
    def out_delta_min(self):
        """Minutes clock-out was relative to scheduled end (positive = stayed late)."""
        if self.shift and self.actual_out:
            return round((self.actual_out - self.shift.end).total_seconds() / 60)
        return None

    @property
    def early_in(self) -> bool:
        d = self.in_delta_min
        return d is not None and d < -self.threshold_min

    @property
    def late_out(self) -> bool:
        d = self.out_delta_min
        return d is not None and d > self.threshold_min

    @property
    def sched_hours(self) -> float:
        return (self.shift.end - self.shift.start).total_seconds() / 3600 if self.shift else 0.0

    @property
    def actual_hours(self) -> float:
        return sum((p.clock_out - p.clock_in).total_seconds() / 3600
                   for p in self.punches if p.clock_out)

    @property
    def status(self) -> str:
        if self.shift is None:
            return "Unscheduled"
        if not self.punches:
            return "No punches"
        if any(p.clock_out is None for p in self.punches):
            return "Missing clock-out"
        flags = []
        if self.early_in:
            flags.append("Early in")
        if self.late_out:
            flags.append("Late out")
        return ", ".join(flags) or "OK"

    @property
    def day(self):
        return (self.shift.start if self.shift else self.punches[0].clock_in).date()


def _overlap(a0, a1, b0, b1) -> float:
    return max(0.0, (min(a1, b1) - max(a0, b0)).total_seconds())


def compare(shifts: list[Shift], punches: list[Punch], threshold_min: int = 15) -> list[Result]:
    by_emp: dict[str, list[Shift]] = {}
    for s in shifts:
        by_emp.setdefault(s.employee_id, []).append(s)

    results = {s.id: Result(s.employee_id, s, threshold_min=threshold_min) for s in shifts}
    unscheduled: list[Result] = []

    for p in sorted(punches, key=lambda p: p.clock_in):
        p_end = p.clock_out or p.clock_in + timedelta(minutes=1)
        best, best_score = None, 0.0
        for s in by_emp.get(p.employee_id, []):
            ov = _overlap(p.clock_in, p_end, s.start, s.end)
            if ov > best_score:
                best, best_score = s, ov
        if best is None:  # no overlap: nearest shift start within window
            cands = [s for s in by_emp.get(p.employee_id, [])
                     if abs(s.start - p.clock_in) <= NEAR_WINDOW
                     or (p.clock_out and abs(s.end - p.clock_out) <= NEAR_WINDOW)]
            if cands:
                best = min(cands, key=lambda s: abs(s.start - p.clock_in))
        if best is not None:
            results[best.id].punches.append(p)
        else:
            unscheduled.append(Result(p.employee_id, None, [p], threshold_min))

    out = list(results.values()) + unscheduled
    out.sort(key=lambda r: (r.employee_id, r.shift.start if r.shift else r.punches[0].clock_in))
    return out
