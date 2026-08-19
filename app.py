"""app.py — acadtrack: a single-owner academic tracker.

Routes are server-rendered forms; there is no API and no client framework.
Every mutating route is POST behind a session CSRF token.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from datetime import date, datetime, timedelta

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix

import db
import llm
import schedule as sch
from auth import auth_bp, current_user, login_required

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_dotenv(path: str) -> None:
    """KEY=VALUE pairs into os.environ, never overriding a real env var."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


def create_app(db_path: str | None = None) -> Flask:
    load_dotenv(os.path.join(BASE_DIR, ".env"))
    app = Flask(__name__)

    app.config.update(
        DB_PATH=db_path or os.environ.get(
            "ACADTRACK_DB", os.path.join(BASE_DIR, "data", "acadtrack.db")),
        SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
        GOOGLE_CLIENT_ID=os.environ.get("GOOGLE_CLIENT_ID", ""),
        GOOGLE_CLIENT_SECRET=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
        GOOGLE_REDIRECT_URI=os.environ.get("GOOGLE_REDIRECT_URI", ""),
        ALLOWED_EMAILS=os.environ.get("ALLOWED_EMAILS", ""),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("ACADTRACK_HTTPS", "") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=180),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )

    def get_conn():
        if "conn" not in g:
            g.conn = db.connect(app.config["DB_PATH"])
        return g.conn

    app.config["GET_CONN"] = get_conn

    @app.teardown_appcontext
    def _close(_exc):
        conn = g.pop("conn", None)
        if conn is not None:
            conn.close()

    with app.app_context():
        db.init(db.connect(app.config["DB_PATH"]))

    # Behind nginx the real scheme arrives as X-Forwarded-Proto; without this
    # the app believes every request is plain http. One hop only — nothing
    # else is allowed to reach gunicorn on 127.0.0.1.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    app.register_blueprint(auth_bp)
    register_routes(app)

    app.jinja_env.globals.update(
        WEEKDAYS=sch.WEEKDAYS, WEEKDAYS_LONG=sch.WEEKDAYS_LONG,
        KINDS=sch.KINDS, fmt_time=sch.fmt_time, to_min=sch.time_to_minutes,
        csrf_token=lambda: _csrf_token())
    app.jinja_env.filters["strip_lec"] = strip_lec
    return app


_LEAD_LEC = re.compile(r"^\s*L\s*\d{1,3}(\s*[-–]\s*L?\s*\d{1,3})?\s*[:.]?\s+",
                       re.IGNORECASE)


def strip_lec(title: str) -> str:
    """Drop a leading 'L9-14 ' from a topic title.

    The titles carry it because that is how the handouts write them, but the UI
    already shows the range in its own column — printing it twice reads as a
    mistake. The stored title keeps it, so re-importing still matches.
    """
    return _LEAD_LEC.sub("", title or "", count=1) or title


# ------------------------------------------------------------------- CSRF --

def _csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def _check_csrf() -> None:
    if request.method == "POST":
        sent = request.form.get("_csrf", "")
        if not sent or sent != session.get("csrf"):
            abort(400, "Bad CSRF token")


# ----------------------------------------------------------------- helpers --

def _today() -> date:
    """Overridable for testing via ?today=YYYY-MM-DD."""
    override = sch.parse_date(request.args.get("today", ""))
    return override or date.today()


def _semester(conn) -> tuple[date | None, date | None]:
    return (sch.parse_date(db.get_setting(conn, "semester_start")),
            sch.parse_date(db.get_setting(conn, "semester_end")))


def _back(default: str = "dashboard"):
    return redirect(request.form.get("next") or request.referrer
                    or url_for(default))


