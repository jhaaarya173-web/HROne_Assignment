"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (real environment variables win over a local .env)

openapi.yaml is the contract (business rules R1-R10); DATA_MODEL.md describes what is stored.
Conventions used throughout this file:
  * Instants are aware UTC datetimes in Python, BSON dates in MongoDB and epoch milliseconds in
    the API. They are converted only at the boundary (from_ms / to_ms).
  * Calendar dates ("YYYY-MM-DD", IST) and shift times ("HH:MM", IST) stay strings, as stored.
  * Analytics are aggregation pipelines. Each pipeline is built by one function that both the
    endpoint and /admin/explain call, so the explained plan is the plan the endpoint runs.
"""
import json
import logging
import os
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from pydantic import AfterValidator, BaseModel, Field, StrictInt, StringConstraints, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError

load_dotenv()  # only fills in variables that are not already set, so real env vars win

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,  # BSON dates come back as aware UTC datetimes, never naive ones
    serverSelectionTimeoutMS=5000,  # fail fast so /health can answer 503
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
logger = logging.getLogger("attendance")

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

GRACE = timedelta(minutes=10)  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_BELOW_HOURS = 4.5  # R5
MAX_SHIFT_LENGTH = timedelta(hours=24)
MAX_TREND_DAYS = 92
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]  # R6
ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


# --------------------------------------------------------------------------- #
# Indexes and startup
# --------------------------------------------------------------------------- #
INDEXES = {
    "employees": [
        # Identity. Unique, so concurrent creates of one emp_code give one 201 and the rest 409.
        ([("emp_code", ASCENDING)], {"unique": True}),
        # GET /employees?department= (sorted by emp_code); trend's $match on department.
        ([("department", ASCENDING), ("emp_code", ASCENDING)], {}),
        # Department summary: employees with joined_on <= end of month (R9).
        ([("joined_on", ASCENDING)], {}),
    ],
    "attendance_logs": [
        # Natural key. Unique, so concurrent punch-ins for one day give one 201 and the rest 409.
        # Also serves PATCH, per-employee lists, the monthly summary and every $lookup by emp_code.
        ([("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True}),
        # Punch-out: the employee's latest punch_in at or before punched_at.
        ([("emp_code", ASCENDING), ("punch_in", DESCENDING)], {}),
        # GET /attendance without emp_code (matches its sort); month range of the leaderboard.
        ([("date", DESCENDING), ("emp_code", ASCENDING)], {}),
        # GET /attendance?status= : equality on status, then already in the list's sort order.
        ([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {}),
    ],
}


def ensure_indexes() -> None:
    for collection, indexes in INDEXES.items():
        for keys, options in indexes:
            try:
                db[collection].create_index(keys, **options)  # a no-op when it already exists
            except OperationFailure as exc:  # e.g. duplicates in seeded data: keep serving
                logger.error("could not create index %s on %s: %s", keys, collection, exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # MongoDB down: start anyway, /health answers 503
        logger.error("MongoDB unavailable at startup: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Time helpers (R1)
# --------------------------------------------------------------------------- #
def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def to_ms(value):
    """Datetime -> epoch milliseconds. Anything else (None, numbers, strings) passes through."""
    if isinstance(value, datetime):
        return (value - EPOCH) // timedelta(milliseconds=1)
    return value


def parse_hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def attendance_date(punch_in: datetime, emp: dict) -> str:
    """R1: the IST calendar date of the punch-in. On an overnight shift a punch-in earlier
    than shift_end belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    shift_start, shift_end = parse_hhmm(emp["shift_start"]), parse_hhmm(emp["shift_end"])
    if shift_end <= shift_start and local.time() < shift_end:
        return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()


def shift_window(day: str, emp: dict) -> tuple[datetime, datetime]:
    """Start and end instants of the shift that belongs to attendance date `day`."""
    d = date.fromisoformat(day)
    start = datetime.combine(d, parse_hhmm(emp["shift_start"]), IST)
    end = datetime.combine(d, parse_hhmm(emp["shift_end"]), IST)
    if end <= start:  # overnight shift: it ends on the next calendar day
        end += timedelta(days=1)
    return start, end


