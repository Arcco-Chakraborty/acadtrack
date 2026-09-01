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

    # nginx serves /static with max-age=604800, so a phone that has visited
    # once keeps the old CSS for a week and simply never sees a UI fix. Stamp
    # every static URL with the file's mtime: the HTML is uncached, so a
    # changed file gets a new URL and is fetched at once, while unchanged ones
    # stay cached for the full week.
    @app.url_defaults
    def _stamp_static(endpoint, values):
        if endpoint != "static" or "filename" not in values:
            return
        path = os.path.join(app.static_folder, values["filename"])
        try:
            values["v"] = int(os.stat(path).st_mtime)
        except OSError:
            pass

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


def _get_proposal(course_id: int, topics) -> dict | None:
    """The AI's pending reading of the log, if there is one.

    It lives in the session rather than the database: it is a proposal about
    what was covered, and only the owner's save makes it a fact. A proposal
    made before the topic list changed is dropped rather than half-applied.
    """
    p = session.get(f"proposal_{course_id}")
    if not p:
        return None
    states = p.get("states") or {}
    if {str(t["id"]) for t in topics} != set(states):
        session.pop(f"proposal_{course_id}", None)
        return None
    return dict(p, by_id={int(k): v for k, v in states.items()})


def _clear_proposal(course_id: int) -> None:
    session.pop(f"proposal_{course_id}", None)


