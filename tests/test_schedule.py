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


def TP(id, hours=1, status="pending"):
    return {"id": id, "hours": hours, "status": status}


def test_coverage_weights_topics_by_the_handout_hours():
    # 2 + 6 + 4 = 12 hours; the 6-hour topic alone is half the syllabus.
    topics = [TP(1, 2, "done"), TP(2, 6, "done"), TP(3, 4)]
    cov = sch.coverage(topics)
    assert cov["hours_total"] == 12
    assert cov["hours_covered"] == 8
    assert cov["hours_left"] == 4
    assert cov["pct_done"] == 67
    assert cov["pct_left"] == 33
    assert cov["weighted"] is True
    assert cov["topics_left"] == 1


def test_coverage_counts_a_partial_topic_as_half_its_hours():
    """Half-taught is not nothing; scoring it zero makes a course sitting
    mid-unit look stalled."""
    cov = sch.coverage([TP(1, 4, "done"), TP(2, 4, "partial"), TP(3, 4)])
    assert cov["hours_covered"] == 6      # 4 + half of 4
    assert cov["pct_left"] == 50
    assert cov["topics_partial"] == 1
    assert cov["topics_left"] == 2        # partial is not finished


def test_coverage_falls_back_to_a_plain_topic_count():
    """A handout that listed modules without hours weighs every topic the
    same, and the UI is told so."""
    cov = sch.coverage([TP(1, 1, "done"), TP(2), TP(3), TP(4)])
    assert cov["weighted"] is False
    assert cov["hours_total"] == 4
    assert cov["pct_left"] == 75


def test_coverage_makes_no_assumption_that_teaching_follows_the_handout():
    """Instructors reorder. A topic done late in the handout says nothing
    about the ones before it, which stay pending and stay counted as left."""
    topics = [TP(1, 3), TP(2, 3), TP(3, 3, "done")]
    cov = sch.coverage(topics)
    assert cov["hours_covered"] == 3
    assert cov["pct_left"] == 67
    assert cov["behind"] == 2          # two skipped over, not covered
    assert cov["frontier"]["id"] == 3


def test_coverage_reports_nothing_skipped_when_teaching_ran_in_order():
    cov = sch.coverage([TP(1, 2, "done"), TP(2, 2, "partial"), TP(3, 2)])
    assert cov["behind"] == 0


def test_coverage_with_no_topics_reports_none_not_zero():
    cov = sch.coverage([])
    assert cov["pct_done"] is None and cov["pct_left"] is None
    assert cov["hours_total"] == 0
    assert cov["frontier"] is None


def test_course_stats_percentage_is_the_weighted_one():
    stats = sch.course_stats([L("attended")],
                             [TP(1, 9, "done"), TP(2, 1)],
                             date(2054, 8, 17))
    assert stats["topics_pct"] == 90        # hours, not 1-of-2 = 50
    assert stats["topics_total"] == 2


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


def test_split_hours_shares_a_range_quoted_against_several_topics():
    """DEMO 209's handout gives "L8-18" to three separate topics. That is
    eleven hours between them, not eleven each -- read the other way the
    course adds up to 114 hours instead of its actual 42."""
    ranges = [(1, 2), (3, 7), (3, 7), (8, 18), (8, 18), (8, 18)]
    hours = sch.split_hours(ranges)
    assert hours == [2, 3, 2, 4, 4, 3]
    assert sum(hours) == 18                    # the span it actually covers


def test_split_hours_leaves_distinct_ranges_alone():
    assert sch.split_hours([(1, 2), (3, 3), (4, 8)]) == [2, 1, 5]


def test_split_hours_gives_every_topic_at_least_an_hour():
    """More topics than the range has hours still counts them all -- a topic
    worth zero would vanish from the percentage."""
    assert sch.split_hours([(1, 2)] * 4) == [1, 1, 1, 1]


def test_split_hours_defaults_to_one_without_a_range():
    assert sch.split_hours([None, None, (4, 6)]) == [1, 1, 3]
    assert sch.split_hours([]) == []


def test_split_hours_does_not_merge_a_range_that_recurs_later():
    """Only neighbours share. The same range appearing again after another
    topic is a separate group, because handouts write modules in blocks."""
    assert sch.split_hours([(1, 4), (5, 6), (1, 4)]) == [4, 2, 4]
