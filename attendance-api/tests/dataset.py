"""Builds a ~100k-record attendance dataset full of the awkward cases the rules describe.

    python -m tests.dataset            # loads into MONGO_DB (default attendance_big)

Cases covered: mid-month and future joiners, employees with no logs at all, a department with
no logs, overnight shifts, weekend records, missing weekdays, ABSENT / LEAVE, half days, open
records, legacy records without history / half_day / late_minutes, millisecond instants, logs of
employees that do not exist, a department name that differs only by case, ties on late minutes
(crafted in July 2026) and records whose stored late_minutes disagree with their punch times
(analytics must read stored values).
"""
import os
import random
from datetime import date, datetime, timedelta, timezone

from pymongo import MongoClient

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PERIOD_START, PERIOD_END = date(2026, 4, 1), date(2026, 9, 30)
SHIFTS = [("09:30", "18:30")] * 14 + [("10:00", "19:00")] * 3 + [("22:00", "06:00")] * 2 + [("20:00", "04:30")]
DEPARTMENTS = ["Engineering", "Sales", "Support", "Finance", "HR", "Operations", "engineering"]


def _at(day: date, hhmm: str, plus: timedelta = timedelta()) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return (datetime(day.year, day.month, day.day, h, m, tzinfo=IST) + plus).astimezone(UTC)


def _derive(day, emp, punch_in, punch_out):
    start = _at(day, emp["shift_start"])
    end = _at(day, emp["shift_end"])
    if end <= start:
        end += timedelta(days=1)
    pin = punch_in.replace(microsecond=0)
    late_s = int((pin - start).total_seconds())
    late = late_s // 60 if late_s > 600 else 0
    if punch_out is None:
        return None, late, 0, False
    pout = punch_out.replace(microsecond=0)
    secs = int((pout - pin).total_seconds())
    wh = (secs * 2 + 36) // 72 / 100  # half-up to 2 decimals
    ot = int((pout - end).total_seconds()) // 60
    return wh, late, ot if ot >= 30 else 0, wh < 4.5


