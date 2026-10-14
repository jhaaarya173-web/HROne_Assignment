"""Analytics against tests/oracle.py on the ~100k-record dataset, plus index usage and speed."""
import json
import time

import pytest

from tests import oracle

MONTHS = ["2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-12"]
DEPARTMENTS = [None, "Engineering", "engineering", "Sales", "Support", "Legal", "Facilities", "Nope"]


def get(client, path, **params):
    r = client.get(path, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_dataset_size(big):
    _, employees, logs = big
    assert len(logs) >= 95_000 and len(employees) > 800


@pytest.mark.parametrize("month", MONTHS)
def test_employee_monthly(big, month):
    client, employees, logs = big
    for emp in employees[::13] + employees[-2:]:
        expected = oracle.employee_monthly(employees, logs, emp["emp_code"], month)
        assert get(client, f"/analytics/employees/{emp['emp_code']}/monthly", month=month) == expected


@pytest.mark.parametrize("month", MONTHS)
@pytest.mark.parametrize("department", DEPARTMENTS)
def test_department_summary(big, month, department):
    client, employees, logs = big
    params = {"month": month} if department is None else {"month": month, "department": department}
    assert get(client, "/analytics/departments/summary", **params) == oracle.department_summary(employees, logs, month, department)


@pytest.mark.parametrize("month", ["2026-04", "2026-07", "2026-09", "2026-11"])
@pytest.mark.parametrize("department", [None, "Sales", "Support", "engineering"])
@pytest.mark.parametrize("limit", [1, 2, 3, 4, 7, 8, 10, 50])
def test_late_leaderboard(big, month, department, limit):
    client, employees, logs = big
    params = {"month": month, "limit": limit, **({} if department is None else {"department": department})}
    assert get(client, "/analytics/leaderboard/late", **params) == oracle.late_leaderboard(employees, logs, month, limit, department)


def test_leaderboard_ties_at_the_cutoff(big):
    client, _, _ = big
    items = get(client, "/analytics/leaderboard/late", month="2026-07", limit=2, department="Sales")["items"]
    assert [i["rank"] for i in items] == [1, 2, 2]
    items = get(client, "/analytics/leaderboard/late", month="2026-07", limit=4)["items"]
    assert [i["rank"] for i in items] == [1, 2, 2, 4, 4, 4, 4, 4]  # two Support employees tie with Sales at 4500


@pytest.mark.parametrize("department", ["Engineering", "engineering", "Sales", "Support", "Legal", "Facilities"])
@pytest.mark.parametrize("start, end", [
    ("2026-07-01", "2026-07-31"), ("2026-03-25", "2026-04-12"), ("2026-07-01", "2026-09-30"),
    ("2026-08-10", "2026-08-10"), ("2026-09-20", "2026-10-05"), ("2026-08-14", "2026-08-25"),
])
def test_department_trend(big, department, start, end):
    client, employees, logs = big
    got = get(client, f"/analytics/departments/{department}/trend", **{"from": start, "to": end})
    assert got == oracle.department_trend(employees, logs, department, start, end)


def test_trend_errors(big):
    client, _, _ = big
    trend = lambda dept, f, t: client.get(f"/analytics/departments/{dept}/trend", params={"from": f, "to": t}).status_code
    assert trend("Nope", "2026-07-01", "2026-07-31") == 404
    assert trend("Sales", "2026-07-31", "2026-07-01") == 422
    assert trend("Sales", "2026-07-01", "2026-10-01") == 422  # 93 days
    assert trend("Sales", "2026-07-01", "2026-09-30") == 200  # 92 days
    assert client.get("/analytics/departments/Sales/trend", params={"from": "2026-07-01"}).status_code == 422


def test_analytics_validation(big):
    client, _, _ = big
    for path, params in [
        ("/analytics/employees/EMP0001/monthly", {"month": "2026-13"}),
        ("/analytics/employees/EMP0001/monthly", {}),
        ("/analytics/departments/summary", {"month": "2026-7"}),
        ("/analytics/leaderboard/late", {"month": "2026-07", "limit": 0}),
        ("/analytics/leaderboard/late", {"month": "2026-07", "limit": 51}),
    ]:
        assert client.get(path, params=params).status_code == 422
    assert client.get("/analytics/employees/EMP99999/monthly", params={"month": "2026-07"}).status_code == 404


# --------------------------------------------------------------------------- indexes and speed
EXPLAIN_CASES = [
    ("attendance_list", {}),
    ("attendance_list", {"emp_code": "EMP0042"}),
    ("attendance_list", {"date_from": "2026-07-01", "date_to": "2026-07-31", "page": 3}),
    ("attendance_list", {"status": "ON_DUTY", "page_size": 100}),
    ("attendance_list", {"emp_code": "EMP0042", "status": "LEAVE", "date_from": "2026-05-01"}),
    ("employee_monthly", {"emp_code": "EMP0042", "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}),
    ("department_summary", {"month": "2026-07", "department": "Sales"}),
    ("late_leaderboard", {"month": "2026-07"}),
    ("late_leaderboard", {"month": "2026-07", "department": "Sales", "limit": 3}),
    ("department_trend", {"department": "Sales", "from": "2026-07-01", "to": "2026-09-30"}),
]


@pytest.mark.parametrize("endpoint, params", EXPLAIN_CASES)
def test_explain_uses_indexes(big, endpoint, params):
    client, _, _ = big
    body = get(client, f"/admin/explain/{endpoint}", **params)
    assert body["endpoint"] == endpoint and body["collection"] in ("employees", "attendance_logs")
    plan = json.dumps(body["explain"])
    assert "IXSCAN" in plan and "COLLSCAN" not in plan
    assert '"collectionScans": 0' in plan or "$lookup" not in plan


def test_explain_requires_parameters(big):
    client, _, _ = big
    assert client.get("/admin/explain/employee_monthly", params={"month": "2026-07"}).status_code == 422
    assert client.get("/admin/explain/department_trend", params={"department": "Sales"}).status_code == 422
    assert client.get("/admin/explain/nope").status_code == 422


@pytest.mark.parametrize("path, params", [
    ("/attendance", {}),
    ("/attendance", {"status": "ON_DUTY", "page": 40}),
    ("/attendance", {"date_from": "2026-06-01", "date_to": "2026-06-30"}),
    ("/analytics/employees/EMP0042/monthly", {"month": "2026-07"}),
    ("/analytics/departments/summary", {"month": "2026-07"}),
    ("/analytics/leaderboard/late", {"month": "2026-07"}),
    ("/analytics/departments/Engineering/trend", {"from": "2026-07-01", "to": "2026-09-30"}),
])
def test_fast_at_100k(big, path, params):
    client, _, _ = big
    get(client, path, **params)  # warm up
    started = time.perf_counter()
    get(client, path, **params)
    assert time.perf_counter() - started < 0.5
