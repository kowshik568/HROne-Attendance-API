import os
import enum
import logging
from contextlib import asynccontextmanager
from pathlib import Path as FilePath
from datetime import datetime, time, timedelta, date, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional, Literal

import certifi
os.environ['SSL_CERT_FILE'] = certifi.where()

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, Query, Body, Path, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, EmailStr, PositiveInt, conint, constr, validator
from pymongo import MongoClient, ASCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError, ConnectionFailure, ServerSelectionTimeoutError, ConfigurationError
from bson import ObjectId

# ---------------------------------------------------------------------------
# Configuration & Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load .env from the project root (parent of the `app` package directory),
# regardless of the current working directory the process was started from.
_PROJECT_ROOT = FilePath(__file__).resolve().parent.parent
load_dotenv(dotenv_path=_PROJECT_ROOT / ".env")


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "hrone")

if not MONGO_URI:
    raise ConfigError(
        "MONGO_URI environment variable is not set. Create a .env file in the "
        "project root (see .env.example) with a valid MongoDB connection "
        "string. The application will NOT silently fall back to "
        "mongodb://localhost:27017."
    )

# Bounded timeouts so a bad/unreachable URI fails fast with a clear error
# instead of hanging or producing a confusing traceback.
import certifi
client = MongoClient(
    MONGO_URI,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
    socketTimeoutMS=10000,
    tls=True,
    tlsCAFile=certifi.where(),
)
db = client[MONGO_DB]

EMPLOYEES = db["employees"]
ATTENDANCE = db["attendance"]


def verify_mongo_connection() -> None:
    """Verify connectivity without ever logging the URI or credentials."""
    try:
        client.admin.command("ping")
        logger.info("MongoDB connection verified (database=%s).", MONGO_DB)
    except ServerSelectionTimeoutError as exc:
        logger.error(
            "MongoDB server selection timed out. Check that MONGO_URI host is "
            "reachable, Atlas Network Access allows this IP, and credentials "
            "are correct. (error type: %s)", type(exc).__name__
        )
        raise
    except ConfigurationError as exc:
        logger.error(
            "MongoDB configuration/DNS error. Check the SRV hostname in "
            "MONGO_URI. (error type: %s)", type(exc).__name__
        )
        raise
    except ConnectionFailure as exc:
        logger.error(
            "MongoDB connection failed. (error type: %s)", type(exc).__name__
        )
        raise

# ---------------------------------------------------------------------------
# Helper constants & functions
# ---------------------------------------------------------------------------
IST = timedelta(hours=5, minutes=30)  # Simple offset; we keep everything in UTC internally
_EPOCH = datetime(1970, 1, 1)

def now_utc() -> datetime:
    """Current naive UTC datetime with microsecond truncated to seconds."""
    return datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)