# --------------------------------------------------------------------------- #
# Business-rule helpers (R2 - R5)
# --------------------------------------------------------------------------- #
def round_half_up(value: Decimal, places: int) -> float:
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def compute_late_minutes(punch_in: datetime, shift_start: datetime) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then whole minutes since shift start."""
    elapsed = punch_in - shift_start
    return elapsed // timedelta(minutes=1) if elapsed > GRACE else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: hours to 2 decimals, half-up. Decimal keeps 9.125 from turning into 9.12."""
    seconds = (punch_out - punch_in) // timedelta(seconds=1)
    return round_half_up(Decimal(seconds) / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: datetime) -> int:
    """R3: whole minutes after shift end, counted only from 30 minutes."""
    minutes = (punch_out - shift_end) // timedelta(minutes=1)
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def derived_fields(day: str, punch_in: Optional[datetime], punch_out: Optional[datetime], emp: dict) -> dict:
    """The four computed fields of a record (R2 - R5). Instants are truncated to whole seconds first (R1)."""
    fields = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if punch_in is None:  # ABSENT or LEAVE
        return fields
    punch_in = punch_in.replace(microsecond=0)
    shift_start, shift_end = shift_window(day, emp)
    fields["late_minutes"] = compute_late_minutes(punch_in, shift_start)
    if punch_out is not None:
        punch_out = punch_out.replace(microsecond=0)
        work_hours = compute_work_hours(punch_in, punch_out)
        fields["work_hours"] = work_hours
        fields["overtime_minutes"] = compute_overtime(punch_out, shift_end)
        fields["half_day"] = work_hours < HALF_DAY_BELOW_HOURS
    return fields


# --------------------------------------------------------------------------- #
# Request models and validation
# --------------------------------------------------------------------------- #
def _real_date(value: str) -> str:
    date.fromisoformat(value)  # rejects 2026-02-30 and year 0000
    return value


def _real_month(value: str) -> str:
    date.fromisoformat(value + "-01")
    return value


DateStr = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"), AfterValidator(_real_date)]
MonthStr = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"), AfterValidator(_real_month)]
ShiftTime = Annotated[str, StringConstraints(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
# StrictInt: floats and strings are 422 instead of being coerced. The range rejects seconds.
EpochMillis = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Limit = Annotated[int, Query(ge=1, le=50)]
Explainable = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


class EmployeeCreate(BaseModel):
    emp_code: Annotated[str, StringConstraints(pattern=r"^EMP[0-9]{4,6}$")]
    name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    email: Annotated[str, StringConstraints(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, StringConstraints(min_length=1, max_length=50)]
    shift_start: ShiftTime = "09:30"
    shift_end: ShiftTime = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def shift_has_length(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    # Derived fields are not declared, so a client that sends them is ignored (extra keys are dropped).
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: Annotated[str, StringConstraints(min_length=5, max_length=200)]
    regularized_by: Annotated[str, StringConstraints(min_length=1, max_length=50)]


def reject(message: str, *loc: str):
    """422 with FastAPI's default validation body, for rules a schema cannot express."""
    raise RequestValidationError([{"type": "value_error", "loc": loc or ("body",), "msg": message, "input": None}])


def require(**params) -> None:
    missing = [{"type": "missing", "loc": ("query", name), "msg": "Field required", "input": None}
               for name, value in params.items() if value is None]
    if missing:
        raise RequestValidationError(missing)


def month_range(month: str) -> tuple[str, str]:
    year, mon = map(int, month.split("-"))
    return f"{month}-01", f"{month}-{monthrange(year, mon)[1]:02d}"


def working_days(start: str, end: str) -> int:
    """R7: Mon-Fri from start to end inclusive; 0 when start is after end."""
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    return sum(1 for i in range((last - first).days + 1) if (first + timedelta(days=i)).weekday() < 5)


def check_trend_range(start: str, end: str) -> None:
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    if days < 1:
        reject("'to' must not be before 'from'", "query", "to")
    if days > MAX_TREND_DAYS:
        reject(f"the range may cover at most {MAX_TREND_DAYS} days", "query", "to")


# --------------------------------------------------------------------------- #
# Response shapes (no _id, instants as epoch ms, defaults for legacy records)
# --------------------------------------------------------------------------- #
EMPLOYEE_FIELDS = ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")


def employee_out(doc: dict) -> dict:
    return {**{f: doc.get(f) for f in EMPLOYEE_FIELDS}, "created_at": to_ms(doc.get("created_at"))}


def history_out(entry: dict) -> dict:
    return {
        "at": to_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": {field: {"from": to_ms(c.get("from")), "to": to_ms(c.get("to"))}
                    for field, c in (entry.get("changes") or {}).items()},
    }


def stored_values(doc: dict) -> dict:
    """The fields a correction can change, with the documented defaults for missing ones."""
    return {
        "status": doc["status"],
        "punch_in": doc.get("punch_in"),
        "punch_out": doc.get("punch_out"),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": doc.get("half_day") or False,
    }


def record_out(doc: dict) -> dict:
    values = stored_values(doc)
    values["punch_in"], values["punch_out"] = to_ms(values["punch_in"]), to_ms(values["punch_out"])
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        **values,
        "history": [history_out(h) for h in doc.get("history") or []],
    }


def find_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# Query and pipeline builders (shared by the endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
IS_PRESENT = {"$in": ["$status", PRESENCE]}
IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]}
DAY_CREDIT = {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}  # R5: a half day counts 0.5
LATE_MINUTES = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$gt": [LATE_MINUTES, 0]}
HAS_WORK_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}
PRESENT_DAYS = {"$sum": {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, DAY_CREDIT, 0]}}  # R5 - R7