def regenerate(conn, course_id: int | None = None, from_date: date | None = None) -> int:
    """Materialise lecture rows from slots across the semester.

    Idempotent: existing rows are kept (UNIQUE ignore), and only untouched
    future 'scheduled' rows are cleared first, so marks and topic links are
    never destroyed by a timetable edit.
    """
    start, end = _semester(conn)
    if not start or not end:
        return 0
    from_date = from_date or max(start, date.today())
    holidays = {h["date"] for h in db.list_holidays(conn)}
    # A holiday added after the schedule was built leaves classes sitting on a
    # day that will not happen, so clear those too — but only unmarked ones.
    # Both retractions skip manually added one-off classes (slot_id IS NULL):
    # they were placed by hand -- often deliberately on a holiday or beyond the
    # pattern -- so only rows the generator itself produced are swept.
    if holidays:
        marks = ",".join("?" * len(holidays))
        conn.execute(
            f"DELETE FROM lecture WHERE date IN ({marks}) AND status = 'scheduled' "
            f"AND slot_id IS NOT NULL "
            f"AND id NOT IN (SELECT lecture_id FROM lecture_topic)",
            list(holidays))
    # Likewise, shortening the semester must retract the classes beyond it.
    conn.execute(
        "DELETE FROM lecture WHERE (date < ? OR date > ?) AND status = 'scheduled' "
        "AND slot_id IS NOT NULL "
        "AND id NOT IN (SELECT lecture_id FROM lecture_topic)",
        (sch.fmt(start), sch.fmt(end)))
    conn.commit()
    courses = ([db.get_course(conn, course_id)] if course_id
               else db.list_courses(conn))
    made = 0
    for course in courses:
        if course is None:
            continue
        db.delete_untouched_lectures(conn, course["id"], sch.fmt(from_date))
        for slot in db.list_slots(conn, course["id"]):
            for d in sch.occurrence_dates(slot["weekday"], start, end, holidays):
                if db.create_lecture(conn, course["id"], sch.fmt(d),
                                     slot["start_time"], slot["end_time"],
                                     slot["kind"], slot["id"]) is not None:
                    made += 1
    return made


def retire_slot(conn, slot_id: int, from_date: str) -> None:
    """Delete a slot and the future classes it generated.

    The lecture sweep must run before the slot row goes away: the FK sets
    lecture.slot_id to NULL on delete, and matching on NULL afterwards would
    also catch manually added one-off classes. Marked or topic-linked rows
    survive, as everywhere else.
    """
    conn.execute(
        "DELETE FROM lecture WHERE slot_id = ? AND date >= ? "
        "AND status = 'scheduled' "
        "AND id NOT IN (SELECT lecture_id FROM lecture_topic)",
        (slot_id, from_date))
    db.delete_slot(conn, slot_id)


def _course_overview(conn, today: date) -> list[dict]:
    """Per-course stats plus this week's numbers, for the dashboard cards."""
    week_start, week_end = sch.week_bounds(today)
    next_exam = {}
    for e in db.list_exams(conn):            # ordered by date, so first wins
        if e["date"] >= sch.fmt(today) and e["course_id"] not in next_exam:
            next_exam[e["course_id"]] = dict(
                e, days=(sch.parse_date(e["date"]) - today).days)
    out = []
    for course in db.list_courses(conn):
        lectures = db.list_lectures(conn, course["id"])
        topics = db.list_topics(conn, course["id"])
        stats = sch.course_stats(lectures, topics, today)
        nxt = next((l for l in lectures
                    if l["date"] >= sch.fmt(today)
                    and l["status"] == "scheduled"), None)
        if nxt is not None:
            d = sch.parse_date(nxt["date"])
            nxt = dict(nxt, wd=sch.WEEKDAYS[d.weekday()])
        out.append({
            "course": course,
            "stats": stats,
            "pos": sch.syllabus_position(course, topics, stats["remaining"]),
            "week": sch.week_summary(lectures, week_start, today),
            "pace": sch.pace(stats),
            "last": db.last_covered(conn, course["id"]),
            "next": nxt,
            "exam": next_exam.get(course["id"]),
        })
    return out


# ------------------------------------------------------------------ routes --

