# DECISIONS.md

1. **Indexes.**
   - `employees`: `{emp_code: 1}` (unique, guarantees employee uniqueness and enables fast lookups); `{department: 1, joined_on: 1}` and `{joined_on: 1}` (serves department summary filtering and headcount aggregations via IXSCAN).
   - `attendance_logs`: `{emp_code: 1, date: 1}` (unique, guarantees at most one record per employee-day and supports atomic punch-in/out and monthly analytics); `{date: -1, emp_code: 1}` (serves attendance listing sort/pagination without in-memory sort); `{date: 1, late_minutes: 1}` (serves leaderboard and trend date-range scans); `{emp_code: 1, punch_in: -1}` (serves fast punch-out matching).
   - *Considered & rejected*: `{status: 1, date: 1}` for status filtering was rejected because `date` and `emp_code` indexes already offer high selectivity without index overhead.

2. **Punch-in race.**
   Both requests compute their payload and execute `insert_one`. MongoDB's unique index on `(emp_code, date)` allows only the first insert to succeed (returning 201). The second request encounters a `DuplicateKeyError`, which our exception handler catches and maps directly to HTTP 409 Conflict.

3. **Ties.**
   Using MongoDB's `$setWindowFields` with `$rank`, ranking follows standard competition ranking (1, 2, 2, 4). The `limit` filter (`rank <= limit`) evaluates after ranking. When a tie occurs at the cutoff, all tied employees are returned (yielding more than `limit` rows), sorted by `total_late_minutes` descending and `emp_code` ascending.

4. **Headcount.**
   The aggregation pipeline starts on the `employees` collection (matching `joined_on <= month_end`), rather than `attendance_logs`. Headcount is computed via `{"$sum": 1}` across employees. Attendance records are joined via `$lookup`. Employees with empty log arrays contribute 1 to headcount and 0 to attendance sums, ensuring zero-log employees are counted.

5. **One thing you would change for 100x data.**
   Using MongoDB to compute and maintain pre-aggregated daily/monthly statisctics so that we do not have to query raw logs and instead query the precomputed statistcs.
