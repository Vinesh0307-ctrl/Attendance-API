"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date as dt_date
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])

    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers & Business Rules (R1 - R10)
# --------------------------------------------------------------------------- #
def round_half_up(value: Any, decimals: int) -> float:
    if value is None:
        return None
    d = Decimal(str(value))
    exp = Decimal("10") ** (-decimals)
    return float(d.quantize(exp, rounding=ROUND_HALF_UP))


def to_epoch_ms(dt_val: Optional[datetime]) -> Optional[int]:
    if dt_val is None:
        return None
    if isinstance(dt_val, (int, float)):
        return int(dt_val)
    if isinstance(dt_val, datetime):
        if dt_val.tzinfo is None:
            dt_val = dt_val.replace(tzinfo=timezone.utc)
        return int(dt_val.timestamp() * 1000)
    return None


def from_epoch_ms(ms: int) -> datetime:
    truncated_sec = ms // 1000
    return datetime.fromtimestamp(truncated_sec, tz=timezone.utc)


def parse_time_str(time_str: str) -> tuple[int, int]:
    h, m = map(int, time_str.split(":"))
    return h, m


def get_attendance_date(punch_in_dt_utc: datetime, shift_start: str, shift_end: str) -> str:
    ist_dt = punch_in_dt_utc.astimezone(IST)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        eh, em = parse_time_str(shift_end)
        end_time = time(eh, em, 0)
        if ist_dt.time() < end_time:
            return (ist_dt.date() - timedelta(days=1)).isoformat()
    return ist_dt.date().isoformat()


