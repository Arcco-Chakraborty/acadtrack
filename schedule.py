"""schedule.py — pure date/statistics logic.

Nothing here touches the database or Flask, so it can be tested directly.
Weekdays are 0=Mon .. 6=Sun throughout (matching date.weekday()).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKDAYS_LONG = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                 "Saturday", "Sunday"]
KINDS = {"L": "Lecture", "T": "Tutorial", "P": "Practical"}
STATUSES = ["scheduled", "attended", "skipped", "cancelled"]

# A lecture that has happened, whether or not the owner turned up. Cancelled
# classes are excluded everywhere: they were never taught, so they neither
# count against attendance nor consume syllabus.
HELD = ("attended", "skipped")


def parse_date(s: str) -> date | None:
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return None


def fmt(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def week_bounds(d: date) -> tuple[date, date]:
    """(Monday, Sunday) of the week containing d."""
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)


def week_index(semester_start: date | None, d: date) -> int | None:
    """1-based teaching week number, or None without a semester start."""
    if not semester_start:
        return None
    start_monday, _ = week_bounds(semester_start)
    days = (d - start_monday).days
    return (days // 7) + 1 if days >= 0 else None


def occurrence_dates(weekday: int, start: date, end: date,
                     holidays: set[str] | None = None) -> list[date]:
    """Every `weekday` date in [start, end], skipping holidays."""
    if start > end:
        return []
    holidays = holidays or set()
    first = start + timedelta(days=(weekday - start.weekday()) % 7)
    out, cur = [], first
    while cur <= end:
        if fmt(cur) not in holidays:
            out.append(cur)
        cur += timedelta(days=7)
    return out


def time_to_minutes(hhmm: str) -> int:
    try:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return 0


def fmt_time(hhmm: str) -> str:
    """'14:00' -> '2:00 pm'. Returns the input unchanged if unparseable."""
    mins = time_to_minutes(hhmm)
    if not hhmm or ":" not in str(hhmm):
        return hhmm
    h, m = divmod(mins, 60)
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {suffix}"


def course_stats(lectures, topics, today: date) -> dict:
    """Aggregate one course's lectures + topics into the numbers the UI shows.

    `lectures` and `topics` are sequences of mappings (sqlite3.Row works).
    """
    today_s = fmt(today)
    total = attended = skipped = cancelled = past_scheduled = remaining = 0
    for lec in lectures:
        status = lec["status"]
        if status == "cancelled":
            cancelled += 1
            continue
        total += 1
        if status == "attended":
            attended += 1
        elif status == "skipped":
            skipped += 1
        elif lec["date"] < today_s:
            past_scheduled += 1   # happened but never marked
        else:
            remaining += 1

    held = attended + skipped
    marked = held  # unmarked past lectures are excluded from the percentage
    t_total = len(topics)
    t_done = sum(1 for t in topics if t["status"] == "done")
    t_partial = sum(1 for t in topics if t["status"] == "partial")

    return {
        "lectures_total": total,
        "attended": attended,
        "skipped": skipped,
        "cancelled": cancelled,
        "held": held,
        "unmarked_past": past_scheduled,
        "remaining": remaining,
        "attendance_pct": round(100 * attended / marked) if marked else None,
        "topics_total": t_total,
        "topics_done": t_done,
        "topics_partial": t_partial,
        # Partial topics count half — a topic half-taught is not nothing, and
        # showing it as zero makes a course mid-unit look stalled.
        "topics_pct": round(100 * (t_done + 0.5 * t_partial) / t_total) if t_total else None,
        "topics_left": t_total - t_done,
    }


def week_summary(lectures, week_start: date, today: date) -> dict:
    """Counts for one week: how many classes, how many already done, how many
    still to come. 'left' is what the owner asks for — classes remaining from
    today onwards, not the whole week's total."""
    week_end = week_start + timedelta(days=6)
    ws, we, today_s = fmt(week_start), fmt(week_end), fmt(today)
    total = done = left = cancelled = 0
    for lec in lectures:
        if not (ws <= lec["date"] <= we):
            continue
        if lec["status"] == "cancelled":
            cancelled += 1
            continue
        total += 1
        if lec["status"] in HELD or lec["date"] < today_s:
            done += 1
        else:
            left += 1
    return {"total": total, "done": done, "left": left, "cancelled": cancelled}


