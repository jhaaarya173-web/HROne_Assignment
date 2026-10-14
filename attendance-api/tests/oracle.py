"""Expected analytics computed in plain Python, straight from the rules, to check the pipelines against.

Written independently of app/main.py on purpose: it loops over documents (which the API must not
do) and uses only the stored derived values, exactly as the rules define the analytics.
"""
from calendar import monthrange
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}


def half_up(value: Decimal, places: int) -> float:
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def _days(first: date, last: date):
    d = first
    while d <= last:
        yield d
        d += timedelta(days=1)


def _month(month: str):
    y, m = map(int, month.split("-"))
    return date(y, m, 1), date(y, m, monthrange(y, m)[1])


def _credit(doc) -> Decimal:
    return Decimal("0.5") if doc.get("half_day") is True else Decimal(1)


def _weekday(doc) -> bool:
    return date.fromisoformat(doc["date"]).weekday() < 5


def _late(doc) -> int:
    return doc.get("late_minutes") or 0


def employee_monthly(employees, logs, emp_code, month):
    emp = next(e for e in employees if e["emp_code"] == emp_code)
    first, last = _month(month)
    start = max(first, date.fromisoformat(emp["joined_on"]))
    working = sum(1 for d in _days(start, last) if d.weekday() < 5)
    recs = [d for d in logs if d["emp_code"] == emp_code and first.isoformat() <= d["date"] <= last.isoformat()]
    present = sum((_credit(d) for d in recs if d["status"] in PRESENCE and _weekday(d)), Decimal(0))
    return {
        "emp_code": emp_code, "month": month, "working_days": working, "present_days": float(present),
        "leave_days": sum(1 for d in recs if d["status"] == "LEAVE"),
        "late_count": sum(1 for d in recs if _late(d) > 0),
        "total_late_minutes": sum(_late(d) for d in recs),
        "total_overtime_minutes": sum(d.get("overtime_minutes") or 0 for d in recs),
        "attendance_pct": half_up(present * 100 / working, 2) if working else None,
    }


def department_summary(employees, logs, month, department=None):
    first, last = _month(month)
    f, l = first.isoformat(), last.isoformat()
    members = {}
    for e in employees:
        if e["joined_on"] <= l and (department is None or e["department"] == department):
            members[e["emp_code"]] = e["department"]
    out = {}
    for dept in sorted(set(members.values())):
        out[dept] = {"department": dept, "headcount": sum(1 for v in members.values() if v == dept),
                     "present_days": Decimal(0), "wh": [], "late_count": 0, "total_late_minutes": 0,
                     "leave_count": 0, "on_duty_count": 0}
    for d in logs:
        dept = members.get(d["emp_code"])
        if dept is None or not f <= d["date"] <= l:
            continue
        row = out[dept]
        if d["status"] in PRESENCE and _weekday(d):
            row["present_days"] += _credit(d)
        if d["status"] in PRESENCE and isinstance(d.get("work_hours"), (int, float)):
            row["wh"].append(Decimal(str(d["work_hours"])))
        if _late(d) > 0:
            row["late_count"] += 1
            row["total_late_minutes"] += _late(d)
        row["leave_count"] += d["status"] == "LEAVE"
        row["on_duty_count"] += d["status"] == "ON_DUTY"
    items = []
    for row in out.values():
        wh = row.pop("wh")
        row["avg_work_hours"] = half_up(sum(wh) / len(wh), 2) if wh else None
        row["present_days"] = float(row["present_days"])
        items.append(row)
    return {"month": month, "items": items}


def late_leaderboard(employees, logs, month, limit=10, department=None):
    first, last = _month(month)
    f, l = first.isoformat(), last.isoformat()
    emps = {e["emp_code"]: e for e in employees}
    totals = {}
    for d in logs:
        e = emps.get(d["emp_code"])
        if e is None or not f <= d["date"] <= l or _late(d) <= 0:
            continue
        if department is not None and e["department"] != department:
            continue
        t = totals.setdefault(d["emp_code"], [0, 0])
        t[0] += _late(d)
        t[1] += 1
    ordered = sorted(totals.items(), key=lambda kv: (-kv[1][0], kv[0]))
    items = []
    for code, (total, count) in ordered:
        rank = 1 + sum(1 for _, (t, _) in ordered if t > total)  # competition ranking
        if rank <= limit:
            items.append({"rank": rank, "emp_code": code, "name": emps[code]["name"],
                          "department": emps[code]["department"], "total_late_minutes": total, "late_count": count})
    return {"month": month, "items": items}


def department_trend(employees, logs, department, start, end):
    members = {e["emp_code"]: e["joined_on"] for e in employees if e["department"] == department}
    by_day = {}
    for d in logs:
        if d["emp_code"] in members and start <= d["date"] <= end:
            by_day.setdefault(d["date"], []).append(d)
    rows, rates = [], []
    for day in _days(date.fromisoformat(start), date.fromisoformat(end)):
        iso = day.isoformat()
        headcount = sum(1 for j in members.values() if j <= iso)
        today = by_day.get(iso, [])
        present = sum((_credit(d) for d in today if d["status"] in PRESENCE), Decimal(0))
        working = day.weekday() < 5
        rate = present / headcount if working and headcount else None
        rate = Decimal(str(half_up(rate, 4))) if rate is not None else None
        rates.append(rate)
        window = [r for r in rates[-7:] if r is not None]
        rows.append({
            "date": iso, "is_working_day": working, "headcount": headcount, "present_count": float(present),
            "late_count": sum(1 for d in today if _late(d) > 0),
            "attendance_rate": float(rate) if rate is not None else None,
            "moving_avg_7d": half_up(sum(window) / len(window), 4) if window else None,
        })
    return {"department": department, "items": rows}
