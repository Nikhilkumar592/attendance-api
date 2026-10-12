"""Analytics pipelines vs an independent pure-Python reference on awkward data."""
import random
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import pytest

PRES = {"PRESENT", "WFH", "ON_DUTY"}


def q(x, places):
    return float(Decimal(str(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def seed(raw, seed_no=7):
    rnd = random.Random(seed_no)
    emps = [
        ("EMP0001", "Eng", "2026-01-05"), ("EMP0002", "Eng", "2026-01-05"), ("EMP0003", "Eng", "2026-07-15"),  # mid-month joiner
        ("EMP0004", "Eng", "2026-08-01"),                                                                      # joins after July
        ("EMP0005", "Eng", "2026-01-05"),                                                                      # no logs at all
        ("EMP0006", "HR", "2026-01-05"), ("EMP0007", "HR", "2026-01-05"), ("EMP0008", "HR", "2026-01-05"),
        ("EMP0009", "Ops", "2026-12-01"),                                                                      # dept with nobody joined yet
    ]
    raw.employees.insert_many([{"emp_code": c, "name": f"Name {c}", "email": "a@b.co", "department": d,
                                "shift_start": "09:30", "shift_end": "18:30", "joined_on": j,
                                "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)} for c, d, j in emps])
    logs = []
    for c, d, j in emps:
        if c in ("EMP0005", "EMP0009"):
            continue
        day = max(date(2026, 7, 1), date.fromisoformat(j)) if j < "2026-08-01" else date(2026, 8, 3)
        while day <= date(2026, 8, 9):
            if rnd.random() < 0.8:  # missing days = no record
                st = rnd.choice(["PRESENT", "PRESENT", "PRESENT", "WFH", "ON_DUTY", "LEAVE", "ABSENT"])
                rec = {"emp_code": c, "date": day.isoformat(), "status": st, "punch_in": None, "punch_out": None,
                       "work_hours": None, "late_minutes": 0, "overtime_minutes": 0}
                if rnd.random() < 0.9:
                    rec["half_day"] = False  # sometimes missing (legacy)
                if rnd.random() < 0.9:
                    rec["history"] = []
                if st in PRES:
                    pin = datetime(day.year, day.month, day.day, 4, 0, tzinfo=timezone.utc)
                    rec["punch_in"] = pin
                    rec["late_minutes"] = rnd.choice([0, 0, 0, 12, 25, 25, 40])
                    if rnd.random() < 0.85:
                        h = round(rnd.uniform(2, 10), 2)
                        rec["punch_out"] = pin + timedelta(hours=h)
                        rec["work_hours"] = h
                        rec["overtime_minutes"] = rnd.choice([0, 0, 35, 70])
                        rec["half_day"] = h < 4.5
                logs.append(rec)
            day += timedelta(days=1)
    # orphan log whose employee does not exist: ignored by the leaderboard
    logs.append({"emp_code": "EMP9999", "date": "2026-07-08", "status": "PRESENT", "late_minutes": 500,
                 "punch_in": datetime(2026, 7, 8, 5, tzinfo=timezone.utc)})
    raw.attendance_logs.insert_many(logs)
    return emps, logs


def ref_working_days(month, joined):
    y, m = map(int, month.split("-"))
    d = date(y, m, 1)
    n = 0
    while d.month == m:
        if d.weekday() < 5 and d.isoformat() >= joined:
            n += 1
        d += timedelta(days=1)
    return n


def pv(r):  # present value on any day
    return (0.5 if r.get("half_day") else 1) if r["status"] in PRES else 0


def is_wd(ds):
    return date.fromisoformat(ds).weekday() < 5


def test_employee_monthly(api):
    c, raw = api
    emps, logs = seed(raw)
    for month in ("2026-07", "2026-08", "2026-06", "2026-12"):
        for code, dept, joined in emps:
            rs = [r for r in logs if r["emp_code"] == code and r["date"].startswith(month)]
            wd = ref_working_days(month, joined)
            present = sum(pv(r) for r in rs if is_wd(r["date"]))
            exp = {"emp_code": code, "month": month, "working_days": wd, "present_days": present,
                   "leave_days": sum(r["status"] == "LEAVE" for r in rs),
                   "late_count": sum((r.get("late_minutes") or 0) > 0 for r in rs),
                   "total_late_minutes": sum(r.get("late_minutes") or 0 for r in rs),
                   "total_overtime_minutes": sum(r.get("overtime_minutes") or 0 for r in rs),
                   "attendance_pct": q(Decimal(str(present)) / wd * 100, 2) if wd else None}
            got = c.get(f"/analytics/employees/{code}/monthly", params={"month": month})
            assert got.status_code == 200
            assert got.json() == exp, (code, month)
    assert c.get("/analytics/employees/NOPE/monthly", params={"month": "2026-07"}).status_code == 404
    for bad in ("2026-13", "2026-7", "x", "0000-01"):
        assert c.get("/analytics/employees/EMP0001/monthly", params={"month": bad}).status_code == 422
    assert c.get("/analytics/employees/EMP0001/monthly").status_code == 422


def test_department_summary(api):
    c, raw = api
    emps, logs = seed(raw)
    for month in ("2026-07", "2026-08", "2026-06"):
        last = {"2026-07": "2026-07-31", "2026-08": "2026-08-31", "2026-06": "2026-06-30"}[month]
        exp = []
        for dept in sorted({d for _, d, _ in emps}):
            people = [c_ for c_, d, j in emps if d == dept and j <= last]
            if not people:
                continue
            rs = [r for r in logs if r["emp_code"] in people and r["date"].startswith(month)]
            wh = [r["work_hours"] for r in rs if r["status"] in PRES and r.get("work_hours") is not None]
            exp.append({"department": dept, "headcount": len(people),
                        "present_days": sum(pv(r) for r in rs if is_wd(r["date"])),
                        "avg_work_hours": q(Decimal(sum(Decimal(str(x)) for x in wh)) / len(wh), 2) if wh else None,
                        "late_count": sum((r.get("late_minutes") or 0) > 0 for r in rs),
                        "total_late_minutes": sum(r.get("late_minutes") or 0 for r in rs),
                        "leave_count": sum(r["status"] == "LEAVE" for r in rs),
                        "on_duty_count": sum(r["status"] == "ON_DUTY" for r in rs)})
        got = c.get("/analytics/departments/summary", params={"month": month}).json()
        assert got == {"month": month, "items": exp}, month
    # department filter, and a department nobody has joined yet is omitted
    got = c.get("/analytics/departments/summary", params={"month": "2026-07", "department": "HR"}).json()
    assert [i["department"] for i in got["items"]] == ["HR"] and got["items"][0]["headcount"] == 3
    got = c.get("/analytics/departments/summary", params={"month": "2026-07"}).json()
    assert "Ops" not in [i["department"] for i in got["items"]]
    # Eng headcount in July: EMP0001,2,3(joined 07-15),5(no logs) = 4; EMP0004 joins in August
    assert next(i for i in got["items"] if i["department"] == "Eng")["headcount"] == 4


def test_leaderboard_ties_and_limit(api):
    c, raw = api
    for code, dept in (("EMP0001", "A"), ("EMP0002", "A"), ("EMP0003", "B"), ("EMP0004", "B"), ("EMP0005", "A"), ("EMP0006", "B")):
        raw.employees.insert_one({"emp_code": code, "name": code, "department": dept, "email": "a@b.co",
                                  "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"})
    totals = {"EMP0001": 100, "EMP0002": 80, "EMP0003": 80, "EMP0004": 80, "EMP0005": 10, "EMP0006": 0}
    for code, t in totals.items():
        raw.attendance_logs.insert_one({"emp_code": code, "date": "2026-07-06", "status": "PRESENT", "late_minutes": t})
    raw.attendance_logs.insert_one({"emp_code": "EMP0005", "date": "2026-08-01", "status": "PRESENT", "late_minutes": 999})  # other month
    raw.attendance_logs.insert_one({"emp_code": "GHOST", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 999})
    g = lambda **p: c.get("/analytics/leaderboard/late", params={"month": "2026-07", **p}).json()["items"]
    rows = g(limit=10)
    assert [(r["rank"], r["emp_code"], r["total_late_minutes"]) for r in rows] == [
        (1, "EMP0001", 100), (2, "EMP0002", 80), (2, "EMP0003", 80), (2, "EMP0004", 80), (5, "EMP0005", 10)]
    assert rows[0] == {"rank": 1, "emp_code": "EMP0001", "name": "EMP0001", "department": "A",
                       "total_late_minutes": 100, "late_count": 1}
    # limit=2 -> everyone with rank <= 2, i.e. all three tied at rank 2
    assert [r["emp_code"] for r in g(limit=2)] == ["EMP0001", "EMP0002", "EMP0003", "EMP0004"]
    assert [r["emp_code"] for r in g(limit=1)] == ["EMP0001"]
    # ranking inside a department only
    assert [(r["rank"], r["emp_code"]) for r in g(department="B")] == [(1, "EMP0003"), (1, "EMP0004")]
    assert [(r["rank"], r["emp_code"]) for r in g(department="A")] == [(1, "EMP0001"), (2, "EMP0002"), (3, "EMP0005")]
    assert c.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 51}).status_code == 422
    assert c.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 0}).status_code == 422
    assert c.get("/analytics/leaderboard/late", params={"month": "2026-09"}).json()["items"] == []


def test_trend(api):
    c, raw = api
    emps, logs = seed(raw)
    frm, to = date(2026, 6, 25), date(2026, 8, 12)
    for dept in ("Eng", "HR"):
        people = [(c_, j) for c_, d, j in emps if d == dept]
        rows, rates = [], []
        d = frm
        while d <= to:
            ds = d.isoformat()
            hc = sum(j <= ds for _, j in people)
            rs = [r for r in logs if r["emp_code"] in {p for p, _ in people} and r["date"] == ds]
            present = sum(pv(r) for r in rs)
            wd = d.weekday() < 5
            rate = q(Decimal(str(present)) / hc, 4) if wd and hc > 0 else None
            rates.append(rate)
            window = [x for x in rates[max(0, len(rates) - 7):] if x is not None]
            rows.append({"date": ds, "is_working_day": wd, "headcount": hc, "present_count": present,
                         "late_count": sum((r.get("late_minutes") or 0) > 0 for r in rs), "attendance_rate": rate,
                         "moving_avg_7d": q(Decimal(sum(Decimal(str(x)) for x in window)) / len(window), 4) if window else None})
            d += timedelta(days=1)
        got = c.get(f"/analytics/departments/{dept}/trend", params={"from": frm.isoformat(), "to": to.isoformat()})
        assert got.status_code == 200, got.text
        assert got.json() == {"department": dept, "items": rows}, dept
    # exactly one row per day, weekend rows have null rate
    items = got.json()["items"]
    assert len(items) == (to - frm).days + 1
    assert all(i["attendance_rate"] is None for i in items if not i["is_working_day"])


def test_trend_no_records_and_errors(api):
    c, raw = api
    raw.employees.insert_one({"emp_code": "EMP0001", "name": "x", "department": "Eng", "email": "a@b.co",
                              "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-07-03"})
    r = c.get("/analytics/departments/Eng/trend", params={"from": "2026-07-01", "to": "2026-07-07"}).json()["items"]
    assert [(i["date"], i["headcount"], i["attendance_rate"], i["moving_avg_7d"]) for i in r] == [
        ("2026-07-01", 0, None, None), ("2026-07-02", 0, None, None), ("2026-07-03", 1, 0.0, 0.0),
        ("2026-07-04", 1, None, 0.0), ("2026-07-05", 1, None, 0.0), ("2026-07-06", 1, 0.0, 0.0), ("2026-07-07", 1, 0.0, 0.0)]
    t = lambda **p: c.get("/analytics/departments/Eng/trend", params=p).status_code
    assert c.get("/analytics/departments/Nope/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404
    assert t(**{"from": "2026-07-05", "to": "2026-07-01"}) == 422
    assert t(**{"from": "2026-01-01", "to": "2026-04-03"}) == 422   # 93 days
    assert t(**{"from": "2026-01-01", "to": "2026-04-02"}) == 200   # 92 days
    assert t(**{"from": "2026-01-01"}) == 422
    assert t(**{"from": "bad", "to": "2026-01-01"}) == 422
