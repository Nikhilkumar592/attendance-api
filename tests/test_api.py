from datetime import datetime

from app.main import IST


def ms(s):
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST).timestamp() * 1000)


def emp(code="EMP0001", **kw):
    d = {"emp_code": code, "name": "N " + code, "email": f"{code}@x.com", "department": "Eng", "joined_on": "2026-01-05"}
    d.update(kw)
    return d


def test_health(api):
    c, _ = api
    assert c.get("/health").json() == {"status": "ok"}


def test_employee_create_validation_and_list(api):
    c, _ = api
    r = c.post("/employees", json=emp())
    assert r.status_code == 201
    b = r.json()
    assert "_id" not in b and isinstance(b["created_at"], int) and b["shift_start"] == "09:30"
    assert c.post("/employees", json=emp()).status_code == 409
    for bad in (emp("E1"), emp("EMP0002", shift_start="09:00", shift_end="09:00"), emp("EMP0002", joined_on="2026-02-30"),
                emp("EMP0002", shift_start="9:00"), emp("EMP0002", email="nope"), {"emp_code": "EMP0002"}):
        assert c.post("/employees", json=bad).status_code == 422, bad
    for i in range(2, 8):
        c.post("/employees", json=emp(f"EMP000{i}", department="HR" if i % 2 else "Eng"))
    r = c.get("/employees", params={"page_size": 2, "page": 2}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0003", "EMP0004"] and r["total"] == 7
    r = c.get("/employees", params={"department": "HR"}).json()
    assert r["total"] == 3 and [e["emp_code"] for e in r["items"]] == ["EMP0003", "EMP0005", "EMP0007"]
    assert c.get("/employees", params={"page": 0}).status_code == 422
    assert c.get("/employees", params={"page_size": 101}).status_code == 422


def test_punch_in_rules(api):
    c, _ = api
    c.post("/employees", json=emp())
    assert c.post("/attendance/punch-in", json={"emp_code": "NOPE"}).status_code == 404
    r = c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06 09:40:01") + 900})
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["date"] == "2026-07-06" and b["late_minutes"] == 10 and b["history"] == []
    assert b["punch_in"] == ms("2026-07-06 09:40:01")  # truncated to the second
    assert b["punch_out"] is None and b["work_hours"] is None and b["half_day"] is False
    assert c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06 12:00:00")}).status_code == 409
    for bad in ({"status": "ABSENT"}, {"status": "LEAVE"}, {"status": "x"}, {"punched_at": 1783312500},
                {"punched_at": 1783312500000.5}, {"punched_at": "1783312500000"}, {"punched_at": True}, {"punched_at": 99999999999}):
        r = c.post("/attendance/punch-in", json={"emp_code": "EMP0001", **bad})
        assert r.status_code == 422, bad


def test_overnight_punch_in_and_out(api):
    c, _ = api
    c.post("/employees", json=emp("EMP0005", shift_start="22:00", shift_end="06:00"))
    r = c.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms("2026-07-06 21:55:00")})
    assert r.json()["date"] == "2026-07-06" and r.json()["late_minutes"] == 0
    r = c.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms("2026-07-07 06:40:00")})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["work_hours"] == 8.75 and b["overtime_minutes"] == 40 and b["half_day"] is False
    # early-morning punch-in belongs to previous day's shift
    r = c.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms("2026-07-08 02:00:00")})
    assert r.json()["date"] == "2026-07-07"


def test_punch_out_errors(api):
    c, _ = api
    c.post("/employees", json=emp())
    assert c.post("/attendance/punch-out", json={"emp_code": "NOPE"}).status_code == 404
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06 18:00:00")}).status_code == 404
    c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06 09:00:00")})
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06 09:00:00")}).status_code == 422
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-07 09:00:01")}).status_code == 422
    r = c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06 13:00:00")})
    assert r.status_code == 200 and r.json()["work_hours"] == 4.0 and r.json()["half_day"] is True
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06 14:00:00")}).status_code == 409