def count_if(condition) -> dict:
    return {"$sum": {"$cond": [condition, 1, 0]}}


def half_up_expr(expr, places: int) -> dict:
    """Round a non-negative number half-up inside MongoDB (R8). $round rounds half to even, so
    it is not used. Done in Decimal128 so that 0.125 really is 0.125; null stays null."""
    scale = 10**places
    scaled = {"$multiply": [{"$toDecimal": expr}, scale]}
    return {"$divide": [{"$floor": {"$add": [scaled, Decimal128("0.5")]}}, scale]}


def attendance_list_query(emp_code, date_from, date_to, status) -> dict:
    if date_from and date_to and date_from > date_to:
        reject("date_from must not be after date_to", "query", "date_from")
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    return query


def employee_monthly_pipeline(emp_code: str, first: str, last: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_days": PRESENT_DAYS,
            "leave_days": count_if({"$eq": ["$status", "LEAVE"]}),
            "late_count": count_if(IS_LATE),
            "total_late_minutes": {"$sum": LATE_MINUTES},  # weekend records count here too (R7)
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(first: str, last: str, department: Optional[str]) -> list:
    """Starts from employees, so people with no logs are still in the headcount (R9).
    Each employee's logs are summarised inside the $lookup; departments then add up those
    sums, which keeps avg_work_hours an average over records, not an average of averages."""
    match = {"joined_on": {"$lte": last}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$group": {
                    "_id": None,
                    "present_days": PRESENT_DAYS,
                    "work_hours_sum": {"$sum": {"$cond": [HAS_WORK_HOURS, {"$toDecimal": "$work_hours"}, 0]}},
                    "work_hours_records": count_if(HAS_WORK_HOURS),
                    "late_count": count_if(IS_LATE),
                    "total_late_minutes": {"$sum": LATE_MINUTES},
                    "leave_count": count_if({"$eq": ["$status", "LEAVE"]}),
                    "on_duty_count": count_if({"$eq": ["$status", "ON_DUTY"]}),
                }},
            ],
            "as": "stats",
        }},
        {"$unwind": {"path": "$stats", "preserveNullAndEmptyArrays": True}},  # keep zero-log employees
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            # $sum skips missing values, so a zero-log employee adds 0 to every total
            "present_days": {"$sum": "$stats.present_days"},
            "work_hours_sum": {"$sum": "$stats.work_hours_sum"},
            "work_hours_records": {"$sum": "$stats.work_hours_records"},
            "late_count": {"$sum": "$stats.late_count"},
            "total_late_minutes": {"$sum": "$stats.total_late_minutes"},
            "leave_count": {"$sum": "$stats.leave_count"},
            "on_duty_count": {"$sum": "$stats.on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
        {"$replaceWith": {
            "department": "$_id",
            "headcount": "$headcount",
            "present_days": "$present_days",
            "avg_work_hours": {"$cond": [
                {"$gt": ["$work_hours_records", 0]},
                {"$toDouble": half_up_expr({"$divide": ["$work_hours_sum", "$work_hours_records"]}, 2)},
                None,
            ]},
            "late_count": "$late_count",
            "total_late_minutes": "$total_late_minutes",
            "leave_count": "$leave_count",
            "on_duty_count": "$on_duty_count",
        }},
    ]