def to_epoch_ms(dt: datetime) -> int:
    """Convert a naive UTC datetime to epoch milliseconds.

    NOTE: deliberately avoids ``datetime.timestamp()`` because that method
    interprets naive datetimes using the *host's local timezone*, which
    produced incorrect results on any machine not running in UTC. All
    datetimes in this module are naive-but-UTC by convention, so we compute
    the offset from the epoch directly.
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return int((dt - _EPOCH).total_seconds() * 1000)

def epoch_ms_to_dt(ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=ms)

def round_half_up(value: Decimal, places: int = 2) -> Decimal:
    quant = Decimal('1.' + '0' * places)
    return value.quantize(quant, rounding=ROUND_HALF_UP)

# ---------------------------------------------------------------------------
# Pydantic models (v2 syntax)
# ---------------------------------------------------------------------------
class ShiftTime(str):
    @classmethod
    def __get_validators__(cls):
        yield cls.validate

    @classmethod
    def validate(cls, v):
        try:
            datetime.strptime(v, "%H:%M")
        except Exception:
            raise ValueError("time must be HH:MM format")
        return v

class EmployeeBase(BaseModel):
    emp_code: constr(pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: EmailStr
    department: str = Field(..., min_length=1, max_length=100)
    shift_start: ShiftTime = Field(default="09:30")
    shift_end: ShiftTime = Field(default="18:30")
    joined_on: date

class EmployeeCreate(EmployeeBase):
    pass

class EmployeeResponse(EmployeeBase):
    created_at: int

class EmployeeListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: List[EmployeeResponse]

class AttendanceStatus(str, enum.Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"

class PunchInRequest(BaseModel):
    emp_code: constr(pattern=r"^EMP\d{4,6}$")
    status: AttendanceStatus = Field(..., description="One of PRESENT, WFH, ON_DUTY")

    @validator("status")
    def allowed_status(cls, v):
        if v not in {AttendanceStatus.PRESENT, AttendanceStatus.WFH, AttendanceStatus.ON_DUTY}:
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return v

class PunchOutRequest(BaseModel):
    emp_code: constr(pattern=r"^EMP\d{4,6}$")

class AttendanceRecord(BaseModel):
    emp_code: str
    date: date
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    status: Optional[AttendanceStatus] = None
    late_minutes: Optional[int] = None
    overtime_minutes: Optional[int] = None
    work_hours: Optional[Decimal] = None
    half_day: Optional[bool] = None
    reason: Optional[constr(min_length=5, max_length=200)] = None
    regularized_by: Optional[constr(min_length=1, max_length=50)] = None
    history: Optional[List[dict]] = None

class AttendanceListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: List[AttendanceRecord]

class AttendancePatch(BaseModel):
    status: Optional[AttendanceStatus] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: Optional[constr(min_length=5, max_length=200)] = None
    regularized_by: Optional[constr(min_length=1, max_length=50)] = None

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="HROne Employee Attendance & Analytics API")

# ---------------------------------------------------------------------------
# Startup – create indexes
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup_indexes():
    # Employees – unique emp_code
    EMPLOYEES.create_index([("emp_code", ASCENDING)], unique=True)
    # Attendance – unique per employee per date (date stored as YYYY-MM-DD string)
    ATTENDANCE.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    # Additional indexes for common queries
    ATTENDANCE.create_index([("date", ASCENDING)])
    ATTENDANCE.create_index([("status", ASCENDING)])
    logger.info("Indexes ensured")

# ---------------------------------------------------------------------------
# Helpers for shift calculations (all in UTC internal, but treat input times as IST)
# ---------------------------------------------------------------------------
def get_shift_datetimes(emp_doc: dict, target_date: date) -> tuple[datetime, datetime]:
    """Return shift_start_dt and shift_end_dt (UTC) for the given employee on target_date.
    Handles overnight shifts where shift_end <= shift_start.
    """
    shift_start = datetime.combine(target_date, datetime.strptime(emp_doc["shift_start"], "%H:%M").time())
    shift_end = datetime.combine(target_date, datetime.strptime(emp_doc["shift_end"], "%H:%M").time())
    # Apply IST offset to get UTC equivalents
    shift_start_utc = shift_start - IST
    shift_end_utc = shift_end - IST
    if shift_end_utc <= shift_start_utc:
        # Overnight – shift_end belongs to next day
        shift_end_utc += timedelta(days=1)
    return shift_start_utc, shift_end_utc

def calculate_late(punch_in_ts: int, shift_start_utc: datetime) -> int:
    punch_in_dt = epoch_ms_to_dt(punch_in_ts)
    diff = (punch_in_dt - shift_start_utc).total_seconds() / 60
    if diff <= 10:
        return 0
    return int(diff)  # floor minutes

def calculate_overtime(punch_out_ts: int, shift_end_utc: datetime) -> int:
    punch_out_dt = epoch_ms_to_dt(punch_out_ts)
    diff = (punch_out_dt - shift_end_utc).total_seconds() / 60
    if diff < 30:
        return 0
    return int(diff)

def calculate_work_hours(punch_in_ts: int, punch_out_ts: int) -> Decimal:
    secs = (punch_out_ts - punch_in_ts) / 1000
    hours = Decimal(secs) / Decimal(3600)
    return round_half_up(hours, 2)

# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------
@app.get("/health")
async def health_check():
    try:
        # cheap ping
        client.admin.command('ping')
        return {"status": "ok"}
    except PyMongoError as e:
        logger.error(f"Mongo health check failed: {e}")
        raise HTTPException(status_code=503, detail="MongoDB unavailable")

# ---------------------------------------------------------------------------
# Employee endpoints
# ---------------------------------------------------------------------------
@app.post("/employees", response_model=EmployeeResponse, status_code=201)
async def create_employee(emp: EmployeeCreate):
    doc = emp.model_dump()
    doc["created_at"] = to_epoch_ms(now_utc())
    try:
        EMPLOYEES.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Employee already exists")
    return EmployeeResponse(**doc)

@app.get("/employees", response_model=EmployeeListResponse)
async def list_employees(
    department: Optional[str] = Query(None),
    page: PositiveInt = Query(1, ge=1),
    page_size: PositiveInt = Query(20, le=100),
):
    filter_: dict = {}
    if department:
        filter_["department"] = department  # case‑sensitive exact match
    total = EMPLOYEES.count_documents(filter_)
    cursor = (
        EMPLOYEES.find(filter_)
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [EmployeeResponse(**doc) for doc in cursor]
    return EmployeeListResponse(total=total, page=page, page_size=page_size, items=items)

# ---------------------------------------------------------------------------
# Attendance endpoints
# ---------------------------------------------------------------------------
def attendance_date_from_timestamp(ts: int) -> date:
    # Convert epoch ms to date in IST (so that a punch at 02:00 IST belongs to previous day if overnight)
    dt_ist = epoch_ms_to_dt(ts) + IST
    return dt_ist.date()

@app.post("/attendance/punch-in", status_code=201)
async def punch_in(req: PunchInRequest):
    # Find employee to get shift times
    emp_doc = EMPLOYEES.find_one({"emp_code": req.emp_code})
    if not emp_doc:
        raise HTTPException(status_code=404, detail="Employee not found")
    now = now_utc()
    now_ms = to_epoch_ms(now)
    # Determine logical attendance date according to rule R1 (overnight handling)
    shift_start_utc, shift_end_utc = get_shift_datetimes(emp_doc, now.date())
    # If current time (UTC) is before shift_end_utc and shift_end_utc is on next day, treat as previous day
    attendance_date = now.date()
    if shift_end_utc <= shift_start_utc:  # overnight shift
        if now < shift_end_utc:
            attendance_date = (now - timedelta(days=1)).date()
    # Prepare document – unique per emp_code+date
    doc = {
        "emp_code": req.emp_code,
        "date": attendance_date.isoformat(),  # store as string for easy indexing
        "punch_in": now_ms,
        "status": req.status.value,
        "history": [],
    }
    try:
        ATTENDANCE.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Punch‑in already recorded for this date")
    return {"msg": "Punch‑in recorded", "date": attendance_date.isoformat()}

@app.post("/attendance/punch-out", status_code=200)
async def punch_out(req: PunchOutRequest):
    # Find most recent open record (punch_out missing) for employee
    cursor = ATTENDANCE.find({"emp_code": req.emp_code, "punch_out": {"$exists": False}}).sort("date", -1).limit(1)
    doc = next(cursor, None)
    if not doc:
        raise HTTPException(status_code=404, detail="Open attendance record not found")
    now_ms = to_epoch_ms(now_utc())
    # Compute derived fields using employee shift info
    emp_doc = EMPLOYEES.find_one({"emp_code": req.emp_code})
    if not emp_doc:
        raise HTTPException(status_code=404, detail="Employee not found")
    shift_start_utc, shift_end_utc = get_shift_datetimes(emp_doc, datetime.fromisoformat(doc["date"]).date())
    late = calculate_late(doc["punch_in"], shift_start_utc)
    overtime = calculate_overtime(now_ms, shift_end_utc)
    work_hours = calculate_work_hours(doc["punch_in"], now_ms)
    half_day = work_hours < Decimal('4.5')
    update = {
        "$set": {
            "punch_out": now_ms,
            "late_minutes": late,
            "overtime_minutes": overtime,
            "work_hours": float(work_hours),
            "half_day": half_day,
        }
    }
    ATTENDANCE.update_one({"_id": doc["_id"]}, update)
    return {"msg": "Punch‑out recorded", "date": doc["date"]}

@app.get("/attendance", response_model=AttendanceListResponse)
async def get_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    status: Optional[AttendanceStatus] = Query(None),
    page: PositiveInt = Query(1, ge=1),
    page_size: PositiveInt = Query(20, le=100),
):
    filter_: dict = {}
    if emp_code:
        filter_["emp_code"] = emp_code
    if status:
        filter_["status"] = status.value
    if date_from or date_to:
        date_filter = {}
        if date_from:
            date_filter["$gte"] = date_from.isoformat()
        if date_to:
            date_filter["$lte"] = date_to.isoformat()
        filter_["date"] = date_filter
    total = ATTENDANCE.count_documents(filter_)
    cursor = (
        ATTENDANCE.find(filter_)
        .sort([("date", -1), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [AttendanceRecord(**doc) for doc in cursor]
    return AttendanceListResponse(total=total, page=page, page_size=page_size, items=items)

@app.patch("/attendance/{emp_code}/{att_date}")
async def regularize_attendance(
    emp_code: str = Path(..., pattern=r"^EMP\d{4,6}$"),
    att_date: date = Path(...),
    body: AttendancePatch = Body(...),
    by: str = Query(..., min_length=1, max_length=50, description="User performing regularization"),
):
    # Locate record
    doc = ATTENDANCE.find_one({"emp_code": emp_code, "date": att_date.isoformat()})
    if not doc:
        raise HTTPException(status_code=404, detail="Attendance record not found")
    updates = {}
    changes = {}
    # Helper to record diffs
    def record_change(field, old, new):
        if old != new:
            changes[field] = {"from": old, "to": new}
    if body.status is not None:
        record_change("status", doc.get("status"), body.status.value)
        updates["status"] = body.status.value
    if body.punch_in is not None:
        record_change("punch_in", doc.get("punch_in"), body.punch_in)
        updates["punch_in"] = body.punch_in
    if body.punch_out is not None:
        record_change("punch_out", doc.get("punch_out"), body.punch_out)
        updates["punch_out"] = body.punch_out
    if body.reason is not None:
        record_change("reason", doc.get("reason"), body.reason)
        updates["reason"] = body.reason
    if body.regularized_by is not None:
        record_change("regularized_by", doc.get("regularized_by"), body.regularized_by)
        updates["regularized_by"] = body.regularized_by
    # Re‑compute derived fields if punch_in/out changed
    emp_doc = EMPLOYEES.find_one({"emp_code": emp_code})
    if not emp_doc:
        raise HTTPException(status_code=404, detail="Employee not found")
    if "punch_in" in updates or "punch_out" in updates:
        punch_in_ts = updates.get("punch_in", doc.get("punch_in"))
        punch_out_ts = updates.get("punch_out", doc.get("punch_out"))
        if punch_in_ts and punch_out_ts:
            shift_start_utc, shift_end_utc = get_shift_datetimes(emp_doc, att_date)
            late = calculate_late(punch_in_ts, shift_start_utc)
            overtime = calculate_overtime(punch_out_ts, shift_end_utc)
            work_hours = calculate_work_hours(punch_in_ts, punch_out_ts)
            half_day = work_hours < Decimal('4.5')
            updates.update({
                "late_minutes": late,
                "overtime_minutes": overtime,
                "work_hours": float(work_hours),
                "half_day": half_day,
            })
    if not updates:
        raise HTTPException(status_code=400, detail="No updatable fields provided")
    # Append single history entry
    history_entry = {
        "at": to_epoch_ms(now_utc()),
        "by": by,
        "reason": body.reason or "",
        "changes": changes,
    }
    ATTENDANCE.update_one({"_id": doc["_id"]}, {"$set": updates, "$push": {"history": history_entry}})
    return {"msg": "Attendance regularized"}

# ---------------------------------------------------------------------------
# Analytics endpoints (simplified implementations using aggregation)
# ---------------------------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
async def employee_monthly(emp_code: str, month: str = Query(..., regex=r"^\d{4}-\d{2}$")):
    # month format YYYY-MM
    start = f"{month}-01"
    # compute end of month
    start_dt = datetime.strptime(start, "%Y-%m-%d")
    next_month = (start_dt.replace(day=28) + timedelta(days=4)).replace(day=1)
    end = next_month.strftime("%Y-%m-%d")
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lt": end}}},
        {"$group": {
            "_id": None,
            "working_days": {"$sum": 1},
            "present_days": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, 1, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            "attendance_pct": {"$avg": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, 1, 0]}}
        } }
    ]

    result = list(ATTENDANCE.aggregate(pipeline))
    if not result:
        return JSONResponse(content={}, status_code=404)
    return result[0]

@app.get("/analytics/departments/summary")
async def departments_summary(month: str = Query(..., regex=r"^\d{4}-\d{2}$")):
    start = f"{month}-01"
    start_dt = datetime.strptime(start, "%Y-%m-%d")
    next_month = (start_dt.replace(day=28) + timedelta(days=4)).replace(day=1)
    end = next_month.strftime("%Y-%m-%d")
    pipeline = [
        {"$match": {"date": {"$gte": start, "$lt": end}}},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"},
        {"$group": {
            "_id": "$emp.department",
            "headcount": {"$addToSet": "$emp.emp_code"},
            "avg_work_hours": {"$avg": "$work_hours"}
        }},
        {"$project": {
            "department": "$_id",
            "headcount": {"$size": "$headcount"},
            "avg_work_hours": {"$round": ["$avg_work_hours", 2]}
        }}
    ]
    return list(ATTENDANCE.aggregate(pipeline))

@app.get("/analytics/leaderboard/late")
async def late_leaderboard(limit: int = Query(10, gt=0)):
    pipeline = [
        {"$match": {"late_minutes": {"$gt": 0}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"}
        }},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$setWindowFields": {
            "output": {
                "rank": {"$rank": {}},
                "prev_late": {"$lag": {"output": "$total_late_minutes", "by": 1}}
            }
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"emp_code": "$_id", "total_late_minutes": 1, "rank": 1, "_id": 0}}
    ]
    return list(ATTENDANCE.aggregate(pipeline))

@app.get("/analytics/departments/{department}/trend")
async def department_trend(
    department: str,
    from_date: date = Query(...),
    to_date: date = Query(...),
):
    pipeline = [
        {"$match": {"date": {"$gte": from_date.isoformat(), "$lte": to_date.isoformat()}}},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}
        }},
        {"$densify": {"field": "_id", "range": {"step": 1, "bounds": [from_date.isoformat(), to_date.isoformat()]}, "type": "date"}},
        {"$addFields": {"is_working_day": {"$cond": [{"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$_id"}}}, [2,3,4,5,6]]}, True, False]}}},
        {"$lookup": {
            "from": "employees",
            "let": {"date": "$_id"},
            "pipeline": [
                {"$match": {"department": department, "joined_on": {"$lte": "$$date"}}},
                {"$group": {"_id": null, "headcount": {"$sum": 1}}}
            ],
            "as": "head"
        }},
        {"$addFields": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$head.headcount", 0]}, 0]}}},
        {"$setWindowFields": {
            "partitionBy": null,
            "sortBy": {"_id": 1},
            "output": {"moving_avg_7d": {"$avg": "$present_count", "window": {"range": [-6, 0]}}}
        }},
        {"$project": {"date": "$_id", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": {"$cond": [{"$gt": ["$headcount", 0]}, {"$divide": ["$present_count", "$headcount"]}, None]}, "moving_avg_7d": 1, "_id": 0}}
    ]
    return list(ATTENDANCE.aggregate(pipeline))

# ---------------------------------------------------------------------------
# Admin explain endpoint
# ---------------------------------------------------------------------------
@app.get("/admin/explain/{endpoint}")
async def explain(endpoint: str):
    mapping = {
        "attendance_list": lambda: ATTENDANCE.find().explain("executionStats"),
        "employee_monthly": lambda: ATTENDANCE.aggregate([{"$match": {"emp_code": "DUMMY"}}]).explain("executionStats"),
        "department_summary": lambda: ATTENDANCE.aggregate([{"$match": {"date": "2020-01-01"}}]).explain("executionStats"),
        "late_leaderboard": lambda: ATTENDANCE.aggregate([{"$match": {"late_minutes": {"$gt": 0}}}]).explain("executionStats"),
        "department_trend": lambda: ATTENDANCE.aggregate([{"$match": {"date": "2020-01-01"}}]).explain("executionStats"),
    }
    if endpoint not in mapping:
        raise HTTPException(status_code=400, detail="Unknown endpoint for explain")
    # Execute explain – note: the pipeline here is placeholder; real explain would need params
    return mapping[endpoint]()

