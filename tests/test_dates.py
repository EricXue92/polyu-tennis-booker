from datetime import date

from freezegun import freeze_time

from src.dates import compute_target_date


@freeze_time("2026-05-09 00:30:00", tz_offset=0)  # 08:30 HKT
def test_target_is_seven_days_after_today_hkt():
    assert compute_target_date() == date(2026, 5, 16)


@freeze_time("2026-05-09 15:59:00", tz_offset=0)  # 23:59 HKT same day
def test_target_uses_hkt_calendar_day_late_evening():
    assert compute_target_date() == date(2026, 5, 16)


@freeze_time("2026-05-09 16:00:00", tz_offset=0)  # 00:00 HKT next day
def test_target_rolls_over_at_hkt_midnight():
    assert compute_target_date() == date(2026, 5, 17)