def late_leaderboard_pipeline(first: str, last: str, limit: int, department: Optional[str]) -> list:
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},  # drops logs whose employee does not exist
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})  # rank inside the department
    return pipeline + [
        # $rank is competition ranking: ties share a rank and the next rank is skipped (1, 2, 2, 4).
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies after ranking, so ties at the cutoff stay
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$replaceWith": {
            "rank": "$rank",
            "emp_code": "$_id",
            "name": "$employee.name",
            "department": "$employee.department",
            "total_late_minutes": "$total_late_minutes",
            "late_count": "$late_count",
        }},
    ]


def department_trend_pipeline(department: str, start: str, end: str) -> list:
    """One row per calendar day from start to end.

    Every employee of the department contributes one "joined" event (their joined_on, clamped
    into the range) plus their logs in the range. Events are grouped per day, $densify adds the
    days that have no events, a running sum of "joined" gives the headcount (R9), and a 7-row
    window gives the moving average. The range is never left, so the first rows use a shorter window.
    """
    lower = datetime.combine(date.fromisoformat(start), time(), UTC)
    upper = datetime.combine(date.fromisoformat(end), time(), UTC) + timedelta(days=1)  # exclusive
    join_event = {
        "date": {"$min": [{"$max": ["$joined_on", start]}, end]},
        "joined": {"$cond": [{"$lte": ["$joined_on", end]}, 1, 0]},
        "present": 0,
        "late": 0,
    }
    return [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": start, "$lte": end}}},
                {"$project": {
                    "_id": 0,
                    "date": 1,
                    "joined": {"$literal": 0},
                    "present": {"$cond": [IS_PRESENT, DAY_CREDIT, 0]},
                    "late": {"$cond": [IS_LATE, 1, 0]},
                }},
            ],
            "as": "logs",
        }},
        {"$project": {"events": {"$concatArrays": [[join_event], "$logs"]}}},
        {"$unwind": "$events"},
        {"$group": {
            "_id": "$events.date",
            "joined": {"$sum": "$events.joined"},
            "present_count": {"$sum": "$events.present"},
            "late_count": {"$sum": "$events.late"},
        }},
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id", "timezone": "UTC"}}}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lower, upper]}}},
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {"headcount": {"$sum": "$joined", "window": {"documents": ["unbounded", "current"]}}},
        }},
        {"$set": {
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
            "present_count": {"$ifNull": ["$present_count", 0]},  # days added by $densify
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            half_up_expr({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4),
            None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            # $avg ignores nulls and returns null when the window has no rate at all
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$replaceWith": {
            "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "is_working_day": "$is_working_day",
            "headcount": "$headcount",
            "present_count": "$present_count",
            "late_count": "$late_count",
            "attendance_rate": {"$toDouble": "$attendance_rate"},
            "moving_avg_7d": {"$toDouble": half_up_expr("$moving_avg_7d", 4)},
        }},
    ]