def build(seed: int = 7, n_employees: int = 1000):
    rnd = random.Random(seed)
    employees, logs = [], []
    for i in range(1, n_employees + 1):
        dept = DEPARTMENTS[i % 6] if i % 97 else "engineering"
        kind = rnd.random()
        if kind < 0.70:
            joined = date(2025, 1, 1) + timedelta(days=rnd.randrange(300))
        elif kind < 0.90:
            joined = PERIOD_START + timedelta(days=rnd.randrange((PERIOD_END - PERIOD_START).days))
        elif kind < 0.95:
            joined = date(2026, 12, 1) + timedelta(days=rnd.randrange(30))  # joins after the period
        else:
            joined = date(2025, 6, 1)  # never punches (zero logs)
        shift = rnd.choice(SHIFTS)
        employees.append({
            "emp_code": f"EMP{i:04d}", "name": f"Person {i}", "email": f"p{i}@example.com", "department": dept,
            "shift_start": shift[0], "shift_end": shift[1], "joined_on": joined.isoformat(),
            "created_at": datetime(2025, 1, 1, tzinfo=UTC), "_no_logs": kind >= 0.95,
        })
    employees.append({"emp_code": "EMP7001", "name": "Lead", "email": "l@example.com", "department": "Legal",
                      "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-08-17",
                      "created_at": datetime(2026, 8, 17, tzinfo=UTC), "_no_logs": False})
    employees.append({"emp_code": "EMP7002", "name": "Keeper", "email": "k@example.com", "department": "Facilities",
                      "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2025-01-01",
                      "created_at": datetime(2025, 1, 1, tzinfo=UTC), "_no_logs": True})

    for emp in employees:
        if emp.pop("_no_logs"):
            continue
        day = max(PERIOD_START, date.fromisoformat(emp["joined_on"]))
        while day <= PERIOD_END:
            weekend = day.weekday() >= 5
            r = rnd.random()
            if weekend and r > 0.04 or not weekend and r < 0.06:
                day += timedelta(days=1)
                continue  # no record at all
            doc = {"emp_code": emp["emp_code"], "date": day.isoformat()}
            if not weekend and r < 0.09:
                doc.update(status="ABSENT", punch_in=None, punch_out=None, work_hours=None,
                           late_minutes=0, overtime_minutes=0, half_day=False)
            elif not weekend and r < 0.13:
                doc.update(status="LEAVE", punch_in=None, punch_out=None, work_hours=None,
                           late_minutes=0, overtime_minutes=0, half_day=False)
            else:
                status = rnd.choices(["PRESENT", "WFH", "ON_DUTY"], [75, 18, 7])[0]
                offset = timedelta(seconds=rnd.randint(-900, 600))
                if rnd.random() < 0.15:
                    offset = timedelta(seconds=rnd.randint(540, 5400))  # late, incl. around the grace edge
                punch_in = _at(day, emp["shift_start"], offset)
                if rnd.random() < 0.1:
                    punch_in += timedelta(milliseconds=rnd.randint(1, 999))
                q = rnd.random()
                if q < 0.03:
                    punch_out = None
                elif q < 0.10:
                    punch_out = punch_in + timedelta(seconds=rnd.randint(7200, 16500))  # half day
                else:
                    punch_out = punch_in + timedelta(seconds=rnd.randint(28800, 36000))
                wh, late, ot, half = _derive(day, emp, punch_in, punch_out)
                if rnd.random() < 0.01:
                    late = rnd.randint(11, 200)  # stored value disagrees with the punch times
                doc.update(status=status, punch_in=punch_in, punch_out=punch_out, work_hours=wh,
                           late_minutes=late, overtime_minutes=ot, half_day=half)
            doc["history"] = []
            if rnd.random() < 0.10:  # legacy record
                del doc["history"]
                del doc["half_day"]
                if doc["late_minutes"] == 0 and doc["overtime_minutes"] == 0 and rnd.random() < 0.5:
                    del doc["late_minutes"], doc["overtime_minutes"]
            logs.append(doc)
            day += timedelta(days=1)

    for i in range(40):  # logs of employees that do not exist
        logs.append({"emp_code": f"EMP9{i:05d}", "date": f"2026-07-{i % 28 + 1:02d}", "status": "PRESENT",
                     "punch_in": datetime(2026, 7, i % 28 + 1, 5, 0, tzinfo=UTC), "punch_out": None,
                     "work_hours": None, "late_minutes": 999, "overtime_minutes": 0, "half_day": False, "history": []})

    _craft_july_ties(logs, employees)
    return employees, logs


def _craft_july_ties(logs, employees):
    """Give 10 Sales employees July late totals with ties at several ranks, above everyone else."""
    sales = [e["emp_code"] for e in employees if e["department"] == "Sales" and e["joined_on"] <= "2026-06-30"]
    by_emp = {}
    for doc in logs:
        if doc["date"].startswith("2026-07") and doc["status"] in ("PRESENT", "WFH", "ON_DUTY"):
            by_emp.setdefault(doc["emp_code"], []).append(doc)
    targets = [5000, 4800, 4800, 4500, 4500, 4500, 4000, 3900, 3900, 3900]
    chosen = [code for code in sales if len(by_emp.get(code, [])) >= 2][:len(targets)]
    for code, target in zip(chosen, targets):
        july = by_emp[code]
        current = sum(d.get("late_minutes") or 0 for d in july)
        july[0]["late_minutes"] = (july[0].get("late_minutes") or 0) + target - current
    # And two employees outside Sales tied with Sales members, so department filters matter.
    others = [e["emp_code"] for e in employees if e["department"] == "Support" and by_emp.get(e["emp_code"])]
    for code in others[:2]:
        july = by_emp[code]
        current = sum(d.get("late_minutes") or 0 for d in july)
        july[0]["late_minutes"] = (july[0].get("late_minutes") or 0) + 4500 - current


def load(db) -> tuple[int, int]:
    employees, logs = build()
    for name in ("employees", "attendance_logs"):
        db.drop_collection(name)
    db.employees.insert_many(employees)
    db.attendance_logs.insert_many(logs)
    return len(employees), len(logs)


if __name__ == "__main__":
    database = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))[os.getenv("MONGO_DB", "attendance_big")]
    print("loaded %d employees and %d attendance records into %s" % (*load(database), database.name))
