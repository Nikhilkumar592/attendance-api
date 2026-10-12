"""
Employee Attendance & Analytics API (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (real environment variables win over a local .env)

Layout of this file
  1. configuration + indexes
  2. pure helpers (time handling, rules R1-R5, rounding, serialization)
  3. request models
  4. endpoints: health, employees, attendance, analytics, admin/explain
"""
import calendar
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False: variables already in the environment take priority

log = logging.getLogger("attendance")

# tz_aware=True makes PyMongo return aware UTC datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
NON_PRESENT_STATUSES = ["ABSENT", "LEAVE"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_HOURS = Decimal("4.50")  # R5
MAX_SHIFT = timedelta(hours=24)


# --------------------------------------------------------------------------- #
# Indexes (created at startup, idempotent)
# --------------------------------------------------------------------------- #
_indexes_ready = False


def ensure_indexes() -> None:
    """create_index is a no-op when an identical index already exists."""
    global _indexes_ready
    if _indexes_ready:
        return
    e, a = db.employees, db.attendance_logs
    # uniqueness of the business key; also the lookup target of every $lookup
    # Use MongoDB's default name (emp_code_1) so an existing equivalent index is reused.
    e.create_index([("emp_code", ASCENDING)], unique=True)
    # department filter + sort by emp_code (GET /employees, trend/summary lookups)
    e.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
    # headcount (R9): joined_on <= end of period
    e.create_index([("joined_on", ASCENDING)], name="joined_on")
    # one record per (emp_code, date): the race guard for punch-in, PATCH addressing,
    # monthly/summary/trend range scans per employee
    a.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    # global listing order (date desc, emp_code asc) + month range scans (leaderboard)
    a.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    # status filter without emp_code, already in listing order
    a.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp"
    )
    # punch-out: latest record of an employee with punch_in <= t
    a.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in")
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # keep serving /health (503) instead of crashing
        log.error("index creation failed at startup: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError):
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# Helpers: validation
# --------------------------------------------------------------------------- #
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


def invalid(loc: tuple, msg: str) -> RequestValidationError:
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


def parse_date(value: str, loc: tuple) -> Date:
    if not DATE_RE.match(value):
        raise invalid(loc, "date must be YYYY-MM-DD")
    try:
        d = Date.fromisoformat(value)
    except ValueError:
        raise invalid(loc, "not a valid calendar date")
    return d


def parse_month(value: str, loc: tuple = ("query", "month")) -> tuple[str, str, int]:
    """-> (first day 'YYYY-MM-01', first day of next month, number of days)."""
    if not MONTH_RE.match(value) or value.startswith("0000"):
        raise invalid(loc, "month must be YYYY-MM")
    y, m = int(value[:4]), int(value[5:])
    nxt_y, nxt_m = (y + 1, 1) if m == 12 else (y, m + 1)
    if nxt_y > 9999:
        raise invalid(loc, "month out of range")
    return f"{y:04d}-{m:02d}-01", f"{nxt_y:04d}-{nxt_m:02d}-01", calendar.monthrange(y, m)[1]


# --------------------------------------------------------------------------- #
# Helpers: instants, rounding, serialization
# --------------------------------------------------------------------------- #
def aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    return (aware(dt) - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    return EPOCH + timedelta(milliseconds=ms)


def trunc_seconds(dt: datetime) -> datetime:
    """R1: whole seconds before any calculation; the truncated value is what is stored."""
    return aware(dt).replace(microsecond=0)


def now_seconds() -> datetime:
    return trunc_seconds(datetime.now(UTC))


def round_half_up(value: Any, places: int) -> float:
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def _conv(v: Any) -> Any:
    return to_ms(v) if isinstance(v, datetime) else v


def serialize_history(history: Optional[list]) -> list:
    out = []
    for h in history or []:
        changes = {
            k: {"from": _conv(v.get("from")), "to": _conv(v.get("to"))}
            for k, v in (h.get("changes") or {}).items()
        }
        out.append({"at": to_ms(h.get("at")), "by": h.get("by"), "reason": h.get("reason"), "changes": changes})
    return out


def serialize_attendance(d: dict) -> dict:
    """Stored document -> API record. Missing legacy fields get their documented defaults."""
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": to_ms(d.get("punch_in")),
        "punch_out": to_ms(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": d.get("late_minutes") or 0,
        "overtime_minutes": d.get("overtime_minutes") or 0,
        "half_day": bool(d.get("half_day", False)),
        "history": serialize_history(d.get("history")),
    }


def serialize_employee(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "name": d["name"],
        "email": d["email"],
        "department": d["department"],
        "shift_start": d["shift_start"],
        "shift_end": d["shift_end"],
        "joined_on": d["joined_on"],
        "created_at": to_ms(d.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Helpers: shifts and rules R1-R5
# --------------------------------------------------------------------------- #
def hm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hm(shift_end) <= hm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; early-morning punches of an overnight
    shift belong to the shift that started the previous day."""
    ist = aware(punch_in).astimezone(IST)
    d = ist.date()
    if is_overnight(shift_start, shift_end) and (ist.hour, ist.minute) < hm(shift_end):
        d -= timedelta(days=1)
    return d.isoformat()


def shift_start_at(date_str: str, shift_start: str) -> datetime:
    return datetime.combine(Date.fromisoformat(date_str), time(*hm(shift_start)), tzinfo=IST)


def shift_end_at(date_str: str, shift_start: str, shift_end: str) -> datetime:
    end = datetime.combine(Date.fromisoformat(date_str), time(*hm(shift_end)), tzinfo=IST)
    return end + timedelta(days=1) if is_overnight(shift_start, shift_end) else end


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor minutes since start."""
    seconds = int((trunc_seconds(punch_in) - shift_start_at(date_str, shift_start)).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: exact seconds / 3600, 2 decimals, half-up (Decimal avoids float and banker's rounding)."""
    seconds = int((trunc_seconds(punch_out) - trunc_seconds(punch_in)).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: floor minutes after shift end, counted only if >= 30."""
    seconds = int((trunc_seconds(punch_out) - shift_end_at(date_str, shift_start, shift_end)).total_seconds())
    minutes = seconds // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def derive_fields(emp: dict, date_str: str, status: str, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """The four derived fields for a record, per the table in DATA_MODEL.md section 3."""
    if status in NON_PRESENT_STATUSES or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, date_str, emp["shift_start"])
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, date_str, emp["shift_start"], emp["shift_end"]),
        "half_day": Decimal(str(hours)) < HALF_DAY_HOURS,  # R5 on the rounded value
    }


def working_days(month_start: str, days_in_month: int, joined_on: str) -> int:
    """R7: Mon-Fri in the month, counted from joined_on for mid-month joiners."""
    first = Date.fromisoformat(month_start)
    last = first.replace(day=days_in_month)
    start = max(first, Date.fromisoformat(joined_on))
    return sum(1 for i in range((last - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
# Epoch milliseconds: strict int (no floats, strings or bools); seconds-looking values are too small.
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        Date.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        ensure_indexes()  # no-op once done; covers "Mongo came up after the app"
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = trunc_seconds(datetime.now(UTC))
    try:
        db.employees.insert_one(doc)  # unique index decides races: one 201, the rest 409
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict = {"department": department} if department else {}
    total = db.employees.count_documents(q)
    cur = db.employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_employee(d) for d in cur], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = trunc_seconds(from_ms(body.punched_at)) if body.punched_at is not None else now_seconds()
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive_fields(emp, d, body.status, ts, None),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = trunc_seconds(from_ms(body.punched_at)) if body.punched_at is not None else now_seconds()
    # latest record that started at or before the punch-out; works across midnight
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = aware(rec["punch_in"])
    if ts <= p_in or ts > p_in + MAX_SHIFT:
        raise invalid(("body", "punched_at"), "punched_at must be after punch_in and within 24 hours of it")
    fields = derive_fields(emp, rec["date"], rec["status"], p_in, ts)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},  # atomic: only one concurrent punch-out can match
        {"$set": {
            "punch_out": ts,
            "work_hours": fields["work_hours"],
            "overtime_minutes": fields["overtime_minutes"],
            "half_day": fields["half_day"],
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_attendance(updated)


def attendance_query(emp_code, date_from, date_to, status) -> dict:
    """Shared by GET /attendance and its explain."""
    df = parse_date(date_from, ("query", "date_from")) if date_from else None
    dt = parse_date(date_to, ("query", "date_to")) if date_to else None
    if df and dt and df > dt:
        raise invalid(("query", "date_from"), "date_from must not be after date_to")
    if status is not None and status not in PRESENT_STATUSES + NON_PRESENT_STATUSES:
        raise invalid(("query", "status"), "invalid status")
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if df or dt:
        q["date"] = {}
        if df:
            q["date"]["$gte"] = df.isoformat()
        if dt:
            q["date"]["$lte"] = dt.isoformat()
    if status:
        q["status"] = status
    return q


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_query(emp_code, date_from, date_to, status)
    # empty filter: metadata count instead of scanning the whole collection
    total = db.attendance_logs.count_documents(q) if q else db.attendance_logs.estimated_document_count()
    cur = db.attendance_logs.find(q).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_attendance(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    d = parse_date(date, ("path", "date")).isoformat()
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": d})
    if rec is None:
        raise HTTPException(404, "no record for that date")

    old_status = rec["status"]
    old_in = aware(rec["punch_in"]) if rec.get("punch_in") else None
    old_out = aware(rec["punch_out"]) if rec.get("punch_out") else None
    history = rec.get("history") or []

    new_status = body.status or old_status
    if new_status in NON_PRESENT_STATUSES:
        if body.punch_in is not None or body.punch_out is not None:
            raise invalid(("body",), f"punch times cannot be supplied with status {new_status}")
        new_in = new_out = None  # ABSENT / LEAVE clear both punch times
    else:
        new_in = trunc_seconds(from_ms(body.punch_in)) if body.punch_in is not None else old_in
        new_out = trunc_seconds(from_ms(body.punch_out)) if body.punch_out is not None else old_out
        if new_in is None:
            raise invalid(("body", "punch_in"), f"status {new_status} requires a punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != d:
            raise invalid(("body", "punch_in"), "punch_in must stay on the record's attendance date")
        if new_out is not None and (new_out <= new_in or new_out > new_in + MAX_SHIFT):
            raise invalid(("body", "punch_out"), "punch_out must be after punch_in and within 24 hours")

    if (new_status, new_in, new_out) == (old_status, old_in, old_out):
        raise invalid(("body",), "request changes nothing")

    new_vals = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
                **derive_fields(emp, d, new_status, new_in, new_out)}
    old_vals = {
        "status": old_status, "punch_in": old_in, "punch_out": old_out,
        "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False)),
    }
    changes = {k: {"from": old_vals[k], "to": v} for k, v in new_vals.items() if v != old_vals[k]}
    entry = {"at": now_seconds(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: only apply if the record is exactly as we read it
    # (same state and same number of history entries); otherwise someone else got there first.
    updated = db.attendance_logs.find_one_and_update(
        {
            "emp_code": emp_code, "date": d,
            "status": old_status, "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(history)]},
        },
        {"$set": new_vals, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; retry")
    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# Analytics: pipeline builders (shared by the endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
def _is_present():
    return {"$in": ["$status", PRESENT_STATUSES]}


def _weekday():
    """Mon-Fri on the record's IST date string ($dayOfWeek: 1=Sunday .. 7=Saturday)."""
    dow = {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
    return {"$and": [{"$gte": [dow, 2]}, {"$lte": [dow, 6]}]}


def _present_value():
    """R5-R7 contribution of one record to present_days."""
    return {"$cond": [{"$and": [_is_present(), _weekday()]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}


def _late_flag():
    return {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}


def monthly_pipeline(emp_code: str, month: str) -> list:
    start, nxt, _ = parse_month(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lt": nxt}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": _present_value()},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": _late_flag()},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def summary_pipeline(month: str, department: Optional[str]) -> list:
    """Runs on employees (so people without logs still count), joins logs per employee."""
    start, nxt, days = parse_month(month)
    last = f"{start[:8]}{days:02d}"
    match: dict = {"joined_on": {"$lte": last}}  # R9
    if department:
        match["department"] = department
    wh_ok = {"$and": [_is_present(), {"$isNumber": "$work_hours"}]}
    per_employee = [
        {"$match": {"$expr": {"$and": [
            {"$eq": ["$emp_code", "$$c"]}, {"$gte": ["$date", start]}, {"$lt": ["$date", nxt]},
        ]}}},
        {"$group": {
            "_id": None,
            "present": {"$sum": _present_value()},
            # work hours as integer hundredths so sums are exact; averaged over records
            "wh_cents": {"$sum": {"$cond": [wh_ok, {"$floor": {"$add": [{"$multiply": ["$work_hours", 100]}, 0.5]}}, 0]}},
            "wh_n": {"$sum": {"$cond": [wh_ok, 1, 0]}},
            "late_n": {"$sum": _late_flag()},
            "late_sum": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "leave": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
    ]

    def pick(field):
        return {"$sum": {"$ifNull": [{"$arrayElemAt": [f"$s.{field}", 0]}, 0]}}

    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"c": "$emp_code"}, "pipeline": per_employee, "as": "s"}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": pick("present"),
            "wh_cents": pick("wh_cents"),
            "wh_n": pick("wh_n"),
            "late_count": pick("late_n"),
            "total_late_minutes": pick("late_sum"),
            "leave_count": pick("leave"),
            "on_duty_count": pick("on_duty"),
        }},
        {"$sort": {"_id": 1}},
    ]


def leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    start, nxt, _ = parse_month(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": start, "$lt": nxt}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees are dropped
    ]
    if department:
        pipeline.append({"$match": {"e.department": department}})  # rank inside the department
    pipeline += [
        {"$project": {
            "_id": 0, "emp_code": "$_id", "name": "$e.name", "department": "$e.department",
            "total_late_minutes": 1, "late_count": 1,
        }},
        # standard competition ranking (1,2,2,4) computed by the database
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies after ranking
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]
    return pipeline


def trend_pipeline(codes: list, joined: list, frm: Date, to: Date) -> list:
    """One row per calendar day (gap-filled in the database) with a 7-row moving average."""
    n_days = (to - frm).days + 1
    day = {"$add": [{"$dateFromString": {"dateString": frm.isoformat()}}, {"$multiply": ["$$i", 86400000]}]}
    row = {"$let": {"vars": {"day": day}, "in": {"$let": {
        "vars": {"d": {"$dateToString": {"format": "%Y-%m-%d", "date": "$$day"}}, "dow": {"$dayOfWeek": "$$day"}},
        "in": {
            "date": "$$d",
            "wd": {"$and": [{"$gte": ["$$dow", 2]}, {"$lte": ["$$dow", 6]}]},
            "hc": {"$size": {"$filter": {"input": {"$literal": joined}, "as": "j", "cond": {"$lte": ["$$j", "$$d"]}}}},
            "st": {"$arrayElemAt": [
                {"$filter": {"input": "$daily", "as": "x", "cond": {"$eq": ["$$x._id", "$$d"]}}}, 0]},
        },
    }}}}
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": frm.isoformat(), "$lte": to.isoformat()}}},
        # $facet always yields exactly one document, even when no log matched
        {"$facet": {"daily": [{"$group": {
            "_id": "$date", "present": {"$sum": _present_value_any_day()}, "late": {"$sum": _late_flag()},
        }}]}},
        {"$project": {"rows": {"$map": {"input": {"$range": [0, n_days]}, "as": "i", "in": row}}}},
        {"$unwind": "$rows"},
        {"$replaceRoot": {"newRoot": "$rows"}},
        {"$addFields": {
            "present": {"$ifNull": ["$st.present", 0]},
            "late": {"$ifNull": ["$st.late", 0]},
        }},
        # rate in 1/10000 units, half-up: floor(x + 0.5); null off working days or with no headcount
        {"$addFields": {"rate_i": {"$cond": [
            {"$and": ["$wd", {"$gt": ["$hc", 0]}]},
            {"$floor": {"$add": [{"$divide": [{"$multiply": ["$present", 10000]}, "$hc"]}, 0.5]}},
            None,
        ]}}},
        {"$addFields": {"has": {"$cond": [{"$ne": ["$rate_i", None]}, 1, 0]}}},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "ma_sum": {"$sum": "$rate_i", "window": {"documents": [-6, 0]}},
                "ma_n": {"$sum": "$has", "window": {"documents": [-6, 0]}},
            },
        }},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": "$wd", "headcount": "$hc",
            "present_count": "$present", "late_count": "$late",
            "attendance_rate": {"$cond": [{"$eq": ["$has", 1]}, {"$divide": ["$rate_i", 10000]}, None]},
            "moving_avg_7d": {"$cond": [
                {"$gt": ["$ma_n", 0]},
                {"$divide": [{"$floor": {"$add": [{"$divide": ["$ma_sum", "$ma_n"]}, 0.5]}}, 10000]},
                None,
            ]},
        }},
        {"$sort": {"date": 1}},
    ]


def _present_value_any_day():
    """Trend counts presence on any day of the week (weekends included)."""
    return {"$cond": [_is_present(), {"$cond": ["$half_day", 0.5, 1]}, 0]}


def department_people(department: str) -> tuple[list, list]:
    docs = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    return [d["emp_code"] for d in docs], [d["joined_on"] for d in docs]


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, _, days = parse_month(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    agg = next(iter(db.attendance_logs.aggregate(monthly_pipeline(emp_code, month))), {})
    wd = working_days(start, days, emp["joined_on"])
    present = agg.get("present_days", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": round_half_up(present, 2),
        "leave_days": agg.get("leave_days", 0),
        "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        "attendance_pct": round_half_up(Decimal(str(present)) / wd * 100, 2) if wd else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    parse_month(month)
    items = []
    for r in db.employees.aggregate(summary_pipeline(month, department)):
        avg = None
        if r["wh_n"]:
            avg = round_half_up(Decimal(int(r["wh_cents"])) / Decimal(int(r["wh_n"])) / 100, 2)
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": round_half_up(r["present_days"], 2),
            "avg_work_hours": avg,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    parse_month(month)
    items = [
        {
            "rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"]),
        }
        for r in db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department))
    ]
    return {"month": month, "items": items}


