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
"""
from __future__ import annotations

import json
import sys

import app as acad
import db


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
        made = 0
        if start and end:
            import schedule as sch
            holidays = {h["date"] for h in db.list_holidays(conn)}
            s, e = sch.parse_date(start), sch.parse_date(end)
            for course in db.list_courses(conn):
                for slot in db.list_slots(conn, course["id"]):
                    for d in sch.occurrence_dates(slot["weekday"], s, e, holidays):
                        if db.create_lecture(conn, course["id"], sch.fmt(d),
                                             slot["start_time"], slot["end_time"],
                                             slot["kind"], slot["id"]) is not None:
                            made += 1

        print(f"courses added : {counts['courses']}")
        print(f"slots added   : {counts['slots']}")
        print(f"classes made  : {made}")
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
