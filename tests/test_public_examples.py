"""The published fictional examples must form a usable, consistent demo."""
import json
from datetime import date
from pathlib import Path

import app as acad
import db


def test_synthetic_imports_combine_without_duplicate_courses(tmp_path):
    root = Path(__file__).resolve().parents[1]
    timetable = json.loads((root / "timetable.json").read_text(encoding="utf-8"))
    semester = json.loads((root / "semester.json").read_text(encoding="utf-8"))
    assert timetable["_example"] and semester["_example"]
    assert timetable["semester"] == semester["semester"]
    assert {c["code"] for c in timetable["courses"]} == {
        c["code"] for c in semester["courses"]
    }
    assert all(c["code"].startswith("DEMO ") for c in timetable["courses"])

    conn = db.connect(str(tmp_path / "demo.db"))
    try:
        db.init(conn)
        first = acad.import_timetable(conn, timetable)
        second = acad.import_timetable(conn, semester)
        assert first["courses"] == 2 and first["slots"] == 2
        assert second["courses"] == 0
        assert second["holidays"] == 1 and second["topics"] == 3
        assert second["exams"] == 2
        acad.regenerate(conn, from_date=date(2030, 1, 7))
        lectures = db.list_lectures(conn)
        assert len(lectures) == 7
        assert all(row["date"] != "2030-01-21" for row in lectures)
        assert len(db.list_courses(conn)) == 2
        assert len(db.list_exams(conn)) == 2

        # Reimporting both examples must retain the same courses and classes.
        acad.import_timetable(conn, timetable)
        acad.import_timetable(conn, semester)
        acad.regenerate(conn, from_date=date(2030, 1, 7))
        assert len(db.list_courses(conn)) == 2
        assert len(db.list_lectures(conn)) == 7
        assert len(db.list_exams(conn)) == 2
    finally:
        conn.close()
