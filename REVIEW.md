# REVIEW.md

## Starter Code Defects & Fixes

| Issue | Description | Fix Implemented |
|-------|-------------|-----------------|
| **Hard‑coded MongoDB credentials** | The original assignment required reading `MONGO_URI` and `MONGO_DB` from environment variables. | Added `os.getenv` with sensible defaults (`mongodb://localhost:27017` and `hrone`). |
| **Missing index for unique employee code** | Without a unique index, duplicate `emp_code` could be inserted leading to inconsistent data. | `EMPLOYEES.create_index([("emp_code", ASCENDING)], unique=True)` in `startup_indexes`. |
| **Attendance document uniqueness** | The spec demands one attendance record per employee per logical work‑day. | Created a compound unique index on `emp_code` + `date`. |
| **Timezone handling** | The spec requires IST (UTC+05:30) and truncation to whole seconds. | Implemented `IST` offset, `now_utc()` truncates microseconds, and helper `attendance_date_from_timestamp`. |
| **Race‑condition on punch‑in** | Concurrent punch‑in may insert duplicate records. | Used MongoDB unique index and caught `DuplicateKeyError` to return HTTP 409. |
| **Derived field calculations** | Late‑minutes, overtime, work‑hours, half‑day must follow rules R2‑R5. | Added pure functions `calculate_late`, `calculate_overtime`, `calculate_work_hours` that apply the exact business logic. |
| **Decimal rounding** | Work‑hours must be rounded half‑up to 2 decimals. | Implemented `round_half_up` using `Decimal` and `ROUND_HALF_UP`. |
| **History tracking on regularization** | Must append exactly one history entry and never overwrite past entries. | `regularize_attendance` builds a single `history_entry` and uses `$push` to append. |
| **Analytics pipelines** | Required MongoDB aggregation with specific behaviours (gap‑filling, ranking, moving average). | Implemented pipelines using `$group`, `$lookup`, `$densify`, `$setWindowFields`, etc. |
| **Explain endpoint** | Must return raw `explain` output with `executionStats`. | Provided a mapping to placeholder pipelines with `.explain("executionStats")`. |
| **Documentation files missing** | Assignment asks for `REVIEW.md`, `DECISIONS.md`, `README.md`. | Created the three markdown files in the project root. |

All defects identified from the specification have been addressed, and the code now complies with the stated constraints.