def group_by_day(lectures, week_start: date) -> list[dict]:
    """Seven day buckets for the week grid."""
    days = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        ds = fmt(d)
        days.append({
            "date": d,
            "date_s": ds,
            "label": WEEKDAYS[i],
            "long": WEEKDAYS_LONG[i],
            "lectures": [l for l in lectures if l["date"] == ds],
        })
    return days


def suggest_topics(topics, lectures_done: int) -> dict:
    """Which topics a 'done till lecture N' watermark implies.

    A topic whose lecture range ends at or before N is done; one that straddles
    N is partial. Topics with no lecture numbers (a handout that gave modules
    but no numbering) are left out of the suggestion entirely — guessing at
    them would silently mark work as covered that may not be.

    Returns {"done": {ids}, "partial": {ids}, "unnumbered": [rows]}.
    """
    done, partial, unnumbered = set(), set(), []
    for t in topics:
        lo, hi = t["lec_from"], t["lec_to"]
        if lo is None and hi is None:
            unnumbered.append(t)
            continue
        lo = lo if lo is not None else hi
        hi = hi if hi is not None else lo
        if lectures_done >= hi:
            done.add(t["id"])
        elif lectures_done >= lo:
            partial.add(t["id"])
    return {"done": done, "partial": partial, "unnumbered": unnumbered}


def syllabus_position(course, topics, lectures_left_calendar: int) -> dict:
    """The headline numbers: where the course is, and what is left.

    `lectures_planned` comes from the handout's course plan. `remaining_plan`
    is how many of those are still to be delivered; `lectures_left_calendar`
    is how many classes the timetable still has. They answer different
    questions and can disagree — a course running behind has more calendar
    slots left than plan lectures.
    """
    done_n = course["lectures_done"] or 0
    planned = course["lectures_planned"]
    numbered = [t for t in topics if t["lec_from"] or t["lec_to"]]

    # Fall back to the last lecture any topic mentions, so a course whose
    # planned count was never set still reports something meaningful.
    if not planned and numbered:
        planned = max((t["lec_to"] or t["lec_from"]) for t in numbered)

    t_total = len(topics)
    t_done = sum(1 for t in topics if t["status"] == "done")
    t_partial = sum(1 for t in topics if t["status"] == "partial")
    t_left = t_total - t_done

    remaining_plan = max(0, planned - done_n) if planned else None
    # Topics per lecture, measured on what has actually been covered — a more
    # honest rate than dividing the syllabus by the lecture count, because the
    # early lectures of most courses move slower than the plan implies.
    rate = (t_done + 0.5 * t_partial) / done_n if done_n else None
    # The estimate depends only on the observed rate. Gating it on lectures
    # remaining used to hide the shortfall exactly when the plan had run out,
    # which is when the warning matters most.
    est_lectures_for_rest = round(t_left / rate) if rate else None

    return {
        "lectures_done": done_n,
        "lectures_planned": planned,
        "remaining_plan": remaining_plan,
        "lectures_left_calendar": lectures_left_calendar,
        "topics_total": t_total,
        "topics_done": t_done,
        "topics_partial": t_partial,
        "topics_left": t_left,
        "pct": round(100 * (t_done + 0.5 * t_partial) / t_total) if t_total else None,
        "rate": round(rate, 2) if rate else None,
        "est_lectures_for_rest": est_lectures_for_rest,
        # Will the remaining plan lectures cover the remaining topics at the
        # rate seen so far? Negative means the course has to speed up.
        "slack": (remaining_plan - est_lectures_for_rest)
                 if (remaining_plan is not None
                     and est_lectures_for_rest is not None) else None,
        "unnumbered": sum(1 for t in topics
                          if t["lec_from"] is None and t["lec_to"] is None),
    }


def pace(stats: dict) -> dict | None:
    """Is the syllabus keeping up with the calendar? Compares fraction of
    topics done against fraction of lectures held. Returns None when either
    side has no data to compare."""
    if not stats["topics_total"] or not stats["lectures_total"]:
        return None
    topic_frac = (stats["topics_done"] + 0.5 * stats["topics_partial"]) / stats["topics_total"]
    lec_frac = stats["held"] / stats["lectures_total"]
    delta = round((topic_frac - lec_frac) * 100)
    if abs(delta) < 8:
        label, tone = "on track", "ok"
    elif delta > 0:
        label, tone = "ahead", "good"
    else:
        label, tone = "behind", "warn"
    return {"delta": delta, "label": label, "tone": tone}
