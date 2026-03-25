from datetime import datetime, timezone

import db


def test_dt_to_timestamp_assumes_naive_values_are_utc():
    value = datetime(2026, 3, 23, 10, 30, 45)

    assert db._dt_to_timestamp(value) == datetime(2026, 3, 23, 10, 30, 45, tzinfo=timezone.utc).timestamp()


def test_dt_to_iso_marks_utc_timezone():
    value = datetime(2026, 3, 23, 10, 30, 45)

    assert db._dt_to_iso(value) == "2026-03-23T10:30:45Z"
