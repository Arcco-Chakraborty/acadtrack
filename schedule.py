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
    cov = coverage(topics)

    return {
        "lectures_total": total,
        "attended": attended,
        "skipped": skipped,
        "cancelled": cancelled,
        "held": held,
        "unmarked_past": past_scheduled,
        "remaining": remaining,
        "attendance_pct": round(100 * attended / marked) if marked else None,
        "topics_total": cov["topics_total"],
        "topics_done": cov["topics_done"],
        "topics_partial": cov["topics_partial"],
        "topics_left": cov["topics_left"],
        # Syllabus progress is weighted by the handout's hours per topic, not
        # by topic count — see coverage().
        "topics_pct": cov["pct_done"],
        "covered_frac": cov["covered_frac"],
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


def split_hours(ranges) -> list[int]:
    """Turn each topic's handout lecture range into a weight in hours.

    Handouts routinely quote one range across several topics of a module --
    DEMO 209 gives "L8-18" to three separate lines -- so a range is shared out
    between the topics that quote it, not handed to each of them. Without that
    the course adds up to 114 hours instead of its actual 42.

    `ranges` is one (from, to) or None per topic, in handout order. Consecutive
    topics quoting the same range form a group; its span is split as evenly as
    whole hours allow, earlier topics taking the remainder. Every topic is
    worth at least 1, so a group with more topics than hours still counts them
    all.
    """
    out = []
    i = 0
    ranges = list(ranges)
    while i < len(ranges):
        r = ranges[i]
        j = i + 1
        while j < len(ranges) and ranges[j] == r and r is not None:
            j += 1
        n = j - i
        if r is None:
            out.extend([1] * n)
        else:
            lo, hi = r
            span = max(1, hi - lo + 1)
            base, rem = divmod(span, n)
            out.extend([max(1, base + (1 if k < rem else 0)) for k in range(n)])
        i = j
    return out


def topic_hours(t) -> float:
    """How much of the syllabus one topic is worth.

    The handout's own hours-per-topic, when it gave any; 1 otherwise, which
    makes the percentage a plain topic count for a handout that listed modules
    without hours. This is a measure of how much *material* a topic holds — it
    says nothing about when it is taught, which is why it survives instructors
    running late or teaching the syllabus out of order.
    """
    try:
        h = float(t["hours"])
    except (KeyError, IndexError, TypeError, ValueError):
        return 1.0
    return h if h > 0 else 1.0


def _tidy(x: float):
    """Whole hours print as whole numbers -- "42", not "42.0". Half an hour
    comes from a partial topic and is worth showing."""
    x = round(x, 1)
    return int(x) if x == int(x) else x


def coverage(topics) -> dict:
    """How much of the syllabus is covered, weighted by handout hours.

    A `partial` topic counts half: half-taught is not nothing, and scoring it
    zero makes a course sitting mid-unit look stalled.

    Coverage is per topic and carries no assumption that the syllabus is taught
    in order — instructors reorder, and a topic later in the handout can be
    done while an earlier one is not. `behind` counts exactly those: topics
    still pending that sit before the furthest one already covered.
    """
    total = done_h = part_h = 0.0
    t_done = t_partial = 0
    weighted = False
    frontier = -1
    for i, t in enumerate(topics):
        h = topic_hours(t)
        total += h
        if h != 1.0:
            weighted = True
        if t["status"] == "done":
            done_h += h
            t_done += 1
            frontier = i
        elif t["status"] == "partial":
            part_h += h
            t_partial += 1
            frontier = i
    behind = sum(1 for t in list(topics)[:frontier + 1]
                 if t["status"] == "pending")

    covered = done_h + 0.5 * part_h
    frac = covered / total if total else None
    return {
        "hours_total": _tidy(total),
        "hours_covered": _tidy(covered),
        "hours_left": _tidy(total - covered),
        "covered_frac": frac,
        "pct_done": round(100 * frac) if frac is not None else None,
        "pct_left": round(100 * (1 - frac)) if frac is not None else None,
        "topics_total": len(topics),
        "topics_done": t_done,
        "topics_partial": t_partial,
        "topics_left": len(topics) - t_done,
        # True when the handout gave real hours, so the UI can say whether the
        # percentage is hours-weighted or just a topic count.
        "weighted": weighted,
        "behind": behind,
        "frontier": topics[frontier] if frontier >= 0 else None,
    }


def pace(stats: dict) -> dict | None:
    """Is the syllabus keeping up with the calendar? Compares the fraction of
    the syllabus covered against the fraction of classes already held. Returns
    None when either side has no data to compare."""
    if not stats["topics_total"] or not stats["lectures_total"]:
        return None
    if stats["covered_frac"] is None:
        return None
    lec_frac = stats["held"] / stats["lectures_total"]
    delta = round((stats["covered_frac"] - lec_frac) * 100)
    if abs(delta) < 8:
        label, tone = "on track", "ok"
    elif delta > 0:
        label, tone = "ahead", "good"
    else:
        label, tone = "behind", "warn"
    return {"delta": delta, "label": label, "tone": tone}
