"""seed_timetable.py — load a timetable JSON straight into the database.

The same importer the /timetable page uses, on the command line. This is how a
timetable transcribed from a photo gets in without any of it being typed twice.

    python seed_timetable.py timetable.json            # add / update
    python seed_timetable.py timetable.json --replace  # also drop old slots
                                                       # on matched courses

Shape:

    {
      "semester": {"name": "...", "start": "YYYY-MM-DD", "end": "YYYY-MM-DD"},
      "courses": [
        {"code": "DEMO 204", "title": "...", "instructor": "", "credits": 4,
         "color": "#7aa2f7",
         "slots": [{"weekday": "Mon", "start": "09:00", "end": "09:50",
                    "kind": "L", "room": "LT-1", "label": "T3"}],
         "topics": ["Unit 1: ...", "Unit 1: ..."]}
      ]
    }

weekday accepts 0-6 (Mon-Sun) or a name. kind is L / T / P.

A top-level "dropped": ["DEMO 210", ...] archives courses you no longer take:
their history is kept and the archive is reversible, but they disappear from
the week, the dashboard and the timetable. A course listed under "courses" is
un-archived, so re-taking one just means putting it back in the file.
"""
from __future__ import annotations

import json
import sys

import app as acad
import db
import schedule as sch


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    replace = "--replace" in argv
    if len(args) != 1:
        print(__doc__)
        return 2

    with open(args[0], encoding="utf-8") as f:
        payload = json.load(f)

    flask_app = acad.create_app()
    with flask_app.app_context():
        conn = db.connect(flask_app.config["DB_PATH"])
        db.init(conn)
        counts = acad.import_timetable(conn, payload, replace=replace)
        start = db.get_setting(conn, "semester_start")
        end = db.get_setting(conn, "semester_end")
        before = len(db.list_lectures(conn))
        # Deliberately the same regenerate() the web app uses, rather than a
        # second copy of the logic: it is the one place that also retracts
        # classes falling on a holiday or outside a shortened semester.
        acad.regenerate(conn, from_date=sch.parse_date(start) if start else None)
        after = len(db.list_lectures(conn))

        print(f"courses added : {counts['courses']}")
        print(f"slots added   : {counts['slots']}")
        print(f"holidays      : {counts['holidays']}")
        print(f"topics added  : {counts['topics']}")
        print(f"exams         : {counts['exams']}")
        print(f"dropped       : {counts['dropped']}")
        print(f"classes       : {before} -> {after}")
        if not (start and end):
            print("NOTE: no semester start/end set, so no classes were "
                  "generated. Set them in Settings and hit Rebuild.")
        for c in db.list_courses(conn):
            n = len(db.list_slots(conn, c['id']))
            t = len(db.list_topics(conn, c['id']))
            print(f"  {c['code']:<12} {n} slots, {t} topics")
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
