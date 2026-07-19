from app import _next_available_workday


def test_next_workday_defaults_when_work_week_is_not_a_collection():
    result = _next_available_workday("2026-04-24", set(), True)

    assert result == "2026-04-27"


def test_next_workday_skips_weekends_and_ended_days():
    result = _next_available_workday(
        "2026-04-24",
        {"2026-04-27"},
        [1, 2, 3, 4, 5],
    )

    assert result == "2026-04-28"
