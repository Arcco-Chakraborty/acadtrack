from datetime import date

import schedule as sch


def L(status="scheduled", d="2054-08-17", start="09:00"):
    return {"status": status, "date": d, "start_time": start, "end_time": "10:00"}


def T(status="pending"):
    return {"status": status}


def test_week_bounds_lands_on_monday():
    mon, sun = sch.week_bounds(date(2054, 8, 20))   # a Thursday
    assert mon == date(2054, 8, 17)
    assert sun == date(2054, 8, 23)
    assert sch.week_bounds(date(2054, 8, 17))[0] == date(2054, 8, 17)


def test_week_index_counts_from_the_start_monday():
    start = date(2054, 8, 5)                        # a Wednesday
    assert sch.week_index(start, date(2054, 8, 5)) == 1
    assert sch.week_index(start, date(2054, 8, 9)) == 1     # same week
    assert sch.week_index(start, date(2054, 8, 10)) == 2    # next Monday
    assert sch.week_index(None, date(2054, 8, 10)) is None
    assert sch.week_index(start, date(2054, 7, 1)) is None   # before term


def test_occurrence_dates_skips_holidays_and_respects_bounds():
    days = sch.occurrence_dates(0, date(2054, 8, 5), date(2054, 8, 31))
    assert days == [date(2054, 8, 10), date(2054, 8, 17),
                    date(2054, 8, 24), date(2054, 8, 31)]

    with_holiday = sch.occurrence_dates(0, date(2054, 8, 5), date(2054, 8, 31),
                                        {"2054-08-17"})
    assert date(2054, 8, 17) not in with_holiday
    assert len(with_holiday) == 3

    assert sch.occurrence_dates(0, date(2054, 9, 1), date(2054, 8, 1)) == []


def test_fmt_time_handles_noon_and_midnight():
    assert sch.fmt_time("09:00") == "9:00 am"
    assert sch.fmt_time("12:00") == "12:00 pm"
    assert sch.fmt_time("00:30") == "12:30 am"
    assert sch.fmt_time("14:05") == "2:05 pm"
    assert sch.fmt_time("") == ""


def test_course_stats_excludes_cancelled_from_every_total():
    lectures = [L("attended"), L("attended"), L("skipped"), L("cancelled"),
                L("scheduled", "2054-08-01"),        # past, never marked
                L("scheduled", "2099-01-01")]        # future
    stats = sch.course_stats(lectures, [T("done"), T("partial"), T()],
                             date(2054, 8, 17))

    assert stats["lectures_total"] == 5              # cancelled one dropped
    assert stats["cancelled"] == 1
    assert stats["held"] == 3
    assert stats["unmarked_past"] == 1
    assert stats["remaining"] == 1
    assert stats["attendance_pct"] == 67             # 2 of 3 marked
    assert stats["topics_done"] == 1
    assert stats["topics_left"] == 2
    assert stats["topics_pct"] == 50                 # partial counts half


def test_course_stats_with_nothing_reports_none_not_zero():
    stats = sch.course_stats([], [], date(2054, 8, 17))
    assert stats["attendance_pct"] is None
    assert stats["topics_pct"] is None
    assert stats["lectures_total"] == 0


def test_week_summary_left_counts_only_from_today_onward():
    week_start = date(2054, 8, 17)
    lectures = [
        L("attended", "2054-08-17"),
        L("scheduled", "2054-08-18"),    # yesterday relative to `today`
        L("scheduled", "2054-08-20"),    # still ahead
        L("cancelled", "2054-08-21"),
        L("scheduled", "2054-08-31"),    # next week: out of range
    ]
    s = sch.week_summary(lectures, week_start, date(2054, 8, 19))
    assert s["total"] == 3
    assert s["done"] == 2                # attended + past-unmarked
    assert s["left"] == 1
    assert s["cancelled"] == 1


def test_group_by_day_always_returns_seven_buckets():
    days = sch.group_by_day([L("attended", "2054-08-19")], date(2054, 8, 17))
    assert len(days) == 7
    assert days[0]["label"] == "Mon"
    assert len(days[2]["lectures"]) == 1
    assert days[3]["lectures"] == []


def test_pace_compares_syllabus_against_calendar():
    behind = sch.pace(sch.course_stats(
        [L("attended")] * 8 + [L("scheduled", "2099-01-01")] * 2,
        [T("done")] * 2 + [T()] * 8, date(2054, 8, 17)))
    assert behind["tone"] == "warn" and behind["label"] == "behind"

    ahead = sch.pace(sch.course_stats(
        [L("attended")] * 2 + [L("scheduled", "2099-01-01")] * 8,
        [T("done")] * 8 + [T()] * 2, date(2054, 8, 17)))
    assert ahead["label"] == "ahead"

    assert sch.pace(sch.course_stats([], [], date(2054, 8, 17))) is None
