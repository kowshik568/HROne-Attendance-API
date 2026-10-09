# README.md

## HROne Employee Attendance & Analytics API

### Overview
A FastAPI backend implementing the full HROne employee attendance and analytics specification. It uses:
- **Python 3.11+**
- **FastAPI** for the HTTP layer
- **MongoDB 6.0+** as the data store (via `pymongo`)
- **Pydantic v2** for request/response validation
- **Uvicorn** as the ASGI server

All code lives in a single `app/main.py` file.

### Prerequisites
1. **Python 3.11** or newer installed.
2. **MongoDB 6.0** (local or remote). If running locally, the default URI `mongodb://localhost:27017` and database name `hrone` are used.
3. **Git** (optional, for cloning the repo).

### Setup
```bash
# Clone the repository (or copy the generated files into a folder)
git clone <repo‑url> hrone-api
cd hrone-api

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate   # on Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### Environment Variables
Create a `.env` file in the project root (or set the variables in your shell) with:
```
MONGO_URI=mongodb://localhost:27017   # or your remote connection string
MONGO_DB=hrone                        # database name
```
The application falls back to the above defaults if the variables are missing.

### Running the Server
```bash
uvicorn app.main:app --port 8000
```
The API will be available at `http://localhost:8000`.

### Health Check
```
GET /health
```
Returns `{"status": "ok"}` (HTTP 200) when MongoDB is reachable, otherwise HTTP 503.

### API Summary
| Category | Endpoint | Method | Description |
|----------|----------|--------|-------------|
| **Employees** | `/employees` | `POST` | Create a new employee (validates `emp_code`, email, etc.) |
| | `/employees` | `GET` | List employees with optional department filter & pagination |
| **Attendance** | `/attendance/punch-in` | `POST` | Record punch‑in (race‑safe, unique per employee/date) |
| | `/attendance/punch-out` | `POST` | Record punch‑out, compute late/overtime/work‑hours/half‑day |
| | `/attendance` | `GET` | List attendance records with filters & pagination |
| | `/attendance/{emp_code}/{date}` | `PATCH` | Manual regularisation – updates fields, recomputes derived values, appends history |
| **Analytics** | `/analytics/employees/{emp_code}/monthly` | `GET` | Monthly summary for a single employee |
| | `/analytics/departments/summary` | `GET` | Department‑wide summary for a month (headcount includes zero‑log employees) |
| | `/analytics/leaderboard/late` | `GET` | Late‑arrival leaderboard using standard competition ranking |
| | `/analytics/departments/{department}/trend` | `GET` | Daily trend for a department with gap‑filling and 7‑day moving average |
| **Admin** | `/admin/explain/{endpoint}` | `GET` | Returns raw MongoDB explain output for the selected internal pipeline |

### Testing
You can run a quick sanity check with `curl` or any HTTP client:
```bash
# Health check
curl http://localhost:8000/health

# Create an employee (example)
curl -X POST http://localhost:8000/employees \
  -H "Content-Type: application/json" \
  -d '{"emp_code":"EMP0001","name":"Alice","email":"alice@example.com","department":"Engineering","joined_on":"2023-01-15"}'
```
For a full test suite, add your own `pytest` files in a `tests/` directory and run `pytest`.

### Documentation
FastAPI automatically generates OpenAPI docs. Visit:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

### License
This project is provided for educational purposes. Replace this section with your own licensing information if needed.