def register_routes(app: Flask) -> None:

    @app.before_request
    def _guard():
        _check_csrf()

    @app.context_processor
    def _inject():
        return {"user": current_user(), "today": date.today()}

    # -- dashboard ---------------------------------------------------------

    @app.route("/")
    @login_required
    def dashboard():
        conn = app.config["GET_CONN"]()
        today = _today()
        week_start, week_end = sch.week_bounds(today)
        overview = _course_overview(conn, today)
        lectures = db.list_lectures(conn, start=sch.fmt(week_start),
                                    end=sch.fmt(week_end))
        sem_start, sem_end = _semester(conn)
        totals = {
            "left": sum(o["week"]["left"] for o in overview),
            "done": sum(o["week"]["done"] for o in overview),
            "total": sum(o["week"]["total"] for o in overview),
            "skipped": sum(o["stats"]["skipped"] for o in overview),
            "unmarked": sum(o["stats"]["unmarked_past"] for o in overview),
        }
        exams = [dict(e, days=(sch.parse_date(e["date"]) - today).days)
                 for e in db.list_exams(conn) if e["date"] >= sch.fmt(today)]
        today_lectures = [l for l in lectures if l["date"] == sch.fmt(today)]
        # Highlight the class happening / coming up next, but only when the
        # page is being viewed on the real today -- an overridden ?today has
        # no meaningful "now".
        now_s = datetime.now().strftime("%H:%M") if today == date.today() else None
        next_id = next((l["id"] for l in today_lectures
                        if l["status"] == "scheduled"
                        and (now_s is None or l["end_time"] > now_s)), None)
        return render_template(
            "dashboard.html", overview=overview, totals=totals,
            days=sch.group_by_day(lectures, week_start), today=today,
            week_start=week_start, week_end=week_end,
            week_no=sch.week_index(sem_start, today),
            today_lectures=today_lectures, now_s=now_s, next_id=next_id,
            exams=exams, sem_name=db.get_setting(conn, "semester_name"),
            configured=bool(sem_start and sem_end))

    @app.route("/week")
    @login_required
    def week():
        conn = app.config["GET_CONN"]()
        today = _today()
        anchor = sch.parse_date(request.args.get("start", "")) or today
        week_start, week_end = sch.week_bounds(anchor)
        lectures = db.list_lectures(conn, start=sch.fmt(week_start),
                                    end=sch.fmt(week_end))
        by_lec = db.topics_for_lectures(conn, [l["id"] for l in lectures])
        sem_start, _ = _semester(conn)
        # Topic pickers are rendered per lecture, so fetch each course's list
        # once rather than per row.
        course_topics = {cid: db.list_topics(conn, cid)
                         for cid in {l["course_id"] for l in lectures}}
        return render_template(
            "week.html", days=sch.group_by_day(lectures, week_start),
            week_start=week_start, week_end=week_end, today=today,
            topics_by_lecture=by_lec, course_topics=course_topics,
            summary=sch.week_summary(lectures, week_start, today),
            week_no=sch.week_index(sem_start, week_start),
            prev=sch.fmt(week_start - timedelta(days=7)),
            next=sch.fmt(week_start + timedelta(days=7)),
            courses=db.list_courses(conn))

    # -- courses -----------------------------------------------------------

    @app.route("/courses")
    @login_required
    def courses():
        conn = app.config["GET_CONN"]()
        return render_template("courses.html",
                               overview=_course_overview(conn, _today()),
                               archived=[c for c in db.list_courses(conn, True)
                                         if c["archived"]])

    @app.route("/courses", methods=["POST"])
    @login_required
    def course_create():
        conn = app.config["GET_CONN"]()
        code = request.form.get("code", "").strip()
        if not code:
            flash("A course needs a code.", "error")
            return _back("courses")
        cid = db.create_course(conn, code,
                               request.form.get("title", ""),
                               request.form.get("instructor", ""),
                               _int(request.form.get("credits")),
                               request.form.get("color", ""),
                               len(db.list_courses(conn)))
        return redirect(url_for("course_detail", course_id=cid))

    @app.route("/course/<int:course_id>")
    @login_required
    def course_detail(course_id: int):
        conn = app.config["GET_CONN"]()
        course = db.get_course(conn, course_id)
        if course is None:
            abort(404)
        today = _today()
        lectures = db.list_lectures(conn, course_id)
        topics = db.list_topics(conn, course_id)
        stats = sch.course_stats(lectures, topics, today)
        week_start, _ = sch.week_bounds(today)
        pos = sch.syllabus_position(course, topics, stats["remaining"])
        exams = [dict(e, days=(sch.parse_date(e["date"]) - today).days)
                 for e in db.list_exams(conn, course_id)]
        return render_template(
            "course.html", course=course, topics=topics, lectures=lectures,
            exams=exams,
            stats=stats, pace=sch.pace(stats), today=today, pos=pos,
            suggest=sch.suggest_topics(topics, course["lectures_done"] or 0),
            llm_ready=llm.configured(),
            estimated=db.get_setting(conn, f"estimated_lectures_{course_id}") == "1",
            slots=db.list_slots(conn, course_id),
            last=db.last_covered(conn, course_id),
            week=sch.week_summary(lectures, week_start, today),
            topics_by_lecture=db.topics_for_lectures(
                conn, [l["id"] for l in lectures]),
            upcoming=[l for l in lectures if l["date"] >= sch.fmt(today)][:12],
            past=list(reversed([l for l in lectures
                                if l["date"] < sch.fmt(today)]))[:40])

    @app.route("/course/<int:course_id>/edit", methods=["POST"])
    @login_required
    def course_edit(course_id: int):
        conn = app.config["GET_CONN"]()
        fields = dict(title=request.form.get("title", "").strip(),
                      instructor=request.form.get("instructor", "").strip(),
                      credits=_int(request.form.get("credits")),
                      color=request.form.get("color", ""))
        # Import matching keys on the code, so blanking it would orphan the
        # course from every future re-import.
        if request.form.get("code", "").strip():
            fields["code"] = request.form["code"].strip()
        db.update_course(conn, course_id, **fields)
        flash("Course updated.", "ok")
        return _back("courses")

    @app.route("/course/<int:course_id>/archive", methods=["POST"])
    @login_required
    def course_archive(course_id: int):
        conn = app.config["GET_CONN"]()
        db.update_course(conn, course_id,
                         archived=1 if request.form.get("archive") else 0)
        return redirect(url_for("courses"))

    @app.route("/course/<int:course_id>/delete", methods=["POST"])
    @login_required
    def course_delete(course_id: int):
        conn = app.config["GET_CONN"]()
        db.delete_course(conn, course_id)
        flash("Course deleted.", "ok")
        return redirect(url_for("courses"))

    # -- slots -------------------------------------------------------------

    @app.route("/course/<int:course_id>/slots", methods=["POST"])
    @login_required
    def slot_create(course_id: int):
        conn = app.config["GET_CONN"]()
        start = request.form.get("start_time", "").strip()
        end = request.form.get("end_time", "").strip()
        if not start or not end:
            flash("A slot needs a start and end time.", "error")
            return _back("courses")
        db.create_slot(conn, course_id,
                       _int(request.form.get("weekday")) or 0, start, end,
                       request.form.get("kind", "L"),
                       request.form.get("room", "").strip(),
                       request.form.get("label", "").strip())
        made = regenerate(conn, course_id)
        flash(f"Slot added, {made} classes scheduled.", "ok")
        return _back("courses")

    @app.route("/slot/<int:slot_id>/delete", methods=["POST"])
    @login_required
    def slot_delete(slot_id: int):
        conn = app.config["GET_CONN"]()
        retire_slot(conn, slot_id, sch.fmt(date.today()))
        flash("Slot removed.", "ok")
        return _back("courses")

    # -- topics ------------------------------------------------------------

    @app.route("/course/<int:course_id>/topics", methods=["POST"])
    @login_required
    def topic_create(course_id: int):
        conn = app.config["GET_CONN"]()
        # One per line, so a whole syllabus can be pasted in one go.
        # "Unit 2: Sorting" splits into unit + title.
        raw = request.form.get("titles", "") or request.form.get("title", "")
        added = 0
        for line in raw.splitlines():
            line = line.strip().lstrip("-*•").strip()
            if not line:
                continue
            unit = request.form.get("unit", "").strip()
            if ":" in line and len(line.split(":", 1)[0]) <= 30:
                head, tail = line.split(":", 1)
                if tail.strip():
                    unit, line = head.strip(), tail.strip()
            db.create_topic(conn, course_id, line, unit)
            added += 1
        flash(f"{added} topic{'s' if added != 1 else ''} added.", "ok")
        return _back("courses")

    @app.route("/course/<int:course_id>/progress", methods=["POST"])
    @login_required
    def course_progress(course_id: int):
        """The main way progress is recorded: 'done till lecture N', plus the
        topic checkboxes that N suggested (and the owner corrected)."""
        conn = app.config["GET_CONN"]()
        if db.get_course(conn, course_id) is None:
            abort(404)
        fields = {}
        n = _int(request.form.get("lectures_done"))
        if n is not None:
            fields["lectures_done"] = max(0, n)
        planned = _int(request.form.get("lectures_planned"))
        if planned is not None:
            fields["lectures_planned"] = max(0, planned) or None
        if fields:
            db.update_course(conn, course_id, **fields)

        # Absent `topics_submitted` the form only moved the watermark, so the
        # existing topic states must be left alone rather than all cleared.
        if request.form.get("topics_submitted"):
            done = {_int(t) for t in request.form.getlist("done")}
            partial = {_int(t) for t in request.form.getlist("partial")}
            db.set_topic_statuses(conn, course_id,
                                  {d for d in done if d},
                                  {p for p in partial if p} - done)
        flash("Progress saved.", "ok")
        return _back("courses")

    @app.route("/course/<int:course_id>/suggest")
    @login_required
    def course_suggest(course_id: int):
        """Preview what a watermark would tick, without committing it."""
        conn = app.config["GET_CONN"]()
        course = db.get_course(conn, course_id)
        if course is None:
            abort(404)
        topics = db.list_topics(conn, course_id)
        n = _int(request.args.get("n"))
        if n is None:
            n = course["lectures_done"] or 0
        s = sch.suggest_topics(topics, n)
        return {"n": n, "done": sorted(s["done"]),
                "partial": sorted(s["partial"]),
                "unnumbered": [t["id"] for t in s["unnumbered"]]}

    @app.route("/course/<int:course_id>/estimate", methods=["POST"])
    @login_required
    def course_estimate(course_id: int):
        """Fill in lecture ranges for a course whose handout gave none.

        Only ever touches topics that have no numbers already — a handout's own
        numbering is fact and must never be overwritten by an estimate.
        """
        conn = app.config["GET_CONN"]()
        course = db.get_course(conn, course_id)
        if course is None:
            abort(404)
        if not llm.configured():
            flash("No Gemini keys configured (GEMINI_API_KEYS).", "error")
            return _back("courses")

        topics = db.list_topics(conn, course_id)
        if any(t["lec_from"] for t in topics):
            flash("This course already has lecture numbers from its handout — "
                  "not overwriting them.", "error")
            return _back("courses")
        planned = (_int(request.form.get("lectures_planned"))
                   or course["lectures_planned"])
        if not planned:
            flash("Set how many lectures the course plan has first.", "error")
            return _back("courses")

        try:
            ranges = llm.estimate_lecture_ranges(
                course["code"], course["title"], planned,
                [t["title"] for t in topics])
        except llm.LLMError as e:
            flash(f"Could not estimate: {e}", "error")
            return _back("courses")

        for t, (lo, hi) in zip(topics, ranges):
            db.update_topic(conn, t["id"], lec_from=lo, lec_to=hi)
        db.update_course(conn, course_id, lectures_planned=planned)
        db.set_setting(conn, f"estimated_lectures_{course_id}", "1")
        flash(f"Estimated lecture ranges for {len(ranges)} topics. "
              f"These are a guess, not the handout — check them.", "ok")
        return _back("courses")

    @app.route("/topic/<int:topic_id>", methods=["POST"])
    @login_required
    def topic_update(topic_id: int):
        conn = app.config["GET_CONN"]()
        fields = {}
        for key in ("title", "unit", "status", "note"):
            if key in request.form:
                fields[key] = request.form[key].strip()
        if fields:
            db.update_topic(conn, topic_id, **fields)
        return _back("courses")

    @app.route("/topic/<int:topic_id>/delete", methods=["POST"])
    @login_required
    def topic_delete(topic_id: int):
        conn = app.config["GET_CONN"]()
        db.delete_topic(conn, topic_id)
        return _back("courses")

    @app.route("/topic/<int:topic_id>/move", methods=["POST"])
    @login_required
    def topic_move(topic_id: int):
        """Swap position with the neighbour above/below."""
        conn = app.config["GET_CONN"]()
        row = conn.execute("SELECT * FROM topic WHERE id = ?",
                           (topic_id,)).fetchone()
        if row is None:
            abort(404)
        up = request.form.get("dir") == "up"
        peers = db.list_topics(conn, row["course_id"])
        idx = next((i for i, t in enumerate(peers) if t["id"] == topic_id), None)
        if idx is None:
            return _back("courses")
        swap = idx - 1 if up else idx + 1
        if swap < 0 or swap >= len(peers):
            return _back("courses")
        db.update_topic(conn, topic_id, position=peers[swap]["position"])
        db.update_topic(conn, peers[swap]["id"], position=row["position"])
        return _back("courses")

    # -- lectures ----------------------------------------------------------

    @app.route("/lecture/<int:lecture_id>/status", methods=["POST"])
    @login_required
    def lecture_status(lecture_id: int):
        conn = app.config["GET_CONN"]()
        status = request.form.get("status", "scheduled")
        if status not in sch.STATUSES:
            abort(400)
        db.set_lecture_status(conn, lecture_id, status)
        return _back("dashboard")

    @app.route("/lecture/<int:lecture_id>/note", methods=["POST"])
    @login_required
    def lecture_note(lecture_id: int):
        conn = app.config["GET_CONN"]()
        db.set_lecture_note(conn, lecture_id, request.form.get("note", "").strip())
        return _back("dashboard")

    @app.route("/lecture/<int:lecture_id>/topics", methods=["POST"])
    @login_required
    def lecture_topics(lecture_id: int):
        """Record what was covered, and advance those topics to done."""
        conn = app.config["GET_CONN"]()
        lec = db.get_lecture(conn, lecture_id)
        if lec is None:
            abort(404)
        chosen = {_int(t) for t in request.form.getlist("topic_id") if _int(t)}
        existing = {t["id"] for t in
                    db.topics_for_lectures(conn, [lecture_id]).get(lecture_id, [])}
        for tid in chosen - existing:
            db.link_topic(conn, lecture_id, tid)
            if request.form.get("mark_done", "1") == "1":
                db.update_topic(conn, tid, status="done")
        for tid in existing - chosen:
            db.unlink_topic(conn, lecture_id, tid)
        return _back("dashboard")

    @app.route("/lecture/add", methods=["POST"])
    @login_required
    def lecture_add():
        """A one-off extra class that is not on the weekly timetable."""
        conn = app.config["GET_CONN"]()
        cid = _int(request.form.get("course_id"))
        d = sch.parse_date(request.form.get("date", ""))
        if not cid or not d:
            flash("Pick a course and a date.", "error")
            return _back("week")
        start = request.form.get("start_time", "09:00")
        end = request.form.get("end_time", "10:00")
        if end <= start:
            flash("The class has to end after it starts.", "error")
            return _back("week")
        made = db.create_lecture(conn, cid, sch.fmt(d), start, end,
                                 request.form.get("kind", "L"))
        if made is None:
            flash("That class is already on the schedule.", "error")
        else:
            flash("Extra class added.", "ok")
        return _back("week")

    @app.route("/lecture/<int:lecture_id>/delete", methods=["POST"])
    @login_required
    def lecture_delete(lecture_id: int):
        conn = app.config["GET_CONN"]()
        db.delete_lecture(conn, lecture_id)
        return _back("week")

    # -- exams -------------------------------------------------------------

    @app.route("/course/<int:course_id>/exams", methods=["POST"])
    @login_required
    def exam_add(course_id: int):
        conn = app.config["GET_CONN"]()
        if db.get_course(conn, course_id) is None:
            abort(404)
        label = request.form.get("label", "").strip()
        d = sch.parse_date(request.form.get("date", ""))
        if not label or not d:
            flash("An exam needs a name and a date.", "error")
            return _back("courses")
        db.upsert_exam(conn, course_id, label, sch.fmt(d),
                       request.form.get("session", "").strip())
        flash(f"{label} saved.", "ok")
        return _back("courses")

    @app.route("/exam/<int:exam_id>/delete", methods=["POST"])
    @login_required
    def exam_delete(exam_id: int):
        conn = app.config["GET_CONN"]()
        db.delete_exam(conn, exam_id)
        return _back("courses")

    # -- timetable ---------------------------------------------------------

    @app.route("/timetable")
    @login_required
    def timetable():
        conn = app.config["GET_CONN"]()
        slots = db.list_slots(conn)
        # A real time grid: columns are days, rows are 5-minute ticks, and a
        # block spans its actual duration -- a 2-hour practical reads as one.
        used_days = {s["weekday"] for s in slots}
        n_days = max([5] + [d + 1 for d in used_days])
        lo = min([sch.time_to_minutes(s["start_time"]) for s in slots]
                 or [8 * 60])
        hi = max([sch.time_to_minutes(s["end_time"]) for s in slots]
                 or [18 * 60])
        lo = (lo // 60) * 60
        hi = ((hi + 59) // 60) * 60
        blocks = [{
            "s": s,
            "col": s["weekday"] + 2,
            "r1": (sch.time_to_minutes(s["start_time"]) - lo) // 5 + 2,
            "r2": (sch.time_to_minutes(s["end_time"]) - lo + 4) // 5 + 2,
        } for s in slots]
        return render_template(
            "timetable.html", slots=slots, blocks=blocks, n_days=n_days,
            ticks=(hi - lo) // 5, lo=lo, hours=list(range(lo, hi, 60)),
            today_wd=date.today().weekday(), courses=db.list_courses(conn))

    @app.route("/timetable/import", methods=["POST"])
    @login_required
    def timetable_import():
        """Bulk-load courses + slots from JSON (see seed_timetable.py for the
        shape). Existing courses are matched by code and extended, not
        duplicated."""
        conn = app.config["GET_CONN"]()
        try:
            payload = json.loads(request.form.get("payload", ""))
        except json.JSONDecodeError as e:
            flash(f"That is not valid JSON: {e}", "error")
            return _back("timetable")
        try:
            made = import_timetable(conn, payload,
                                    replace=bool(request.form.get("replace")))
        except (TypeError, KeyError, ValueError) as e:
            flash(f"Could not read that timetable: {e}", "error")
            return _back("timetable")
        regenerate(conn)
        flash(f"Imported {made['courses']} courses and {made['slots']} slots.", "ok")
        return redirect(url_for("timetable"))

    @app.route("/schedule/regenerate", methods=["POST"])
    @login_required
    def schedule_regenerate():
        conn = app.config["GET_CONN"]()
        made = regenerate(conn)
        flash(f"{made} classes scheduled.", "ok")
        return _back("settings_page")

    # -- settings ----------------------------------------------------------

    @app.route("/settings")
    @login_required
    def settings_page():
        conn = app.config["GET_CONN"]()
        return render_template("settings.html", settings=db.all_settings(conn),
                               holidays=db.list_holidays(conn),
                               courses=db.list_courses(conn, True),
                               allowed=app.config.get("ALLOWED_EMAILS", ""))

    @app.route("/settings", methods=["POST"])
    @login_required
    def settings_save():
        conn = app.config["GET_CONN"]()
        start = sch.parse_date(request.form.get("semester_start", ""))
        end = sch.parse_date(request.form.get("semester_end", ""))
        if start and end and end < start:
            # Accepting this would retract every generated class as
            # "outside the semester".
            flash("The semester cannot end before it starts.", "error")
            return redirect(url_for("settings_page"))
        for key in ("semester_start", "semester_end", "semester_name"):
            if key in request.form:
                db.set_setting(conn, key, request.form[key].strip())
        made = regenerate(conn)
        flash(f"Saved. {made} classes scheduled.", "ok")
        return redirect(url_for("settings_page"))

    @app.route("/holidays", methods=["POST"])
    @login_required
    def holiday_add():
        conn = app.config["GET_CONN"]()
        d = sch.parse_date(request.form.get("date", ""))
        if not d:
            flash("Give the date as YYYY-MM-DD.", "error")
            return _back("settings_page")
        db.add_holiday(conn, sch.fmt(d), request.form.get("label", "").strip())
        # A new holiday must remove the pattern classes already sitting on that
        # day. One-off classes stay: a makeup class is often deliberately
        # placed on a holiday.
        conn.execute(
            "DELETE FROM lecture WHERE date = ? AND status = 'scheduled' "
            "AND slot_id IS NOT NULL "
            "AND id NOT IN (SELECT lecture_id FROM lecture_topic)", (sch.fmt(d),))
        conn.commit()
        flash("Holiday added and that day cleared.", "ok")
        return redirect(url_for("settings_page"))

    @app.route("/holidays/<int:holiday_id>/delete", methods=["POST"])
    @login_required
    def holiday_delete(holiday_id: int):
        conn = app.config["GET_CONN"]()
        db.delete_holiday(conn, holiday_id)
        regenerate(conn)
        return redirect(url_for("settings_page"))

    @app.route("/healthz")
    def healthz():
        return {"ok": True}


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def import_timetable(conn, payload: dict, replace: bool = False) -> dict:
    """Load {"semester": {...}, "courses": [{code, title, slots: [...]}]}.

    Matching is by course code, so re-importing a corrected timetable updates
    in place instead of creating a second copy of every course.
    """
    counts = {"courses": 0, "slots": 0, "holidays": 0, "topics": 0,
              "exams": 0, "dropped": 0}
    sem = payload.get("semester") or {}
    for key, setting in (("start", "semester_start"), ("end", "semester_end"),
                         ("name", "semester_name")):
        if sem.get(key):
            db.set_setting(conn, setting, str(sem[key]))

    # Holidays travel with the semester so the whole term is reproducible from
    # one file. A range ("from"/"to") expands to every date inclusive, which is
    # how an academic calendar states exam weeks and breaks.
    for h in payload.get("holidays", []):
        label = str(h.get("label", "")) if isinstance(h, dict) else ""
        if isinstance(h, str):
            dates = [h]
        elif h.get("from") and h.get("to"):
            a, b = sch.parse_date(str(h["from"])), sch.parse_date(str(h["to"]))
            dates = [sch.fmt(a + timedelta(days=i))
                     for i in range((b - a).days + 1)] if a and b else []
        else:
            dates = [str(h.get("date", ""))]
        for d in dates:
            if sch.parse_date(d):
                db.add_holiday(conn, d, label)
                counts["holidays"] += 1

    for entry in payload.get("courses", []):
        code = str(entry.get("code", "")).strip()
        if not code:
            continue
        course = db.get_course_by_code(conn, code)
        if course is None:
            cid = db.create_course(
                conn, code, str(entry.get("title", "")),
                str(entry.get("instructor", "")), _int(entry.get("credits")),
                str(entry.get("color", "")), len(db.list_courses(conn)))
            counts["courses"] += 1
        else:
            cid = course["id"]
            db.update_course(conn, cid, archived=0,
                             title=str(entry.get("title") or course["title"]),
                             instructor=str(entry.get("instructor")
                                            or course["instructor"]),
                             credits=_int(entry.get("credits")) or course["credits"],
                             color=str(entry.get("color") or course["color"]))
        if entry.get("lectures_planned"):
            db.update_course(conn, cid,
                             lectures_planned=_int(entry["lectures_planned"]))
        if replace:
            # The old pattern is void, so its future unmarked classes go too.
            for s in db.list_slots(conn, cid):
                retire_slot(conn, s["id"], sch.fmt(date.today()))

        have = {(s["weekday"], s["start_time"], s["kind"])
                for s in db.list_slots(conn, cid)}
        for slot in entry.get("slots", []):
            wd = slot.get("weekday")
            if isinstance(wd, str):
                wd = _weekday_num(wd)
            start = str(slot.get("start", slot.get("start_time", ""))).strip()
            end = str(slot.get("end", slot.get("end_time", ""))).strip()
            kind = str(slot.get("kind", "L")).upper()[:1] or "L"
            if wd is None or not start or not end:
                continue
            if (wd, start, kind) in have:
                continue
            db.create_slot(conn, cid, wd, start, end, kind,
                           str(slot.get("room", "")), str(slot.get("label", "")))
            have.add((wd, start, kind))
            counts["slots"] += 1

        # Match slots' behaviour: re-importing a corrected syllabus must not
        # append a second copy of every topic.
        have_topics = {t["title"].strip().lower(): t
                       for t in db.list_topics(conn, cid)}
        for t in entry.get("topics", []):
            title = t if isinstance(t, str) else str(t.get("title", ""))
            unit = "" if isinstance(t, str) else str(t.get("unit", ""))
            lo = hi = None
            if isinstance(t, dict):
                lo, hi = parse_lec_range(t.get("lec", ""))
                lo = _int(t.get("lec_from")) if t.get("lec_from") else lo
                hi = _int(t.get("lec_to")) if t.get("lec_to") else hi
            if lo is None and hi is None:      # else infer from "L9-14 ..."
                lo, hi = parse_lec_range(title)
                if lo is None:
                    lo, hi = parse_lec_range(unit)
            key = title.strip().lower()
            if not key:
                continue
            existing = have_topics.get(key)
            if existing is None:
                tid = db.create_topic(conn, cid, title, unit,
                                      lec_from=lo, lec_to=hi)
                counts["topics"] += 1
                # A same-title line later in this payload must see the row
                # that now exists, not a placeholder it cannot update.
                have_topics[key] = {"id": tid, "lec_from": lo, "lec_to": hi}
            elif (lo, hi) != (existing["lec_from"], existing["lec_to"]):
                # Correcting the numbering must not require deleting topics,
                # which would take their done/partial state with them.
                db.update_topic(conn, existing["id"], lec_from=lo, lec_to=hi)
                have_topics[key] = {"id": existing["id"],
                                    "lec_from": lo, "lec_to": hi}

        for ex in entry.get("exams", []):
            if not isinstance(ex, dict):
                continue
            d = sch.parse_date(str(ex.get("date", "")))
            label = str(ex.get("label", "")).strip()
            if not d or not label:
                continue
            db.upsert_exam(conn, cid, label, sch.fmt(d),
                           str(ex.get("session", "")))
            counts["exams"] += 1

    # Courses no longer taken. Archiving rather than deleting keeps their
    # history and is reversible; every cross-course view already excludes an
    # archived course, so its classes stop cluttering the week.
    for code in payload.get("dropped", []):
        course = db.get_course_by_code(conn, str(code).strip())
        if course is not None and not course["archived"]:
            db.update_course(conn, course["id"], archived=1)
            counts["dropped"] += 1
    return counts


_LEC_RE = re.compile(r"^\s*L?\s*(\d{1,3})\s*(?:[-–]\s*L?\s*(\d{1,3}))?\s*$",
                     re.IGNORECASE)


def parse_lec_range(text) -> tuple:
    """'9-14' / 'L9-14' / 'L3' -> (9, 14) / (9, 14) / (3, 3).

    Only a bare lecture reference is accepted. A title like 'L1.1 First order
    equations' deliberately does NOT match: dotted numbering is per-module, not
    the global lecture count, and reading it as one silently mis-maps the whole
    syllabus. Those courses carry explicit lec_from/lec_to instead.
    """
    if not text:
        return (None, None)
    head = str(text).split(None, 1)[0] if " " in str(text) else str(text)
    m = _LEC_RE.match(head)
    if not m:
        return (None, None)
    lo = int(m.group(1))
    hi = int(m.group(2)) if m.group(2) else lo
    return (lo, hi) if lo <= hi else (hi, lo)


def _weekday_num(name: str) -> int | None:
    aliases = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Thr": 3, "Ths": 3,
               "Fri": 4, "Sat": 5, "Sun": 6}
    return aliases.get(name.strip()[:3].title())


app = create_app()

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "5000")),
            debug=os.environ.get("FLASK_DEBUG") == "1")
