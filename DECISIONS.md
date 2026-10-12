# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (uniqueness and the join key), `(department, emp_code)` (filtered, sorted list), `joined_on` (headcount). `attendance_logs`: unique `(emp_code, date)` (one record per day, punch-in race guard, per-employee range scans), `(date desc, emp_code)` (list order, month scans for the leaderboard), `(status, date desc, emp_code)` (status filter), `(emp_code, punch_in desc)` (punch-out lookup). Rejected: an index on `late_minutes`; the month range already narrows the scan, and it would only slow writes.
2. **Punch-in race.** Both requests compute the same date and call `insert_one`. The unique `(emp_code, date)` index lets exactly one succeed; the other gets `DuplicateKeyError` and I return 409. There is no read-then-insert.
3. **Ties.** Rank comes from `$setWindowFields` with `$rank`, so ties share a rank (1, 2, 2, 4). `limit` filters on `rank <= limit` afterwards, so every employee tied at the cutoff is returned, even if that is more than `limit` rows.
4. **Headcount.** The summary pipeline starts from `employees` (joined on or before month end) and then `$lookup`s logs, so someone with no logs still has a row that the `$group` counts.
5. **At 100x.** I would stop scanning raw logs for analytics: keep a daily per-department roll-up updated on punch and correction, and query that instead.
