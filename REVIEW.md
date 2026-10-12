# REVIEW.md

Defects found in the starter `app/main.py`. Line numbers refer to the original starter.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `MongoClient(...)` (l.22) | No `tz_aware=True`, so every datetime comes back naive and the code silently mixes naive and aware values. | `punch_in - ist_time` raises `TypeError`; epoch conversion depends on the server's time zone. | `tz_aware=True`; all instants handled as aware UTC and converted to IST only for rules R1-R3. |
| 2 | `health()` (l.72) | Always returns 200 and never pings MongoDB. | Stop MongoDB, `GET /health` still says `ok`; the grader starts testing too early. | Pings MongoDB; any `PyMongoError` gives 503. |
| 3 | `create_employee` (l.78-85) | Check-then-insert race and no unique index, so parallel creates can both get 201 and store duplicates. | 10 parallel `POST /employees` with the same `emp_code`. | Unique index on `emp_code`; insert and catch `DuplicateKeyError` -> 409. |
| 4 | `EmployeeIn` (l.53-60) | No validation: any string as `emp_code`, email, shift or date; `shift_start == shift_end` accepted; `joined_on="2026-02-30"` accepted. | `POST` with `emp_code="x"`, `shift_start="9:5"` returns 201 instead of 422. | Field patterns from the contract, real date check, `shift_start != shift_end` validator. |
| 5 | `create_employee` (l.82-85) | `created_at = datetime.now()` is naive local time and is returned as a datetime/ISO string, not epoch ms. | Response `created_at` is a string; value shifts with server TZ. | UTC time, truncated to seconds, serialised as epoch ms. |
| 6 | `list_employees` (l.93) | `skip = page * page_size` skips the first page (page 1 returns the second page). | `?page=1` never returns the first employee. | `skip = (page - 1) * page_size`. |
| 7 | `list_employees` (l.94) | `total` counts the whole collection, ignoring the `department` filter; no sort. | `?department=HR` returns `total` = all employees; order is arbitrary. | `count_documents(q)` with the same filter; sort by `emp_code`. |
| 8 | `list_employees` / `list_attendance` params | `page` and `page_size` unchecked: `page_size=0` means `limit(0)` = no limit, negative values raise a 500, `page=0` gives negative offsets/slices, huge sizes are allowed. | `?page=0` returns wrong rows or errors. | `Query(ge=1)` and `le=100` give 422. |
| 9 | `punch_in` (l.101-102) | Unknown employee: `emp` is `None`, `emp["shift_start"]` raises `TypeError`, so a 500 instead of 404. | `POST` with `emp_code="NOPE"`. | Explicit 404. |
| 10 | `punch_in` (l.102-103) | `datetime.fromtimestamp(ms/1000)` uses the server's local zone and keeps milliseconds; `date` is the local calendar date, not the IST date; `if body.punched_at` treats 0 as "missing". | Server in UTC: punch at 00:30 IST gets the previous day's `date`. | Instants built as aware UTC, truncated to whole seconds (R1); date from `attendance_date()` in IST. |
| 11 | `punch_in` (l.103) | No overnight-shift rule: for 22:00-06:00 a 02:00 punch-in is stored on the wrong date. | Overnight employee punches in at 02:00 IST. | `attendance_date()` implements R1; late is measured against that day's shift start. |
| 12 | `punch_in` (l.104-118) | Check-then-insert race, no unique index: two simultaneous punch-ins both get 201 and two documents are stored. | Fire 10 identical punch-ins in parallel. | Unique index `(emp_code, date)`; insert and catch `DuplicateKeyError` -> 409. |
| 13 | `PunchInIn` (l.63-66) | `status` is a free string (ABSENT, LEAVE, garbage accepted); `punched_at: int` coerces `"123"` and `1.0` and accepts seconds, so a bad timestamp is not a 422. | `{"status":"ABSENT"}` or `{"punched_at": 1783312500}` returns 201. | `Literal` of the three presence statuses; strict int in range 1e11..4.1e12. |
| 14 | `punch_in` response (l.119-121) | Returns an `id` (the ObjectId, which must not be exposed) and raw datetimes instead of epoch ms. | Response contains `id` and ISO strings. | `serialize_attendance()` gives exactly the contract fields. |
| 15 | `compute_late_minutes` (l.33-37) | Uses the punch's own date and clock, ignores IST and overnight shifts; floors to minutes *before* the `> 10` test, so 09:40:01 gives 0 instead of 10; milliseconds not truncated. | 09:40:01 with shift 09:30: expected 10, got 0. | Compare whole seconds against 600 first (`> 600`), then floor; shift start taken in IST on the record date. |
| 16 | `compute_work_hours` (l.40-41) | Python `round()` is banker's rounding on a binary float. | 18 s = 0.005 h; `round(2.675,2)` style errors; R4 says half-up. | Exact `Decimal(seconds)/3600` quantised `ROUND_HALF_UP`. |
| 17 | `compute_overtime` (l.44-47) | No 30-minute minimum (R3), ignores overnight (shift end is next day), and is never called by any endpoint. | 18:40 punch-out gives 10 overtime minutes instead of 0. | Rewritten: floor minutes after shift end, counted only if >= 30; end is on the next day for overnight shifts. |
| 18 | `list_attendance` (l.144-150) | Loads the whole collection into memory and sorts/paginates in Python (unusable at 100k docs); `total` from `len(docs)`. | 100k records: seconds per request and high memory. | Query + sort + skip/limit in MongoDB; `count_documents`. |
| 19 | `list_attendance` (l.145) | Sorts by `date` only (no `emp_code` tie-break); `page=0` produces negative slice indexes. | Same-date rows come back in arbitrary order between calls. | Sort `(date desc, emp_code asc)` backed by an index. |
| 20 | `list_attendance` (l.133-150) | Dates and `status` unvalidated; `date_from > date_to` returns an empty list instead of 422; the response leaks `id`, raw datetimes and misses `history`/`half_day` for legacy documents. | `?date_from=2026-07-09&date_to=2026-07-01`; legacy sample doc 9. | Date parsing, ordering check and `Status` enum give 422; serializer fills defaults (`history=[]`, `half_day=false`). |
| 21 | whole file | No indexes at all (every filter is a collection scan) and no `explain` support. | `explain` shows COLLSCAN. | Seven indexes created idempotently at startup (see DECISIONS.md). |

**Looked at and judged fine**
- `load_dotenv()` does not override real environment variables (default `override=False`), so the "env wins" requirement already holds.
- Module-level `MongoClient` is lazy and thread-safe, which is fine for sync endpoints running in FastAPI's threadpool.
- String comparison on `YYYY-MM-DD` for `date_from`/`date_to` is correct because the format sorts lexicographically.
- `doc.pop("_id")` after `insert_one` is needed and correct, since PyMongo adds `_id` to the dict.
