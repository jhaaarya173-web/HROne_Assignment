"""Behaviour of the CRUD endpoints, rules R1 - R5, validation and concurrency."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx
import pytest

IST = timezone(timedelta(hours=5, minutes=30))


def ist(y, mo, d, h, mi, s=0, ms=0) -> int:
    """An IST wall-clock time as epoch milliseconds."""
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp()) * 1000 + ms


def employee(code, **overrides):
    body = {"emp_code": code, "name": "Test", "email": "t@example.com", "department": "QA", "joined_on": "2026-01-01"}
    return {**body, **overrides}


def parallel(calls):
    """Run the zero-argument callables at the same time; return their status codes."""
    with ThreadPoolExecutor(len(calls)) as pool:
        return [r.status_code for r in pool.map(lambda call: call(), calls)]


@pytest.fixture(scope="module", autouse=True)
def people(api):
    api.post("/employees", json=employee("EMP1001"))
    api.post("/employees", json=employee("EMP1002", department="Ops"))
    api.post("/employees", json=employee("EMP1003", shift_start="22:00", shift_end="06:00"))


# --------------------------------------------------------------------------- employees
def test_health(api):
    assert api.get("/health").json() == {"status": "ok"}


def test_create_employee_shape(api):
    r = api.post("/employees", json=employee("EMP2001", created_at=1, extra="ignored"))
    assert r.status_code == 201
    body = r.json()
    assert set(body) == {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"}
    assert (body["shift_start"], body["shift_end"]) == ("09:30", "18:30")
    assert isinstance(body["created_at"], int) and body["created_at"] > 1_700_000_000_000
    assert api.post("/employees", json=employee("EMP2001")).status_code == 409


@pytest.mark.parametrize("bad", [
    {"emp_code": "E1"}, {"emp_code": "EMP12"}, {"emp_code": "EMP1234567"}, {"email": "nope"},
    {"shift_start": "18:30", "shift_end": "18:30"}, {"shift_start": "24:00"}, {"shift_end": "9:30"},
    {"joined_on": "2026-02-30"}, {"joined_on": "05-01-2026"}, {"name": ""}, {"department": "x" * 51},
])
def test_create_employee_validation(api, bad):
    assert api.post("/employees", json=employee("EMP2002", **bad)).status_code == 422


def test_concurrent_creates_give_one_201(api):
    codes = parallel([lambda: httpx.post(str(api.base_url) + "/employees", json=employee("EMP2003"))] * 12)
    assert sorted(codes) == [201] + [409] * 11


def test_list_employees(api):
    for code in ("EMP3003", "EMP3001", "EMP3002"):
        api.post("/employees", json=employee(code, department="Paging"))
    first = api.get("/employees", params={"department": "Paging", "page_size": 2}).json()
    assert [e["emp_code"] for e in first["items"]] == ["EMP3001", "EMP3002"]
    assert (first["total"], first["page"], first["page_size"]) == (3, 1, 2)
    second = api.get("/employees", params={"department": "Paging", "page": 2, "page_size": 2}).json()
    assert [e["emp_code"] for e in second["items"]] == ["EMP3003"]
    assert api.get("/employees", params={"department": "paging"}).json()["total"] == 0  # case-sensitive
    for bad in ({"page": 0}, {"page_size": 0}, {"page_size": 101}, {"page": "x"}):
        assert api.get("/employees", params=bad).status_code == 422


# --------------------------------------------------------------------------- punch-in
@pytest.mark.parametrize("day, wall, late", [
    (6, (9, 40, 0), 0),          # exactly the grace period
    (7, (9, 40, 1), 10),         # one second past it: minutes since shift start
    (8, (10, 15, 59), 45),       # floor
    (9, (9, 0, 0), 0),           # early
])
def test_late_minutes(api, day, wall, late):
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP1001", "punched_at": ist(2026, 4, day, *wall)})
    assert r.status_code == 201
    assert r.json()["late_minutes"] == late


def test_punch_in_truncates_to_seconds_and_uses_ist_date(api):
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP1001", "punched_at": ist(2026, 4, 10, 9, 40, 0, 900)})
    assert r.json()["punch_in"] == ist(2026, 4, 10, 9, 40)
    assert r.json()["late_minutes"] == 0
    # 00:30 IST on the 13th is still the 12th in UTC; the date must be the IST one.
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP1002", "punched_at": ist(2026, 4, 13, 0, 30)})
    body = r.json()
    assert body["date"] == "2026-04-13"
    assert body == {**body, "status": "PRESENT", "punch_out": None, "work_hours": None,
                    "overtime_minutes": 0, "half_day": False, "history": []}
    assert "id" not in body and "_id" not in body


@pytest.mark.parametrize("payload, code", [
    ({"emp_code": "EMP9999"}, 404),
    ({"emp_code": "EMP1001", "punched_at": 1_783_312_500}, 422),            # seconds
    ({"emp_code": "EMP1001", "punched_at": 1_783_312_500_000.0}, 422),      # float
    ({"emp_code": "EMP1001", "punched_at": "1783312500000"}, 422),          # string
    ({"emp_code": "EMP1001", "punched_at": 5_000_000_000_000}, 422),        # out of range
    ({"emp_code": "EMP1001", "punched_at": True}, 422),
    ({"emp_code": "EMP1001", "status": "ABSENT"}, 422),
    ({"emp_code": "EMP1001", "status": "present"}, 422),
    ({"punched_at": 1_783_312_500_000}, 422),
])
def test_punch_in_errors(api, payload, code):
    assert api.post("/attendance/punch-in", json=payload).status_code == code


def test_concurrent_punch_ins_give_one_201(api):
    body = {"emp_code": "EMP1002", "punched_at": ist(2026, 5, 4, 9, 30)}
    codes = parallel([lambda: httpx.post(str(api.base_url) + "/attendance/punch-in", json=body)] * 20)
    assert sorted(codes) == [201] + [409] * 19


# --------------------------------------------------------------------------- punch-out
def punch(api, code, pin, pout=None):
    r = api.post("/attendance/punch-in", json={"emp_code": code, "punched_at": pin})
    assert r.status_code == 201, r.text
    if pout is None:
        return r.json()
    r = api.post("/attendance/punch-out", json={"emp_code": code, "punched_at": pout})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("day, pin, pout, hours, overtime, half", [
    (11, (9, 30), (18, 59, 59), 9.5, 0, False),           # 29:59 after shift end: no overtime
    (12, (9, 30), (19, 0, 0), 9.5, 30, False),            # exactly 30 minutes counts
    (13, (9, 30), (18, 37, 30), 9.13, 0, False),          # 9.125 h rounds half-up
    (14, (9, 30), (13, 59, 42), 4.5, 0, False),           # 4.495 -> 4.50: not a half day
    (15, (9, 30), (13, 59, 41), 4.49, 0, True),           # 4.4947 -> 4.49: half day
])
def test_punch_out_rules(api, day, pin, pout, hours, overtime, half):
    rec = punch(api, "EMP1001", ist(2026, 5, day, *pin), ist(2026, 5, day, *pout))
    assert (rec["work_hours"], rec["overtime_minutes"], rec["half_day"]) == (hours, overtime, half)


def test_overnight_shift(api):
    # Shift 22:00 - 06:00. In at 21:55 on the 18th, out at 06:40 on the 19th.
    rec = punch(api, "EMP1003", ist(2026, 5, 18, 21, 55), ist(2026, 5, 19, 6, 40))
    assert (rec["date"], rec["late_minutes"], rec["overtime_minutes"], rec["work_hours"]) == ("2026-05-18", 0, 40, 8.75)
    # A punch-in at 00:30 belongs to the shift that started at 22:00 the day before.
    rec = punch(api, "EMP1003", ist(2026, 5, 20, 0, 30))
    assert (rec["date"], rec["late_minutes"]) == ("2026-05-19", 150)
    # 06:00 is not earlier than shift_end, so it starts a shift on its own date.
    assert punch(api, "EMP1003", ist(2026, 5, 21, 6, 0))["date"] == "2026-05-21"


def test_punch_out_errors(api):
    post = lambda body: api.post("/attendance/punch-out", json=body)
    assert post({"emp_code": "EMP9999", "punched_at": ist(2026, 5, 25, 18, 0)}).status_code == 404
    assert post({"emp_code": "EMP1002", "punched_at": ist(2020, 1, 1, 18, 0)}).status_code == 404  # nothing before
    punch(api, "EMP1002", ist(2026, 5, 25, 9, 30))
    assert post({"emp_code": "EMP1002", "punched_at": ist(2026, 5, 25, 9, 30)}).status_code == 422  # == punch_in
    assert post({"emp_code": "EMP1002", "punched_at": ist(2026, 5, 26, 9, 31)}).status_code == 422  # > 24 h
    assert post({"emp_code": "EMP1002", "punched_at": 12}).status_code == 422
    assert post({"emp_code": "EMP1002", "punched_at": ist(2026, 5, 25, 18, 30)}).status_code == 200
    assert post({"emp_code": "EMP1002", "punched_at": ist(2026, 5, 25, 18, 45)}).status_code == 409


def test_concurrent_punch_outs_give_one_200(api):
    punch(api, "EMP1002", ist(2026, 5, 27, 9, 30))
    body = {"emp_code": "EMP1002", "punched_at": ist(2026, 5, 27, 18, 30)}
    codes = parallel([lambda: httpx.post(str(api.base_url) + "/attendance/punch-out", json=body)] * 20)
    assert sorted(codes) == [200] + [409] * 19


# --------------------------------------------------------------------------- list
def test_list_attendance(api, mongo):
    page = api.get("/attendance", params={"date_from": "2026-04-06", "date_to": "2026-04-13", "page_size": 100}).json()
    keys = [(r["date"], r["emp_code"]) for r in page["items"]]
    assert keys == sorted(sorted(keys, key=lambda k: k[1]), key=lambda k: k[0], reverse=True)  # date desc, emp asc
    assert page["total"] == len(page["items"]) == 6
    only = api.get("/attendance", params={"emp_code": "EMP1001", "date_to": "2026-04-08", "page_size": 2, "page": 2}).json()
    assert [r["date"] for r in only["items"]] == ["2026-04-06"] and only["total"] == 3
    assert api.get("/attendance", params={"status": "LEAVE"}).json()["total"] == 0
    for bad in ({"date_from": "2026-05-02", "date_to": "2026-05-01"}, {"status": "LATE"},
                {"date_from": "2026-13-01"}, {"page_size": 101}):
        assert api.get("/attendance", params=bad).status_code == 422


def test_legacy_record_defaults(api, mongo):
    mongo["attendance_test"].attendance_logs.insert_one({
        "emp_code": "EMP1002", "date": "2026-03-02", "status": "PRESENT",
        "punch_in": datetime(2026, 3, 2, 4, 2, tzinfo=timezone.utc), "punch_out": None,
        "work_hours": None,
    })
    rec = api.get("/attendance", params={"emp_code": "EMP1002", "date_to": "2026-03-02"}).json()["items"][0]
    assert (rec["history"], rec["half_day"], rec["late_minutes"], rec["overtime_minutes"]) == ([], False, 0, 0)


# --------------------------------------------------------------------------- regularize
def patch(api, code, day, **body):
    return api.patch(f"/attendance/{code}/{day}", json={"reason": "biometric glitch", "regularized_by": "hr.admin", **body})


def test_regularize_punch_in(api):
    punch(api, "EMP1001", ist(2026, 6, 1, 10, 5), ist(2026, 6, 1, 18, 30))
    r = patch(api, "EMP1001", "2026-06-01", punch_in=ist(2026, 6, 1, 9, 28), work_hours=1, late_minutes=99)
    assert r.status_code == 200, r.text
    rec = r.json()
    assert (rec["punch_in"], rec["late_minutes"], rec["work_hours"]) == (ist(2026, 6, 1, 9, 28), 0, 9.03)
    [entry] = rec["history"]
    assert entry["by"] == "hr.admin" and entry["reason"] == "biometric glitch" and isinstance(entry["at"], int)
    assert entry["changes"] == {
        "punch_in": {"from": ist(2026, 6, 1, 10, 5), "to": ist(2026, 6, 1, 9, 28)},
        "late_minutes": {"from": 35, "to": 0},
        "work_hours": {"from": 8.42, "to": 9.03},
    }
    # The next correction appends; the first entry is untouched.
    rec = patch(api, "EMP1001", "2026-06-01", status="WFH").json()
    assert len(rec["history"]) == 2 and rec["history"][1]["changes"] == {"status": {"from": "PRESENT", "to": "WFH"}}


def test_regularize_to_leave_and_back(api):
    punch(api, "EMP1001", ist(2026, 6, 2, 9, 50), ist(2026, 6, 2, 13, 0))
    rec = patch(api, "EMP1001", "2026-06-02", status="LEAVE").json()
    assert (rec["status"], rec["punch_in"], rec["punch_out"], rec["work_hours"], rec["late_minutes"], rec["half_day"]) == \
        ("LEAVE", None, None, None, 0, False)
    assert set(rec["history"][0]["changes"]) == {"status", "punch_in", "punch_out", "work_hours", "late_minutes", "half_day"}
    assert patch(api, "EMP1001", "2026-06-02", status="PRESENT").status_code == 422  # needs a punch_in
    rec = patch(api, "EMP1001", "2026-06-02", status="PRESENT", punch_in=ist(2026, 6, 2, 9, 30)).json()
    assert (rec["status"], rec["punch_out"], rec["work_hours"], len(rec["history"])) == ("PRESENT", None, None, 2)


@pytest.mark.parametrize("body", [
    {"status": "ABSENT", "punch_in": ist(2026, 6, 3, 9, 30)},     # ABSENT has no punch times
    {"punch_in": ist(2026, 6, 4, 9, 30)},                         # moves to another attendance date
    {"punch_out": ist(2026, 6, 3, 9, 0)},                         # before punch_in
    {"punch_out": ist(2026, 6, 4, 9, 31)},                        # more than 24 h
    {"punch_in": ist(2026, 6, 3, 9, 30)},                         # changes nothing
    {"status": "PRESENT"},                                        # changes nothing
    {},                                                           # changes nothing
    {"punch_in": 1_780_000_000},                                  # seconds
    {"status": "LATE"},
])
def test_regularize_rejects(api, body):
    if api.get("/attendance", params={"emp_code": "EMP1002", "date_from": "2026-06-03", "date_to": "2026-06-03"}).json()["total"] == 0:
        punch(api, "EMP1002", ist(2026, 6, 3, 9, 30))
    assert patch(api, "EMP1002", "2026-06-03", **body).status_code == 422


def test_regularize_reason_and_404s(api):
    assert api.patch("/attendance/EMP1002/2026-06-03", json={"reason": "abc", "regularized_by": "x", "status": "WFH"}).status_code == 422
    assert patch(api, "EMP9999", "2026-06-03", status="WFH").status_code == 404
    assert patch(api, "EMP1002", "2026-01-01", status="WFH").status_code == 404
    assert patch(api, "EMP1002", "2026-06-31", status="WFH").status_code == 422


def test_concurrent_regularizations_keep_every_entry(api):
    punch(api, "EMP1002", ist(2026, 6, 8, 9, 30), ist(2026, 6, 8, 18, 30))
    url = str(api.base_url) + "/attendance/EMP1002/2026-06-08"
    bodies = [{"punch_out": ist(2026, 6, 8, 18, m), "reason": "parallel", "regularized_by": "t"} for m in range(31, 51)]
    codes = parallel([lambda b=b: httpx.patch(url, json=b) for b in bodies])
    assert set(codes) <= {200, 409} and 200 in codes
    rec = api.get("/attendance", params={"emp_code": "EMP1002", "date_from": "2026-06-08", "date_to": "2026-06-08"}).json()["items"][0]
    assert len(rec["history"]) == codes.count(200)
