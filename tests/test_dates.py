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


from datetime import time
from unittest.mock import patch

import pytest

from src.dates import seconds_until_hkt_time, sleep_until_hkt


@freeze_time("2026-05-09 00:25:00", tz_offset=0)  # 08:25 HKT, target 08:30 HKT
def test_seconds_until_future_time_today():
    assert seconds_until_hkt_time(time(8, 30)) == pytest.approx(300, abs=1)


@freeze_time("2026-05-09 00:30:00", tz_offset=0)  # exactly 08:30 HKT
def test_seconds_until_past_time_returns_zero():
    # If target already passed today, return 0 (don't wait until tomorrow).
    assert seconds_until_hkt_time(time(8, 0)) == 0


@freeze_time("2026-05-09 00:25:00", tz_offset=0)
def test_sleep_until_calls_sleep_with_correct_duration():
    with patch("src.dates.time.sleep") as mock_sleep:
        sleep_until_hkt(time(8, 30))
    mock_sleep.assert_called_once()
    duration = mock_sleep.call_args[0][0]
    assert 299 <= duration <= 301


@freeze_time("2026-05-09 00:30:00", tz_offset=0)
def test_sleep_until_skips_when_already_past():
    with patch("src.dates.time.sleep") as mock_sleep:
        sleep_until_hkt(time(8, 0))
    mock_sleep.assert_not_called()
