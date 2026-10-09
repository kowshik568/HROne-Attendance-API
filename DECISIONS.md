# DECISIONS.md

## 1. Index Strategy

| Collection | Index(es) | Purpose |
|-----------|-----------|---------|
| **employees** | `{'emp_code': 1}` (unique) | Guarantees uniqueness of employee codes and fast look‑ups for all employee‑related endpoints (`POST /employees`, `GET /employees`). |
| **attendance** | `{'emp_code': 1, 'date': 1}` (unique) | Enforces the **one‑record‑per‑day** rule and enables efficient filtering by employee and date range for punch‑in/out, regularisation and analytics. |
| | `{'date': 1}` | Supports date‑range queries used by analytics pipelines and the `/attendance` list endpoint. |
| | `{'status': 1}` | Optimises filtering by `status` in the attendance list and leaderboard aggregation. |

All indexes are created idempotently at application startup (`startup_indexes`). They are simple B‑tree indexes, which MongoDB can build quickly even on large collections.

## 2. Punch‑In Race Condition Handling

The specification requires **atomic** handling so that concurrent requests for the same employee/date result in exactly one successful record and the rest receive **409 Conflict**.

Implementation details:
1. A **compound unique index** on `emp_code` + `date` guarantees database‑level uniqueness.
2. The `POST /attendance/punch-in` endpoint attempts a regular `insert_one`.
3. If another request has already inserted a document with the same key, MongoDB raises `DuplicateKeyError`.
4. The API catches this exception and returns `HTTP 409` with a clear message.

Because the uniqueness check occurs inside MongoDB's storage engine, the solution is safe against race conditions even under high concurrency.

## 3. Leaderboard Tie‑Breaking (Standard Competition Ranking)

The leaderboard must use **standard competition ranking** (`1, 2, 2, 4`). This is achieved with the aggregation stage `$setWindowFields` and the `$rank` accumulator:
```javascript
{ "$setWindowFields": {
    "output": {
        "rank": { "$rank": {} }
    }
}}
```
MongoDB assigns the same rank to identical `total_late_minutes` values and skips subsequent ranks accordingly, matching the required ranking scheme. The final `$match` limits the result set **after** ranking, ensuring the limit is applied to the ranked list, not to an un‑ranked subset.

## 4. Headcount Including Zero‑Log Employees

Headcount for a department/month must count **all employees whose `joined_on` is on or before the period end**, even if they have no attendance records in that month.

The aggregation pipeline for department summary:
1. `$lookup` from `employees` brings employee documents into the attendance stream.
2. `$group` uses `$addToSet` on `emp.emp_code` to collect unique employee codes that satisfy the `joined_on <= period_end` condition (implemented via a `$match` inside the `$lookup` pipeline).
3. `$project` then computes the size of that set (`$size`) as the headcount.

Because the `$lookup` is performed on the **attendance** collection, employees without any attendance entries will still be counted via the `$lookup`‑pipeline filter, satisfying the requirement.

## 5. Scaling to 100× Data Volume

Assuming the system must handle **~10 M employee** records and **~10 M attendance** records (100× the baseline), the following measures ensure performance and maintainability:

| Concern | Mitigation |
|---------|------------|
| **Index Size & RAM** | Keep indexes narrow (single field or small compound keys). Allocate sufficient RAM to hold the most‑used indexes (`emp_code+date`, `date`, `status`). Use the `usePowerOf2Sizes` index option if necessary. |
| **Write Throughput** | Bulk inserts for attendance can be sharded across multiple Mongos routers (if using a sharded cluster). The unique compound index supports high‑concurrency inserts without full collection scans. |
| **Read Patterns** | All analytics pipelines start with `$match` on date ranges and employee codes, allowing MongoDB to use the `date` or `emp_code+date` indexes for selective scans. `$lookup` uses indexed `emp_code`. |
| **Aggregation Efficiency** | Use `$facet` only when necessary; most pipelines rely on `$group`, `$lookup`, `$densify`, and `$setWindowFields`, which operate on indexed streams. The `$densify` stage benefits from an index on `date`. |
| **Operational Practices** | Periodic index rebuilding (`compact`), archiving old attendance data into a separate collection (e.g., `attendance_archive`), and TTL indexes for temporary logs. Monitoring via MongoDB Cloud Manager to detect hot shards or index miss‑hits. |
| **Testing & CI** | Add load‑testing scripts (e.g., Locust) to simulate 10 k concurrent punch‑ins/punch‑outs and verify that latency stays sub‑second. |

These strategies together ensure the API remains responsive and cost‑effective even when data grows an order of magnitude.

---
*All decisions are documented to aid reviewers and future maintainers.*
