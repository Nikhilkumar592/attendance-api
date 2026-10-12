from datetime import datetime

import pytest
from app import main
from app.main import IST, compute_late_minutes, compute_overtime, compute_work_hours, attendance_date, derive_fields


def ist(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)


def test_late_examples_from_contract():
    d = "2026-07-06"
    assert compute_late_minutes(ist("2026-07-06 09:40:00"), d, "09:30") == 0
    assert compute_late_minutes(ist("2026-07-06 09:40:01"), d, "09:30") == 10
    assert compute_late_minutes(ist("2026-07-06 10:15:59"), d, "09:30") == 45
    assert compute_late_minutes(ist("2026-07-06 08:00:00"), d, "09:30") == 0


def test_overtime_threshold_and_floor():
    d = "2026-07-06"
    assert compute_overtime(ist("2026-07-06 18:59:59"), d, "09:30", "18:30") == 0   # 29m59s
    assert compute_overtime(ist("2026-07-06 19:00:00"), d, "09:30", "18:30") == 30
    assert compute_overtime(ist("2026-07-06 19:10:59"), d, "09:30", "18:30") == 40
    assert compute_overtime(ist("2026-07-06 17:00:00"), d, "09:30", "18:30") == 0


def test_overnight_shift():
    assert attendance_date(ist("2026-07-06 21:55:00"), "22:00", "06:00") == "2026-07-06"
    assert attendance_date(ist("2026-07-07 03:00:00"), "22:00", "06:00") == "2026-07-06"
    assert attendance_date(ist("2026-07-07 06:00:00"), "22:00", "06:00") == "2026-07-07"
    assert compute_overtime(ist("2026-07-07 06:40:00"), "2026-07-06", "22:00", "06:00") == 40
    # sample 7 in DATA_MODEL: 21:55 -> 06:40 next day = 8.75h
    assert compute_work_hours(ist("2026-07-06 21:55:00"), ist("2026-07-07 06:40:00")) == 8.75


def test_work_hours_half_up_not_bankers_or_float():
    base = ist("2026-07-06 09:00:00")
    # 18 s = 0.005 h -> 0.01 (float round() gives 0.01 or 0.0 depending on repr; Decimal half-up is exact)
    assert compute_work_hours(base, ist("2026-07-06 09:00:18")) == 0.01
    # 4.485 h = 16146 s sits exactly on the half-up boundary -> 4.49
    from datetime import timedelta
    assert compute_work_hours(base, base + timedelta(seconds=16146)) == 4.49


def test_half_day_uses_rounded_hours():
    emp = {"shift_start": "09:30", "shift_end": "18:30"}
    pin = ist("2026-07-06 09:00:00")
    # 4h29m56s = 4.4989h -> rounds to 4.50 -> NOT a half day
    pout = ist("2026-07-06 13:29:56")
    f = derive_fields(emp, "2026-07-06", "PRESENT", pin, pout)
    assert f["work_hours"] == 4.5 and f["half_day"] is False
    f = derive_fields(emp, "2026-07-06", "PRESENT", pin, ist("2026-07-06 13:29:00"))
    assert f["work_hours"] == 4.48 and f["half_day"] is True