def test_regularize(api):
    c, raw = api
    c.post("/employees", json=emp())
    c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-07 10:05:00")})
    c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-07 19:10:00")})
    url = "/attendance/EMP0001/2026-07-07"
    base = {"reason": "biometric glitch", "regularized_by": "hr.admin"}
    r = c.patch(url, json={**base, "punch_in": ms("2026-07-07 09:28:00")})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["late_minutes"] == 0 and b["work_hours"] == 9.7 and b["overtime_minutes"] == 40
    h = b["history"]
    assert len(h) == 1 and h[0]["by"] == "hr.admin" and isinstance(h[0]["at"], int)
    assert h[0]["changes"]["punch_in"] == {"from": ms("2026-07-07 10:05:00"), "to": ms("2026-07-07 09:28:00")}
    assert h[0]["changes"]["late_minutes"] == {"from": 35, "to": 0}
    assert "overtime_minutes" not in h[0]["changes"] and "status" not in h[0]["changes"]
    # second correction appends, never overwrites
    r = c.patch(url, json={**base, "status": "LEAVE"})
    assert r.status_code == 200 and len(r.json()["history"]) == 2
    assert r.json()["punch_in"] is None and r.json()["work_hours"] is None and r.json()["half_day"] is False
    # errors
    assert c.patch(url, json={**base, "status": "LEAVE"}).status_code == 422             # changes nothing
    assert c.patch(url, json={**base, "status": "LEAVE", "punch_in": ms("2026-07-07 09:00:00")}).status_code == 422
    assert c.patch(url, json={**base, "status": "PRESENT"}).status_code == 422           # needs punch_in
    assert c.patch(url, json={**base, "status": "WFH", "punch_in": ms("2026-07-08 09:00:00")}).status_code == 422  # other date
    assert c.patch(url, json={**base, "status": "WFH", "punch_in": ms("2026-07-07 09:00:00"), "punch_out": ms("2026-07-07 08:00:00")}).status_code == 422
    assert c.patch(url, json={**base, "reason": "abc"}).status_code == 422
    assert c.patch(url, json={"status": "LEAVE"}).status_code == 422
    assert c.patch("/attendance/EMP0001/2026-07-09", json={**base, "status": "LEAVE"}).status_code == 404
    assert c.patch("/attendance/NOPE/2026-07-07", json={**base, "status": "LEAVE"}).status_code == 404
    assert c.patch("/attendance/EMP0001/2026-13-07", json={**base, "status": "LEAVE"}).status_code == 422
    r = c.patch(url, json={**base, "status": "WFH", "punch_in": ms("2026-07-07 09:00:00"), "punch_out": ms("2026-07-07 12:00:00")})
    assert r.status_code == 200 and r.json()["half_day"] is True and len(r.json()["history"]) == 3
    # derived fields in the body are ignored
    r = c.patch(url, json={**base, "punch_out": ms("2026-07-07 13:00:00"), "work_hours": 99, "late_minutes": 99})
    assert r.status_code == 200 and r.json()["work_hours"] == 4.0


def test_legacy_record_serialization(api):
    c, raw = api
    from datetime import timezone
    raw.attendance_logs.insert_one({"emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
                                    "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=timezone.utc),
                                    "punch_out": datetime(2026, 7, 14, 13, 1, tzinfo=timezone.utc),
                                    "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0})
    b = c.get("/attendance").json()["items"][0]
    assert b["history"] == [] and b["half_day"] is False and "id" not in b and "_id" not in b


def test_attendance_list(api):
    c, _ = api
    for i in (1, 2, 3):
        c.post("/employees", json=emp(f"EMP000{i}"))
    for day in ("2026-07-06", "2026-07-07", "2026-07-08"):
        for i in (3, 1, 2):
            c.post("/attendance/punch-in", json={"emp_code": f"EMP000{i}", "punched_at": ms(f"{day} 09:00:00"),
                                                 "status": "WFH" if day.endswith("07") else "PRESENT"})
    r = c.get("/attendance", params={"page_size": 4}).json()
    assert r["total"] == 9
    assert [(x["date"], x["emp_code"]) for x in r["items"]] == [
        ("2026-07-08", "EMP0001"), ("2026-07-08", "EMP0002"), ("2026-07-08", "EMP0003"), ("2026-07-07", "EMP0001")]
    r = c.get("/attendance", params={"emp_code": "EMP0002", "date_from": "2026-07-07", "date_to": "2026-07-07"}).json()
    assert r["total"] == 1 and r["items"][0]["status"] == "WFH"
    assert c.get("/attendance", params={"status": "WFH"}).json()["total"] == 3
    assert c.get("/attendance", params={"date_from": "2026-07-08", "date_to": "2026-07-06"}).status_code == 422
    assert c.get("/attendance", params={"date_from": "garbage"}).status_code == 422
    assert c.get("/attendance", params={"status": "NOPE"}).status_code == 422
    assert c.get("/attendance", params={"page": 99}).json()["items"] == []


def test_concurrent_races(api):
    from concurrent.futures import ThreadPoolExecutor
    c, _ = api
    codes = []
    with ThreadPoolExecutor(8) as ex:
        codes = list(ex.map(lambda _: c.post("/employees", json=emp()).status_code, range(12)))
    assert sorted(codes) == [201] + [409] * 11
    with ThreadPoolExecutor(8) as ex:
        codes = list(ex.map(lambda _: c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06 09:00:00")}).status_code, range(12)))
    assert sorted(codes) == [201] + [409] * 11
    with ThreadPoolExecutor(8) as ex:
        codes = list(ex.map(lambda _: c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06 18:00:00")}).status_code, range(12)))
    assert sorted(codes) == [200] + [409] * 11
    url = "/attendance/EMP0001/2026-07-06"
    def patch(i):
        return c.patch(url, json={"reason": "concurrent fix", "regularized_by": f"u{i}", "punch_out": ms("2026-07-06 18:00:00") + 60000 * (i + 1)}).status_code
    with ThreadPoolExecutor(8) as ex:
        codes = list(ex.map(patch, range(8)))
    ok = codes.count(200)
    assert ok >= 1 and set(codes) <= {200, 409}
    hist = c.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]["history"]
    assert len(hist) == ok  # every successful PATCH left exactly one history entry; none lost