def trend_range(frm: str, to: str) -> tuple[Date, Date]:
    f, t = parse_date(frm, ("query", "from")), parse_date(to, ("query", "to"))
    if t < f:
        raise invalid(("query", "to"), "to must not be before from")
    if (t - f).days + 1 > 92:
        raise invalid(("query", "to"), "range must not exceed 92 days")
    return f, t


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, frm: str = Query(alias="from"), to: str = Query()):
    f, t = trend_range(frm, to)
    codes, joined = department_people(department)
    if not codes:
        raise HTTPException(404, "unknown department")
    items = []
    for r in db.attendance_logs.aggregate(trend_pipeline(codes, joined, f, t)):
        r["headcount"] = int(r["headcount"])
        r["late_count"] = int(r["late_count"])
        r["present_count"] = round_half_up(r["present_count"], 2)
        items.append(r)
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def _need(**params):
    for name, value in params.items():
        if value is None:
            raise invalid(("query", name), f"{name} is required for this endpoint")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    frm: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        q = attendance_query(emp_code, date_from, date_to, status)
        command = {
            "find": collection, "filter": q, "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size, "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            _need(emp_code=emp_code, month=month)
            collection, pipeline = "attendance_logs", monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            _need(month=month)
            collection, pipeline = "employees", summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            _need(month=month)
            collection, pipeline = "attendance_logs", leaderboard_pipeline(month, limit, department)
        else:
            _need(department=department, **{"from": frm, "to": to})
            f, t = trend_range(frm, to)
            codes, joined = department_people(department)
            collection, pipeline = "attendance_logs", trend_pipeline(codes, joined, f, t)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    result = db.command({"explain": command, "verbosity": "executionStats"})
    # BSON types (ObjectId, Timestamp, ...) -> plain JSON
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(result))}