def _back(default: str = "dashboard", **kw):
    return redirect(request.form.get("next") or request.referrer
                    or url_for(default, **kw))


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
            "cov": sch.coverage(topics),
            "week": sch.week_summary(lectures, week_start, today),
            "pace": sch.pace(stats),
            "note": db.latest_note(conn, course["id"]),
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
        cov = sch.coverage(topics)
        exams = [dict(e, days=(sch.parse_date(e["date"]) - today).days)
                 for e in db.list_exams(conn, course_id)]
        return render_template(
            "course.html", course=course, topics=topics, lectures=lectures,
            exams=exams,
            stats=stats, pace=sch.pace(stats), today=today, cov=cov,
            llm_ready=llm.configured(),
            notes=db.list_notes(conn, course_id),
            proposal=_get_proposal(course_id, topics),
            weighted_by_ai=db.get_setting(conn, f"ai_hours_{course_id}") == "1",
            slots=db.list_slots(conn, course_id),
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
        """Save the topic states — either the AI's proposal as-corrected, or a
        hand edit. Saving is always explicit: nothing the AI proposes touches
        the database until this runs."""
        conn = app.config["GET_CONN"]()
        if db.get_course(conn, course_id) is None:
            abort(404)
        done = {t for t in (_int(x) for x in request.form.getlist("done")) if t}
        partial = {t for t in (_int(x) for x in request.form.getlist("partial"))
                   if t} - done
        db.set_topic_statuses(conn, course_id, done, partial)
        _clear_proposal(course_id)
        flash("Progress saved.", "ok")
        return _back("course_detail", course_id=course_id)

    # -- the log: what was actually covered, in his own words ---------------

    @app.route("/course/<int:course_id>/note", methods=["POST"])
    @login_required
    def note_add(course_id: int):
        conn = app.config["GET_CONN"]()
        if db.get_course(conn, course_id) is None:
            abort(404)
        text = request.form.get("text", "").strip()
        if not text:
            flash("Write what was covered.", "error")
            return _back("course_detail", course_id=course_id)
        d = sch.parse_date(request.form.get("date", "")) or _today()
        db.create_note(conn, course_id, sch.fmt(d), text)
        flash("Logged.", "ok")
        return _back("course_detail", course_id=course_id)

    @app.route("/note/<int:note_id>/delete", methods=["POST"])
    @login_required
    def note_delete(note_id: int):
        conn = app.config["GET_CONN"]()
        row = conn.execute("SELECT course_id FROM progress_note WHERE id = ?",
                           (note_id,)).fetchone()
        db.delete_note(conn, note_id)
        if row is None:
            return _back("courses")
        # The proposal was worked out from a log that no longer says this.
        _clear_proposal(row["course_id"])
        return _back("course_detail", course_id=row["course_id"])

    @app.route("/course/<int:course_id>/resolve", methods=["POST"])
    @login_required
    def course_resolve(course_id: int):
        """Read the log against the handout and propose a state per topic.

        The answer is held in the session, not written: it is a reading of a
        log, and the owner corrects it before it counts.
        """
        conn = app.config["GET_CONN"]()
        course = db.get_course(conn, course_id)
        if course is None:
            abort(404)
        if not llm.configured():
            flash("No Gemini keys configured (GEMINI_API_KEYS).", "error")
            return _back("course_detail", course_id=course_id)

        topics = db.list_topics(conn, course_id)
        notes = db.list_notes(conn, course_id)
        try:
            out = llm.resolve_coverage(course["code"], course["title"],
                                       topics, notes)
        except llm.LLMError as e:
            flash(f"Could not work it out: {e}", "error")
            return _back("course_detail", course_id=course_id)

        states = dict(zip((t["id"] for t in topics), out["states"]))
        changed = sum(1 for t in topics if states[t["id"]] != t["status"])
        session[f"proposal_{course_id}"] = {
            "states": {str(k): v for k, v in states.items()},
            "summary": out["summary"],
            "unmatched": out["unmatched"],
            "changed": changed,
        }
        flash("Read the log against the handout — check it, then save.", "ok")
        return _back("course_detail", course_id=course_id)

    @app.route("/course/<int:course_id>/discard", methods=["POST"])
    @login_required
    def proposal_discard(course_id: int):
        _clear_proposal(course_id)
        flash("Proposal discarded.", "ok")
        return _back("course_detail", course_id=course_id)

    @app.route("/course/<int:course_id>/weigh", methods=["POST"])
    @login_required
    def course_weigh(course_id: int):
        """Weight the topics of a handout that gave no hours.

        Only ever runs on a course whose topics are all still at the default
        weight of 1 — a handout's own hours are fact and are never overwritten.
        """
        conn = app.config["GET_CONN"]()
        course = db.get_course(conn, course_id)
        if course is None:
            abort(404)
        if not llm.configured():
            flash("No Gemini keys configured (GEMINI_API_KEYS).", "error")
            return _back("course_detail", course_id=course_id)
        topics = db.list_topics(conn, course_id)
        if any((t["hours"] or 1) != 1 for t in topics):
            flash("This course already has hours from its handout — "
                  "not overwriting them.", "error")
            return _back("course_detail", course_id=course_id)
        total = _int(request.form.get("total_hours"))
        if not total:
            flash("Say how many lecture-hours the course has first.", "error")
            return _back("course_detail", course_id=course_id)

        try:
            hours = llm.estimate_hours(course["code"], course["title"],
                                       [t["title"] for t in topics], total)
        except llm.LLMError as e:
            flash(f"Could not weigh the topics: {e}", "error")
            return _back("course_detail", course_id=course_id)

        for t, h in zip(topics, hours):
            db.update_topic(conn, t["id"], hours=h)
        db.set_setting(conn, f"ai_hours_{course_id}", "1")
        flash(f"Weighted {len(hours)} topics across {total} hours. "
              f"That is a guess at how big each topic is, not the handout.",
              "ok")
        return _back("course_detail", course_id=course_id)

    @app.route("/topic/<int:topic_id>", methods=["POST"])
    @login_required
    def topic_update(topic_id: int):
        conn = app.config["GET_CONN"]()
        fields = {}
        for key in ("title", "unit", "status", "note"):
            if key in request.form:
                fields[key] = request.form[key].strip()
        hours = _int(request.form.get("hours"))
        if hours is not None:
            fields["hours"] = max(1, hours)
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
        payload_topics = entry.get("topics", [])
        weights = topic_weights(payload_topics)
        for t, hours in zip(payload_topics, weights):
            title = t if isinstance(t, str) else str(t.get("title", ""))
            unit = "" if isinstance(t, str) else str(t.get("unit", ""))
            key = title.strip().lower()
            if not key:
                continue
            existing = have_topics.get(key)
            if existing is None:
                tid = db.create_topic(conn, cid, title, unit, hours=hours)
                counts["topics"] += 1
                # A same-title line later in this payload must see the row
                # that now exists, not a placeholder it cannot update.
                have_topics[key] = {"id": tid, "hours": hours}
            elif hours != existing["hours"]:
                # Correcting the weights must not require deleting topics,
                # which would take their done/partial state with them.
                db.update_topic(conn, existing["id"], hours=hours)
                have_topics[key] = {"id": existing["id"], "hours": hours}

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

    A handout that writes its topics as "L9-14 Interfaces" is stating how many
    hours it gives them, which is all the app keeps -- see topic_hours(). The
    numbers themselves are not tracked: a lecture number is the handout's plan,
    not what happened in the room.

    Only a bare lecture reference is accepted. A title like 'L1.1 First order
    equations' deliberately does NOT match: dotted numbering is per-module, not
    a lecture count, and reading it as one silently mis-weights the syllabus.
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


def topic_weights(entries) -> list[int]:
    """How many lecture-hours the handout gives each topic, in order.

    Per topic the weight comes from an explicit "hours", else from a lecture
    range the handout stated ("L9-14" is six hours' worth of material) in the
    entry, the title or the unit, else 1 -- and then every topic weighs the
    same and the percentage is a plain topic count.

    Ranges are resolved across the whole list rather than one at a time,
    because a handout that quotes "L8-18" against three topics is splitting
    eleven hours between them, not giving each eleven. See sch.split_hours.
    """
    explicit, ranges = [], []
    for entry in entries:
        title = entry if isinstance(entry, str) else str(entry.get("title", ""))
        unit = "" if isinstance(entry, str) else str(entry.get("unit", ""))
        lo = hi = None
        h = None
        if isinstance(entry, dict):
            h = _int(entry.get("hours"))
            lo, hi = parse_lec_range(entry.get("lec", ""))
            lo = _int(entry.get("lec_from")) or lo
            hi = _int(entry.get("lec_to")) or hi
        if lo is None and hi is None:
            lo, hi = parse_lec_range(title)
            if lo is None:
                lo, hi = parse_lec_range(unit)
        explicit.append(max(1, h) if h else None)
        if lo is None and hi is None:
            ranges.append(None)
        else:
            lo = lo if lo is not None else hi
            hi = hi if hi is not None else lo
            ranges.append((lo, hi))
    shared = sch.split_hours(ranges)
    return [e if e is not None else shared[i] for i, e in enumerate(explicit)]


def _weekday_num(name: str) -> int | None:
    aliases = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Thr": 3, "Ths": 3,
               "Fri": 4, "Sat": 5, "Sun": 6}
    return aliases.get(name.strip()[:3].title())


app = create_app()

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "5000")),
            debug=os.environ.get("FLASK_DEBUG") == "1")
