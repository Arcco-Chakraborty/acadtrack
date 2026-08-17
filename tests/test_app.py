import os

import pytest

import app as acad
import db


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAILS", "owner@example.com")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    flask_app = acad.create_app(db_path=str(tmp_path / "t.db"))
    flask_app.config.update(TESTING=True, GOOGLE_CLIENT_ID="")
    with flask_app.test_client() as c:
        conn = db.connect(flask_app.config["DB_PATH"])
        user = db.get_or_create_user(conn, "owner@example.com", "Owner")
        conn.close()
        with c.session_transaction() as s:
            s["user_id"] = user["id"]
            s["csrf"] = "tok"
        c.app = flask_app
        yield c


def post(client, url, **form):
    form.setdefault("_csrf", "tok")
    return client.post(url, data=form, follow_redirects=True)


def conn_of(client):
    return db.connect(client.app.config["DB_PATH"])


# ------------------------------------------------------------------ access --

def test_signed_out_is_redirected_to_login(tmp_path):
    flask_app = acad.create_app(db_path=str(tmp_path / "x.db"))
    with flask_app.test_client() as c:
        r = c.get("/")
        assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_empty_allowlist_denies_everyone(tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAILS", "")
    flask_app = acad.create_app(db_path=str(tmp_path / "y.db"))
    with flask_app.app_context():
        from auth import _allowed
        assert _allowed("anyone@example.com") is False


def test_post_without_csrf_is_rejected(client):
    r = client.post("/courses", data={"code": "DEMO 204"})
    assert r.status_code == 400


# ----------------------------------------------------------------- courses --

def test_course_and_slot_generate_a_schedule(client):
    post(client, "/settings", semester_start="2054-08-03",
         semester_end="2054-08-31", semester_name="Test")
    post(client, "/courses", code="DEMO 204", title="DSA")

    conn = conn_of(client)
    course = db.get_course_by_code(conn, "DEMO 204")
    assert course is not None

    post(client, f"/course/{course['id']}/slots", weekday="0",
         start_time="09:00", end_time="09:50", kind="L")

    lectures = db.list_lectures(conn, course["id"])
    assert [l["date"] for l in lectures] == ["2054-08-03", "2054-08-10",
                                             "2054-08-17", "2054-08-24",
                                             "2054-08-31"]
    conn.close()


def test_regenerate_is_idempotent_and_keeps_marks(client):
    post(client, "/settings", semester_start="2054-08-03", semester_end="2054-08-31")
    post(client, "/courses", code="DEMO 213")
    conn = conn_of(client)
    cid = db.get_course_by_code(conn, "DEMO 213")["id"]
    post(client, f"/course/{cid}/slots", weekday="1", start_time="10:00",
         end_time="10:50")

    first = db.list_lectures(conn, cid)
    marked = first[0]
    post(client, f"/lecture/{marked['id']}/status", status="skipped")

    post(client, "/schedule/regenerate")
    again = db.list_lectures(conn, cid)
    assert len(again) == len(first)                       # no duplicates
    assert db.get_lecture(conn, marked["id"])["status"] == "skipped"
    conn.close()


def test_holiday_removes_that_days_classes(client):
    post(client, "/settings", semester_start="2054-08-03", semester_end="2054-08-31")
    post(client, "/courses", code="DEMO 215")
    conn = conn_of(client)
    cid = db.get_course_by_code(conn, "DEMO 215")["id"]
    post(client, f"/course/{cid}/slots", weekday="0", start_time="11:00",
         end_time="11:50")
    assert any(l["date"] == "2054-08-17" for l in db.list_lectures(conn, cid))

    post(client, "/holidays", date="2054-08-17", label="Break")
    dates = [l["date"] for l in db.list_lectures(conn, cid)]
    assert "2054-08-17" not in dates
    assert "2054-08-24" in dates
    conn.close()


# ------------------------------------------------------------------ topics --

def test_pasted_topics_split_into_unit_and_title(client):
    post(client, "/courses", code="DEMO 205")
    conn = conn_of(client)
    cid = db.get_course_by_code(conn, "DEMO 205")["id"]
    post(client, f"/course/{cid}/topics",
         titles="Unit 1: Sets\n- Unit 1: Relations\n\nUnit 2: Groups")

    topics = db.list_topics(conn, cid)
    assert [(t["unit"], t["title"]) for t in topics] == [
        ("Unit 1", "Sets"), ("Unit 1", "Relations"), ("Unit 2", "Groups")]
    conn.close()


def test_marking_a_lecture_covered_completes_the_topic(client):
    post(client, "/settings", semester_start="2054-08-03", semester_end="2054-08-31")
    post(client, "/courses", code="DEMO 206")
    conn = conn_of(client)
    cid = db.get_course_by_code(conn, "DEMO 206")["id"]
    post(client, f"/course/{cid}/slots", weekday="0", start_time="08:00",
         end_time="08:50")
    post(client, f"/course/{cid}/topics", titles="Inheritance")

    lec = db.list_lectures(conn, cid)[0]
    topic = db.list_topics(conn, cid)[0]
    post(client, f"/lecture/{lec['id']}/topics", topic_id=str(topic["id"]))

    assert db.list_topics(conn, cid)[0]["status"] == "done"
    last = db.last_covered(conn, cid)
    assert last["title"] == "Inheritance"
    conn.close()


# ------------------------------------------------------------------ import --

def test_timetable_import_matches_by_code_and_does_not_duplicate(client):
    payload = {
        "semester": {"start": "2054-08-03", "end": "2054-08-31", "name": "S1"},
        "courses": [{
            "code": "DEMO 201", "title": "Workshop", "credits": 2,
            "slots": [{"weekday": "Tue", "start": "14:00", "end": "16:50",
                       "kind": "P", "room": "WS"}],
            "topics": ["Fitting", "Welding"],
        }],
    }
    import json
    post(client, "/timetable/import", payload=json.dumps(payload))
    conn = conn_of(client)
    course = db.get_course_by_code(conn, "DEMO 201")
    assert course["title"] == "Workshop"
    assert len(db.list_slots(conn, course["id"])) == 1
    # Tuesdays in 3-31 Aug 2054: 4th, 11th, 18th, 25th.
    assert len(db.list_lectures(conn, course["id"])) == 4

    # Same payload again: nothing should double up.
    post(client, "/timetable/import", payload=json.dumps(payload))
    assert len(db.list_courses(conn)) == 1
    assert len(db.list_slots(conn, course["id"])) == 1
    assert len(db.list_lectures(conn, course["id"])) == 4
    conn.close()


def test_import_loads_holidays_and_never_duplicates_topics(client):
    """Re-importing a corrected syllabus must update, not append a second copy
    of every topic — the same guarantee courses and slots already had."""
    import json
    payload = {
        "semester": {"start": "2054-08-03", "end": "2054-08-31"},
        "holidays": [
            {"date": "2054-08-17", "label": "One day"},
            {"from": "2054-08-24", "to": "2054-08-26", "label": "A whole week"},
        ],
        "courses": [{
            "code": "DEMO 206",
            "slots": [{"weekday": "Mon", "start": "09:00", "end": "09:50"}],
            "topics": [{"unit": "M1", "title": "Objects"}, "Inheritance"],
        }],
    }
    post(client, "/timetable/import", payload=json.dumps(payload))
    conn = conn_of(client)
    cid = db.get_course_by_code(conn, "DEMO 206")["id"]

    assert len(db.list_holidays(conn)) == 4          # 1 single + 3 from range
    assert len(db.list_topics(conn, cid)) == 2
    dates = [l["date"] for l in db.list_lectures(conn, cid)]
    assert "2054-08-17" not in dates                 # holiday cleared
    assert "2054-08-24" not in dates                 # inside the range
    assert "2054-08-03" in dates and "2054-08-10" in dates

    post(client, "/timetable/import", payload=json.dumps(payload))
    assert len(db.list_topics(conn, cid)) == 2       # not 4
    assert len(db.list_lectures(conn, cid)) == len(dates)
    conn.close()


def test_shortening_the_semester_retracts_later_classes(client):
    post(client, "/settings", semester_start="2054-08-03", semester_end="2054-08-31")
    post(client, "/courses", code="DEMO 208")
    conn = conn_of(client)
    cid = db.get_course_by_code(conn, "DEMO 208")["id"]
    post(client, f"/course/{cid}/slots", weekday="0", start_time="12:00",
         end_time="12:50")
    assert len(db.list_lectures(conn, cid)) == 5      # 5 Mondays

    post(client, "/settings", semester_start="2054-08-03", semester_end="2054-08-17")
    dates = [l["date"] for l in db.list_lectures(conn, cid)]
    assert dates == ["2054-08-03", "2054-08-10", "2054-08-17"]
    conn.close()


def test_watermark_ticks_topics_and_survives_a_reimport(client):
    """The whole point of the redesign: set 'done till lecture N' once and the
    topics follow, without touching a single class."""
    import json
    payload = {
        "semester": {"start": "2054-08-03", "end": "2054-11-27"},
        "courses": [{
            "code": "DEMO 206", "lectures_planned": 42,
            "topics": [
                {"title": "L1-2 Basics", "lec": "1-2"},
                {"title": "L3 Syntax", "lec": "3"},
                {"title": "L4-8 Classes", "lec": "4-8"},
                {"title": "Unnumbered extra"},
            ],
        }],
    }
    post(client, "/timetable/import", payload=json.dumps(payload))
    conn = conn_of(client)
    cid = db.get_course_by_code(conn, "DEMO 206")["id"]
    topics = db.list_topics(conn, cid)
    assert [(t["lec_from"], t["lec_to"]) for t in topics] == [
        (1, 2), (3, 3), (4, 8), (None, None)]

    # What lecture 5 implies, before committing anything.
    r = client.get(f"/course/{cid}/suggest?n=5")
    body = r.get_json()
    assert body["done"] == [topics[0]["id"], topics[1]["id"]]
    assert body["partial"] == [topics[2]["id"]]
    assert body["unnumbered"] == [topics[3]["id"]]

    post(client, f"/course/{cid}/progress", lectures_done="5",
         topics_submitted="1",
         done=[str(topics[0]["id"]), str(topics[1]["id"])],
         partial=str(topics[2]["id"]))

    assert db.get_course(conn, cid)["lectures_done"] == 5
    after = {t["title"]: t["status"] for t in db.list_topics(conn, cid)}
    assert after["L1-2 Basics"] == "done"
    assert after["L4-8 Classes"] == "partial"
    assert after["Unnumbered extra"] == "pending"

    # Re-importing the corrected syllabus must not reset that progress.
    post(client, "/timetable/import", payload=json.dumps(payload))
    assert db.get_course(conn, cid)["lectures_done"] == 5
    assert {t["title"]: t["status"] for t in db.list_topics(conn, cid)} == after
    conn.close()


def test_strip_lec_removes_only_a_leading_lecture_marker():
    from app import strip_lec
    assert strip_lec("L9-14 Abstract classes") == "Abstract classes"
    assert strip_lec("L3 Java syntax") == "Java syntax"
    assert strip_lec("L1–2 Basics") == "Basics"            # en dash
    # Must not eat content that merely starts with a letter+digits, and must
    # never return an empty string.
    assert strip_lec("Laplace transform") == "Laplace transform"
    assert strip_lec("L1.1 First order") == "L1.1 First order"
    assert strip_lec("Trees and traversal") == "Trees and traversal"
    assert strip_lec("") == ""


def test_progress_without_topics_submitted_only_moves_the_watermark(client):
    """Posting just the number must not silently clear every topic."""
    import json
    post(client, "/timetable/import", payload=json.dumps({
        "courses": [{"code": "DEMO 209",
                     "topics": [{"title": "L1 Sets", "lec": "1"}]}]}))
    conn = conn_of(client)
    cid = db.get_course_by_code(conn, "DEMO 209")["id"]
    tid = db.list_topics(conn, cid)[0]["id"]
    db.update_topic(conn, tid, status="done")

    post(client, f"/course/{cid}/progress", lectures_done="7")
    assert db.get_course(conn, cid)["lectures_done"] == 7
    assert db.list_topics(conn, cid)[0]["status"] == "done"   # untouched
    conn.close()


def test_import_rejects_bad_json_without_500(client):
    r = post(client, "/timetable/import", payload="{not json")
    assert r.status_code == 200
    assert b"not valid JSON" in r.data


# ------------------------------------------------------------------- pages --

def test_every_page_renders(client):
    post(client, "/settings", semester_start="2054-08-03", semester_end="2054-12-05")
    post(client, "/courses", code="DEMO 204", title="DSA")
    conn = conn_of(client)
    cid = db.get_course_by_code(conn, "DEMO 204")["id"]
    post(client, f"/course/{cid}/slots", weekday="0", start_time="09:00",
         end_time="09:50")
    post(client, f"/course/{cid}/topics", titles="Unit 1: Asymptotics")
    conn.close()

    for url in ["/", "/week", "/courses", f"/course/{cid}", "/timetable",
                "/settings", "/healthz"]:
        r = client.get(url)
        assert r.status_code == 200, f"{url} returned {r.status_code}"