# --------------------------------------------------------------------------- #
# System and employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = {**body.model_dump(), "created_at": utc_now()}
    try:
        db.employees.insert_one(doc)  # the unique index decides concurrent duplicates
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    query = {} if department is None else {"department": department}
    cursor = (db.employees.find(query)
              .sort("emp_code", ASCENDING)
              .skip((page - 1) * page_size)
              .limit(page_size))
    items = [employee_out(doc) for doc in cursor]
    return {"items": items, "total": db.employees.count_documents(query), "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = find_employee(body.emp_code)
    punched_at = from_ms(body.punched_at) if body.punched_at is not None else utc_now()
    day = attendance_date(punched_at, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        **derived_fields(day, punched_at, None, emp),
        "history": [],
    }
    try:
        # No check-then-insert: the unique (emp_code, date) index lets exactly one insert win.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = find_employee(body.emp_code)
    punched_at = from_ms(body.punched_at) if body.punched_at is not None else utc_now()
    # Looked up by punch_in, not by today's date, so a punch-out after midnight finds its shift.
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if not record["punch_in"] < punched_at <= record["punch_in"] + MAX_SHIFT_LENGTH:
        reject("punched_at must be after punch_in and at most 24 hours later", "body", "punched_at")

    update = {"punch_out": punched_at, **derived_fields(record["date"], record["punch_in"], punched_at, emp)}
    # Conditional update: of two simultaneous punch-outs only one still sees punch_out == null.
    # punch_in is in the filter too, so a correction made in between is not overwritten.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None, "punch_in": record["punch_in"]},
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    query = attendance_list_query(emp_code, date_from, date_to, status)
    cursor = (db.attendance_logs.find(query)
              .sort(ATTENDANCE_SORT)
              .skip((page - 1) * page_size)
              .limit(page_size))
    items = [record_out(doc) for doc in cursor]
    return {"items": items, "total": db.attendance_logs.count_documents(query), "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, day: Annotated[DateStr, Path(alias="date")], body: RegularizeIn):
    emp = find_employee(emp_code)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if record is None:
        raise HTTPException(404, "no attendance record for this date")

    status = body.status or record["status"]
    if status in PRESENCE:
        punch_in = from_ms(body.punch_in) if body.punch_in is not None else record.get("punch_in")
        punch_out = from_ms(body.punch_out) if body.punch_out is not None else record.get("punch_out")
        if punch_in is None:
            reject("a presence status requires a punch_in")
        if body.punch_in is not None and attendance_date(punch_in, emp) != day:
            reject("punch_in must stay on the record's attendance date", "body", "punch_in")
        if punch_out is not None and not punch_in < punch_out <= punch_in + MAX_SHIFT_LENGTH:
            reject("punch_out must be after punch_in and at most 24 hours later", "body", "punch_out")
    else:
        if body.punch_in is not None or body.punch_out is not None:
            reject("ABSENT and LEAVE records have no punch times")
        punch_in = punch_out = None

    old = stored_values(record)
    new = {"status": status, "punch_in": punch_in, "punch_out": punch_out,
           **derived_fields(day, punch_in, punch_out, emp)}
    if all(new[f] == old[f] for f in ("status", "punch_in", "punch_out")):
        reject("the request changes nothing")
    changes = {f: {"from": old[f], "to": new[f]} for f in new if new[f] != old[f]}
    entry = {"at": utc_now(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: only write if nobody changed the record since we read it.
    # A concurrent PATCH grows history and a concurrent punch-out sets punch_out, so the
    # loser matches nothing and gets 409 instead of silently replacing the winner's entry.
    seen_history = len(record.get("history") or [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": record.get("punch_out"),
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, seen_history]},
        },
        {"$set": {f: c["to"] for f, c in changes.items()}, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "the record was changed by another request; retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthStr):
    emp = find_employee(emp_code)
    first, last = month_range(month)
    days = working_days(max(first, emp["joined_on"]), last)  # R7: mid-month joiners count from joined_on
    stats = next(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, first, last)), {})
    present_days = stats.get("present_days", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": days,
        "present_days": present_days,
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
        "attendance_pct": round_half_up(Decimal(str(present_days)) * 100 / days, 2) if days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthStr, department: Optional[str] = None):
    first, last = month_range(month)
    items = list(db.employees.aggregate(department_summary_pipeline(first, last, department)))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthStr, limit: Limit = 10, department: Optional[str] = None):
    first, last = month_range(month)
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(first, last, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    start: Annotated[DateStr, Query(alias="from")],
    end: Annotated[DateStr, Query(alias="to")],
):
    check_trend_range(start, end)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    items = list(db.employees.aggregate(department_trend_pipeline(department, start, end)))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Explainable,
    emp_code: Optional[str] = None,
    month: Optional[MonthStr] = None,
    department: Optional[str] = None,
    limit: Limit = 10,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    start: Annotated[Optional[DateStr], Query(alias="from")] = None,
    end: Annotated[Optional[DateStr], Query(alias="to")] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": attendance_list_query(emp_code, date_from, date_to, status),
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            require(emp_code=emp_code, month=month)
            collection, pipeline = "attendance_logs", employee_monthly_pipeline(emp_code, *month_range(month))
        elif endpoint == "department_summary":
            require(month=month)
            collection, pipeline = "employees", department_summary_pipeline(*month_range(month), department)
        elif endpoint == "late_leaderboard":
            require(month=month)
            collection, pipeline = "attendance_logs", late_leaderboard_pipeline(*month_range(month), limit, department)
        else:
            require(department=department, **{"from": start, "to": end})
            check_trend_range(start, end)
            collection, pipeline = "employees", department_trend_pipeline(department, start, end)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    result = db.command({"explain": command, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(result))}