def get_shift_datetimes(date_str: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    base_date = dt_date.fromisoformat(date_str)
    sh, sm = parse_time_str(shift_start)
    eh, em = parse_time_str(shift_end)

    start_dt = datetime(base_date.year, base_date.month, base_date.day, sh, sm, 0, tzinfo=IST)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        end_date = base_date + timedelta(days=1)
        end_dt = datetime(end_date.year, end_date.month, end_date.day, eh, em, 0, tzinfo=IST)
    else:
        end_dt = datetime(base_date.year, base_date.month, base_date.day, eh, em, 0, tzinfo=IST)
    return start_dt, end_dt


def compute_late_minutes(punch_in_dt_utc: datetime, shift_start_dt_ist: datetime) -> int:
    punch_in_ist = punch_in_dt_utc.astimezone(IST)
    diff_sec = (punch_in_ist - shift_start_dt_ist).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_dt_utc: datetime, punch_out_dt_utc: datetime) -> float:
    sec = (punch_out_dt_utc - punch_in_dt_utc).total_seconds()
    d_sec = Decimal(str(int(sec)))
    hours = (d_sec / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime(punch_out_dt_utc: datetime, shift_end_dt_ist: datetime) -> int:
    punch_out_ist = punch_out_dt_utc.astimezone(IST)
    diff_sec = (punch_out_ist - shift_end_dt_ist).total_seconds()
    minutes = int(diff_sec // 60)
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    if work_hours is None:
        return False
    return work_hours < 4.50


def count_working_days_in_month(month_str: str, joined_on_str: str) -> int:
    year, month = map(int, month_str.split("-"))
    num_days = calendar.monthrange(year, month)[1]
    first_day = dt_date(year, month, 1)
    last_day = dt_date(year, month, num_days)

    joined_on = dt_date.fromisoformat(joined_on_str)
    if joined_on > last_day:
        return 0

    start_date = max(first_day, joined_on)
    working_days = 0
    curr = start_date
    while curr <= last_day:
        if curr.weekday() < 5:  # 0=Monday, 4=Friday
            working_days += 1
        curr += timedelta(days=1)
    return working_days


def format_attendance_doc(doc: dict) -> dict:
    hist = []
    for h in doc.get("history", []) or []:
        changes = {}
        for k, v in h.get("changes", {}).items():
            fr = v.get("from")
            to = v.get("to")
            if k in ("punch_in", "punch_out"):
                fr = to_epoch_ms(fr) if fr is not None else None
                to = to_epoch_ms(to) if to is not None else None
            changes[k] = {"from": fr, "to": to}
        hist.append({
            "at": to_epoch_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": hist,
    }


def format_employee_doc(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Request & Response Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            dt_date.fromisoformat(v)
        except Exception:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return format_employee_doc(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1),page_size: int = Query(20, ge=1, le=100),):
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [format_employee_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_dt = from_epoch_ms(body.punched_at)
    else:
        now_sec = int(datetime.now(timezone.utc).timestamp())
        punch_in_dt = datetime.fromtimestamp(now_sec, tz=timezone.utc)

    att_date = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    shift_start_dt, _ = get_shift_datetimes(att_date, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_dt, shift_start_dt)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = from_epoch_ms(body.punched_at)
    else:
        now_sec = int(datetime.now(timezone.utc).timestamp())
        punch_out_dt = datetime.fromtimestamp(now_sec, tz=timezone.utc)

    candidate = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}}, sort=[("punch_in", DESCENDING)], )
    if not candidate:
        raise HTTPException(status_code=404, detail="no punch-in found")

    if candidate.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record already punched out")

    punch_in_dt = candidate["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    # Validate punched_at <= punch_in or > 24 hours
    duration_sec = (punch_out_dt - punch_in_dt).total_seconds()
    if duration_sec <= 0 or duration_sec > 86400:
        raise HTTPException(status_code=422, detail="punched_at must be strictly greater than punch_in and within 24 hours",)

    att_date = candidate["date"]
    _, shift_end_dt = get_shift_datetimes(att_date, emp["shift_start"], emp["shift_end"])

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_min = compute_overtime(punch_out_dt, shift_end_dt)
    half_day = compute_half_day(work_hours)

    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": body.emp_code,"date": att_date,"punch_out": None, },
        {"$set": {"punch_out": punch_out_dt, "work_hours": work_hours, "overtime_minutes": overtime_min, "half_day": half_day,}
        },
        return_document=True,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="record already punched out")

    return format_attendance_doc(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code

    if date_from or date_to:
        if date_from:
            try:
                dt_date.fromisoformat(date_from)
            except Exception:
                raise HTTPException(status_code=422, detail="Invalid date_from")
        if date_to:
            try:
                dt_date.fromisoformat(date_to)
            except Exception:
                raise HTTPException(status_code=422, detail="Invalid date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    if status:
        if status not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise HTTPException(status_code=422, detail="Invalid status")
        q["status"] = status

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    item = (db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size))

    items = [format_attendance_doc(d) for d in item]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str,date: str, body: RegularizeRequest,):
    try:
        dt_date.fromisoformat(date)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid date format")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=404, detail="Attendance record not found for this date")
    new_status = body.status if body.status is not None else rec["status"]
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="Setting status ABSENT or LEAVE clears punch times; punch times cannot be supplied",)
        final_punch_in = None
        final_punch_out = None
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        if body.punch_in is not None:
            final_punch_in = from_epoch_ms(body.punch_in)
        else:
            final_punch_in = rec.get("punch_in")
            if final_punch_in and final_punch_in.tzinfo is None:
                final_punch_in = final_punch_in.replace(tzinfo=timezone.utc)

        if final_punch_in is None:
            raise HTTPException(status_code=422, detail="A presence status requires a punch_in")
        calc_date = get_attendance_date(final_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(status_code=422, detail="punch_in does not belong to this attendance date",)

        if body.punch_out is not None:
            final_punch_out = from_epoch_ms(body.punch_out)
        else:
            final_punch_out = rec.get("punch_out")
            if final_punch_out and final_punch_out.tzinfo is None:
                final_punch_out = final_punch_out.replace(tzinfo=timezone.utc)

        shift_start_dt, shift_end_dt = get_shift_datetimes(date, emp["shift_start"], emp["shift_end"])
        final_late_minutes = compute_late_minutes(final_punch_in, shift_start_dt)

        if final_punch_out is not None:
            dur = (final_punch_out - final_punch_in).total_seconds()
            if dur <= 0 or dur > 86400:
                raise HTTPException(status_code=422,detail="punch_out must be after punch_in and within 24 hours",)
            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_overtime_minutes = compute_overtime(final_punch_out, shift_end_dt)
            final_half_day = compute_half_day(final_work_hours)
        else:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False


    changes_db = {}
    changes_api = {}

    def check_diff(field_name: str, old_val: Any, new_val: Any, is_instant: bool = False):
        if is_instant:
            old_ms = to_epoch_ms(old_val)
            new_ms = to_epoch_ms(new_val)
            if old_ms != new_ms:
                changes_db[field_name] = {"from": old_val, "to": new_val}
                changes_api[field_name] = {"from": old_ms, "to": new_ms}
        else:
            if old_val != new_val:
                changes_db[field_name] = {"from": old_val, "to": new_val}
                changes_api[field_name] = {"from": old_val, "to": new_val}

    check_diff("status", rec.get("status"), new_status)
    check_diff("punch_in", rec.get("punch_in"), final_punch_in, is_instant=True)
    check_diff("punch_out", rec.get("punch_out"), final_punch_out, is_instant=True)
    check_diff("work_hours", rec.get("work_hours"), final_work_hours)
    check_diff("late_minutes", rec.get("late_minutes", 0), final_late_minutes)
    check_diff("overtime_minutes", rec.get("overtime_minutes", 0), final_overtime_minutes)
    check_diff("half_day", bool(rec.get("half_day", False)), final_half_day)

    if not changes_db:
        raise HTTPException(status_code=422, detail="A request that changes nothing is 422")

    now_utc = datetime.now(timezone.utc)
    now_truncated = datetime.fromtimestamp(int(now_utc.timestamp()), tz=timezone.utc)

    history_entry_db = {"at": now_truncated,"by": body.regularized_by,"reason": body.reason,"changes": changes_db,}
    existing_history = rec.get("history", []) or []
    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": emp_code,"date": date,"history": {"$size": len(existing_history)},},
        {"$set": {"status": new_status,"punch_in": final_punch_in,"punch_out": final_punch_out,"work_hours": final_work_hours,"late_minutes": final_late_minutes,"overtime_minutes": final_overtime_minutes,"half_day": final_half_day,},
            "$push": {"history": history_entry_db,},
        },
        return_document=True,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict")

    return format_attendance_doc(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str,month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    working_days = count_working_days_in_month(month, emp["joined_on"])

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {
            "$group": {"_id": "$emp_code","present_days": {"$sum": {"$cond": [{"$and":
                                    [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},{"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]},]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},0.0,]}},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]} },
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        data = res[0]
        present_days = float(data.get("present_days", 0.0))
        leave_days = int(data.get("leave_days", 0))
        late_count = int(data.get("late_count", 0))
        total_late = int(data.get("total_late_minutes", 0))
        total_ot = int(data.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        attendance_pct = round_half_up(present_days / working_days * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),department: Optional[str] = None,):
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department is not None:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp": "$emp_code"},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$emp"]},
                {"$gte": ["$date", month_start]},
                {"$lte": ["$date", month_end]}
            ]}}}],
            "as": "logs"
        }},
        {"$project": {
            "department": 1,
            "present_days": {"$sum": {"$map": {
                "input": "$logs", "as": "l",
                "in": {"$cond": [
                    {"$and": [
                        {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, 5]}
                    ]},
                    {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1.0]},
                    0.0
                ]}
            }}},
            "work_hours_sum": {"$sum": {"$map": {
                "input": "$logs", "as": "l",
                "in": {"$cond": [
                    {"$and": [
                        {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$ne": ["$$l.work_hours", None]}
                    ]},
                    "$$l.work_hours", 0.0
                ]}
            }}},
            "work_hours_count": {"$sum": {"$map": {
                "input": "$logs", "as": "l",
                "in": {"$cond": [
                    {"$and": [
                        {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$ne": ["$$l.work_hours", None]}
                    ]},
                    1, 0
                ]}
            }}},
            "late_count": {"$sum": {"$map": {
                "input": "$logs", "as": "l",
                "in": {"$cond": [{"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}, 1, 0]}
            }}},
            "total_late_minutes": {"$sum": {"$map": {
                "input": "$logs", "as": "l",
                "in": {"$ifNull": ["$$l.late_minutes", 0]}
            }}},
            "leave_count": {"$sum": {"$map": {
                "input": "$logs", "as": "l",
                "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]}
            }}},
            "on_duty_count": {"$sum": {"$map": {
                "input": "$logs", "as": "l",
                "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]}
            }}}
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "work_hours_sum": {"$sum": "$work_hours_sum"},
            "work_hours_count": {"$sum": "$work_hours_count"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"}
        }},
        {"$sort": {"_id": ASCENDING}}
    ]
    res = list(db.employees.aggregate(pipeline))
    items = []
    for r in res:
        w_count = r["work_hours_count"]
        w_sum = r["work_hours_sum"]
        avg_wh = round_half_up(w_sum / w_count, 2) if w_count > 0 else None
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": float(r["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),limit: int = Query(10, ge=1, le=50),department: Optional[str] = None,):
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    pipeline: List[Dict[str, Any]] = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
                      "total_late_minutes": "$total_late_minutes", "late_count": "$late_count"}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": DESCENDING}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
    ])

    rows = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for row in rows:
        items.append({
            "rank": row["rank"],
            "emp_code": row["emp_code"],
            "name": row["name"],
            "department": row["department"],
            "total_late_minutes": int(row["total_late_minutes"]),
            "late_count": int(row["late_count"]),
        })

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str,from_: str = Query(..., alias="from"),to: str = Query(...),):
    try:
        from_date = dt_date.fromisoformat(from_)
        to_date = dt_date.fromisoformat(to)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid date format for from or to")

    if to_date < from_date:
        raise HTTPException(status_code=422, detail="to date cannot be earlier than from date")

    diff_days = (to_date - from_date).days
    if diff_days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")
    dept_emp_count = db.employees.count_documents({"department": department})
    if dept_emp_count == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    from_dt = datetime(from_date.year, from_date.month, from_date.day)
    to_dt_exclusive = datetime(to_date.year, to_date.month, to_date.day) + timedelta(days=1)

    pipeline = [
        {"$match": {"date": {"$gte": from_, "$lte": to}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$unionWith": {"coll": "employees", "pipeline": [
            {"$match": {"department": department}},
            {"$limit": 1},
            {"$project": {"_id": 0, "date": {"$literal": from_}, "is_seed": {"$literal": True}}}
        ]}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [
                {"$eq": ["$is_seed", True]}, 0.0,
                {"$cond": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                    0.0
                ]}
            ]}},
            "late_count": {"$sum": {"$cond": [
                {"$eq": ["$is_seed", True]}, 0,
                {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
            ]}}
        }},
        {"$addFields": {"date_dt": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$densify": {"field": "date_dt", "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_dt_exclusive]}}},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
            "date_dt": 1,
            "present_count": {"$ifNull": ["$present_count", 0.0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$date_dt"}, 5]}
        }},
        {"$lookup": {"from": "employees", "pipeline": [
            {"$match": {"department": department}},
            {"$project": {"joined_on": 1}}
        ], "as": "all_dept_emps"}},
        {"$addFields": {"headcount": {"$size": {"$filter": {
            "input": "$all_dept_emps", "as": "e",
            "cond": {"$lte": ["$$e.joined_on", "$date"]}
        }}}}},
        {"$addFields": {"attendance_rate": {"$cond": [
            {"$and": [{"$eq": ["$is_working_day", True]}, {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]},
            None
        ]}}},
        {"$setWindowFields": {"sortBy": {"date": ASCENDING}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, "current"]}}
        }}},
        {"$sort": {"date": ASCENDING}}
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for r in rows:
        att_rate = round_half_up(r["attendance_rate"], 4) if r["attendance_rate"] is not None else None
        m_avg = round_half_up(r["moving_avg_7d"], 4) if r["moving_avg_7d"] is not None else None
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": att_rate,
            "moving_avg_7d": m_avg,
        })

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            if date_from and date_to and date_from > date_to:
                raise HTTPException(status_code=422, detail="date_from > date_to")
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            if status not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
                raise HTTPException(status_code=422, detail="Invalid status")
            q["status"] = status

        skip = (page - 1) * page_size
        exp = db.command(
            "explain",
            {"find": "attendance_logs","filter": q,"sort": {"date": -1, "emp_code": 1},"skip": skip,"limit": page_size,},
            verbosity="executionStats",
        )
        exp_clean = json.loads(json_util.dumps(exp))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp_clean}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{num_days:02d}"

        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
            {"$group": {
                "_id": "$emp_code",
                "present_days": {"$sum": {"$cond": [
                    {"$and": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]}
                    ]},
                    {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                    0.0
                ]}},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}
            }}
        ]
        exp = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        exp_clean = json.loads(json_util.dumps(exp))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp_clean}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{num_days:02d}"

        match_stage: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
        if department is not None:
            match_stage["department"] = department

        pipeline = [
            {"$match": match_stage},
            {"$lookup": {"from": "attendance_logs", "let": {"emp": "$emp_code"}, "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$emp"]},
                    {"$gte": ["$date", month_start]},
                    {"$lte": ["$date", month_end]}
                ]}}}
            ], "as": "logs"}},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}}}
        ]
        exp = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        exp_clean = json.loads(json_util.dumps(exp))
        return {"endpoint": endpoint, "collection": "employees", "explain": exp_clean}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{num_days:02d}"

        pipeline = [
            {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"},
                        "late_count": {"$sum": 1}}},
            {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"},
        ]
        if department is not None:
            pipeline.append({"$match": {"emp.department": department}})

        pipeline.extend([
            {"$setWindowFields": {"sortBy": {"total_late_minutes": DESCENDING}, "output": {"rank": {"$rank": {}}}}},
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
        ])

        exp = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        exp_clean = json.loads(json_util.dumps(exp))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp_clean}

    elif endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        try:
            from_date = dt_date.fromisoformat(from_)
            to_date = dt_date.fromisoformat(to)
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid date format")

        if to_date < from_date:
            raise HTTPException(status_code=422, detail="to cannot be earlier than from")
        if (to_date - from_date).days > 92:
            raise HTTPException(status_code=422, detail="Range cannot exceed 92 days")

        from_dt = datetime(from_date.year, from_date.month, from_date.day)
        to_dt_exclusive = datetime(to_date.year, to_date.month, to_date.day) + timedelta(days=1)

        pipeline = [
            {"$match": {"date": {"$gte": from_, "$lte": to}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}},
            {"$unionWith": {"coll": "employees", "pipeline": [
                {"$match": {"department": department}},
                {"$limit": 1},
                {"$project": {"_id": 0, "date": {"$literal": from_}, "is_seed": {"$literal": True}}}
            ]}},
            {"$group": {"_id": "$date", "present_count": {"$sum": 1}}},
            {"$addFields": {"date_dt": {"$dateFromString": {"dateString": "$_id"}}}},
            {"$densify": {"field": "date_dt",
                          "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_dt_exclusive]}}},
        ]
        exp = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        exp_clean = json.loads(json_util.dumps(exp))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp_clean}

    raise HTTPException(status_code=422, detail="Unknown endpoint")
