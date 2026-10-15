# Employee Attendance & Analytics API

A FastAPI + MongoDB service for recording employee attendance (punch-in / punch-out, manual corrections) and reporting on it (monthly stats, department summaries, a late leaderboard and a daily trend).

## Setup

Requires Python 3.10+ and MongoDB 6.0 or newer (tested design target: 7.0)

```bash
python -m venv .venv
source .venv\Scripts\activate      #check for macos and linux commands
pip install -r requirements.txt

export MONGO_URI="mongodb://localhost:27017"  
export MONGO_DB="attendance_db"

uvicorn app.main:app --port 8000 --reload
```

Open this for Interactive docs in swagger UI: http://localhost:8000/docs

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness check (pings MongoDB, 503 if it is down) |
| POST / GET | `/employees` | Create / list employees (paged, filter by `department`) |
| POST | `/attendance/punch-in` | Start a day (`PRESENT`, `WFH` or `ON_DUTY`) |
| POST | `/attendance/punch-out` | Close the open record |
| GET | `/attendance` | List records (filters, paging) |
| PATCH | `/attendance/{emp_code}/{date}` | Manual correction, recorded in `history` |
| GET | `/analytics/employees/{emp_code}/monthly` | One employee's month |
| GET | `/analytics/departments/summary` | Per-department month totals |
| GET | `/analytics/leaderboard/late` | Most late minutes in a month |
| GET | `/analytics/departments/{department}/trend` | Daily attendance rate with 7-day moving average |
| GET | `/admin/explain/{endpoint}` | MongoDB `explain` for the queries behind an endpoint |

## Testing in Swagger

Leave `punched_at` out of the request body to punch with the current time. If you send it, it must be epoch milliseconds (a value that looks like seconds returns 422).
