"""db.py — SQLite schema and queries.

Single-tenant on purpose: there is no user_id on course/topic/lecture. Access
is gated entirely by the ALLOWED_EMAILS allowlist in auth.py, and both of the
owner's addresses map to the same data.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS user (
  id         INTEGER PRIMARY KEY,
  email      TEXT UNIQUE NOT NULL,
  name       TEXT NOT NULL DEFAULT '',
  picture    TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  last_seen  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS setting (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- lectures_planned: how many lectures the handout's course plan has.
-- lectures_done:    the watermark — "I have been taught up to lecture N".
CREATE TABLE IF NOT EXISTS course (
  id               INTEGER PRIMARY KEY,
  code             TEXT NOT NULL,
  title            TEXT NOT NULL DEFAULT '',
  instructor       TEXT NOT NULL DEFAULT '',
  credits          INTEGER,
  color            TEXT NOT NULL DEFAULT '',
  position         INTEGER NOT NULL DEFAULT 0,
  archived         INTEGER NOT NULL DEFAULT 0,
  created_at       TEXT NOT NULL,
  lectures_planned INTEGER,
  lectures_done    INTEGER NOT NULL DEFAULT 0
);

-- One weekly recurring meeting. weekday: 0=Mon .. 6=Sun.
CREATE TABLE IF NOT EXISTS slot (
  id         INTEGER PRIMARY KEY,
  course_id  INTEGER NOT NULL REFERENCES course(id) ON DELETE CASCADE,
  kind       TEXT NOT NULL DEFAULT 'L',      -- L lecture / T tutorial / P practical
  weekday    INTEGER NOT NULL,
  start_time TEXT NOT NULL,                  -- 'HH:MM'
  end_time   TEXT NOT NULL,
  room       TEXT NOT NULL DEFAULT '',
  label      TEXT NOT NULL DEFAULT ''        -- BITS slot code, e.g. 'T3'
);
CREATE INDEX IF NOT EXISTS slot_course_idx ON slot(course_id);

CREATE TABLE IF NOT EXISTS holiday (
  id    INTEGER PRIMARY KEY,
  date  TEXT UNIQUE NOT NULL,
  label TEXT NOT NULL DEFAULT ''
);

-- One exam sitting for a course. `session` is whatever the institute timetable
-- says (FN1, AN, a time) -- it is displayed, never parsed.
CREATE TABLE IF NOT EXISTS exam (
  id        INTEGER PRIMARY KEY,
  course_id INTEGER NOT NULL REFERENCES course(id) ON DELETE CASCADE,
  label     TEXT NOT NULL,                 -- 'Midsem', 'Compre', 'Quiz 1'
  date      TEXT NOT NULL,                 -- 'YYYY-MM-DD'
  session   TEXT NOT NULL DEFAULT '',
  UNIQUE (course_id, label)
);

-- A materialised occurrence of a slot on a date. Rows are generated from
-- slot x semester dates and carry the status, so regeneration must never
-- destroy one that has been marked.
CREATE TABLE IF NOT EXISTS lecture (
  id         INTEGER PRIMARY KEY,
  course_id  INTEGER NOT NULL REFERENCES course(id) ON DELETE CASCADE,
  slot_id    INTEGER REFERENCES slot(id) ON DELETE SET NULL,
  date       TEXT NOT NULL,                  -- 'YYYY-MM-DD'
  start_time TEXT NOT NULL,
  end_time   TEXT NOT NULL,
  kind       TEXT NOT NULL DEFAULT 'L',
  status     TEXT NOT NULL DEFAULT 'scheduled',  -- scheduled|attended|skipped|cancelled
  note       TEXT NOT NULL DEFAULT '',
  UNIQUE (course_id, date, start_time, kind)
);
CREATE INDEX IF NOT EXISTS lecture_date_idx ON lecture(date);
CREATE INDEX IF NOT EXISTS lecture_course_idx ON lecture(course_id, date);

-- lec_from / lec_to are the handout's own lecture numbering ("L9-14" -> 9, 14).
-- They are what lets "I'm done till lecture N" resolve to a set of topics
-- without anyone tagging lectures by hand.
CREATE TABLE IF NOT EXISTS topic (
  id        INTEGER PRIMARY KEY,
  course_id INTEGER NOT NULL REFERENCES course(id) ON DELETE CASCADE,
  title     TEXT NOT NULL,
  unit      TEXT NOT NULL DEFAULT '',
  position  INTEGER NOT NULL DEFAULT 0,
  status    TEXT NOT NULL DEFAULT 'pending',   -- pending|partial|done
  note      TEXT NOT NULL DEFAULT '',
  lec_from  INTEGER,
  lec_to    INTEGER
);
CREATE INDEX IF NOT EXISTS topic_course_idx ON topic(course_id, position);

CREATE TABLE IF NOT EXISTS lecture_topic (
  lecture_id INTEGER NOT NULL REFERENCES lecture(id) ON DELETE CASCADE,
  topic_id   INTEGER NOT NULL REFERENCES topic(id) ON DELETE CASCADE,
  PRIMARY KEY (lecture_id, topic_id)
);
"""

DEFAULT_SETTINGS = {
    "semester_start": "",
    "semester_end": "",
    "semester_name": "",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    """Open (and if needed create) the database. Callers own the connection."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, detect_types=0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# Columns added after the first release. SQLite has no "ADD COLUMN IF NOT
# EXISTS", so they are applied by inspecting the table.
MIGRATIONS = [
    ("topic", "lec_from", "INTEGER"),
    ("topic", "lec_to", "INTEGER"),
    ("course", "lectures_planned", "INTEGER"),
    ("course", "lectures_done", "INTEGER NOT NULL DEFAULT 0"),
]


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for table, column, decl in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO setting (key, value) VALUES (?, ?)",
                     (k, v))
    conn.commit()


# --------------------------------------------------------------- settings --

def get_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO setting (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    conn.commit()


def all_settings(conn) -> dict:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM setting")}


# ------------------------------------------------------------------ users --

def get_user(conn, user_id: int):
    return conn.execute("SELECT * FROM user WHERE id = ?", (user_id,)).fetchone()


def get_or_create_user(conn, email: str, name: str = "", picture: str = ""):
    email = email.strip().lower()
    row = conn.execute("SELECT * FROM user WHERE email = ?", (email,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO user (email, name, picture, created_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?)", (email, name, picture, _now(), _now()))
        conn.commit()
        row = conn.execute("SELECT * FROM user WHERE email = ?", (email,)).fetchone()
    else:
        conn.execute(
            "UPDATE user SET name = ?, picture = ?, last_seen = ? WHERE id = ?",
            (name or row["name"], picture or row["picture"], _now(), row["id"]))
        conn.commit()
    return row


# ---------------------------------------------------------------- courses --

def list_courses(conn, include_archived: bool = False):
    q = "SELECT * FROM course"
    if not include_archived:
        q += " WHERE archived = 0"
    q += " ORDER BY position, code"
    return conn.execute(q).fetchall()


def get_course(conn, course_id: int):
    return conn.execute("SELECT * FROM course WHERE id = ?", (course_id,)).fetchone()


def get_course_by_code(conn, code: str):
    return conn.execute("SELECT * FROM course WHERE code = ? COLLATE NOCASE",
                        (code.strip(),)).fetchone()


def create_course(conn, code: str, title: str = "", instructor: str = "",
                  credits=None, color: str = "", position: int = 0) -> int:
    cur = conn.execute(
        "INSERT INTO course (code, title, instructor, credits, color, position, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (code.strip(), title.strip(), instructor.strip(), credits, color,
         position, _now()))
    conn.commit()
    return cur.lastrowid


def update_course(conn, course_id: int, **fields) -> None:
    allowed = {"code", "title", "instructor", "credits", "color", "position",
               "archived", "lectures_planned", "lectures_done"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    cols = ", ".join(f"{k} = ?" for k in sets)
    conn.execute(f"UPDATE course SET {cols} WHERE id = ?",
                 (*sets.values(), course_id))
    conn.commit()


def delete_course(conn, course_id: int) -> None:
    conn.execute("DELETE FROM course WHERE id = ?", (course_id,))
    conn.commit()


# ------------------------------------------------------------------ slots --

def list_slots(conn, course_id: int | None = None):
    if course_id is None:
        return conn.execute(
            "SELECT s.*, c.code, c.title, c.color FROM slot s "
            "JOIN course c ON c.id = s.course_id WHERE c.archived = 0 "
            "ORDER BY s.weekday, s.start_time").fetchall()
    return conn.execute(
        "SELECT * FROM slot WHERE course_id = ? ORDER BY weekday, start_time",
        (course_id,)).fetchall()


def create_slot(conn, course_id: int, weekday: int, start_time: str,
                end_time: str, kind: str = "L", room: str = "",
                label: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO slot (course_id, kind, weekday, start_time, end_time, "
        "room, label) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (course_id, kind, int(weekday), start_time, end_time, room, label))
    conn.commit()
    return cur.lastrowid


def delete_slot(conn, slot_id: int) -> None:
    conn.execute("DELETE FROM slot WHERE id = ?", (slot_id,))
    conn.commit()


# --------------------------------------------------------------- holidays --

def list_holidays(conn):
    return conn.execute("SELECT * FROM holiday ORDER BY date").fetchall()


def add_holiday(conn, date: str, label: str = "") -> None:
    conn.execute("INSERT OR REPLACE INTO holiday (date, label) VALUES (?, ?)",
                 (date, label))
    conn.commit()


def delete_holiday(conn, holiday_id: int) -> None:
    conn.execute("DELETE FROM holiday WHERE id = ?", (holiday_id,))
    conn.commit()


# ------------------------------------------------------------------ exams --

def list_exams(conn, course_id: int | None = None):
    if course_id is not None:
        return conn.execute(
            "SELECT e.*, c.code, c.color FROM exam e "
            "JOIN course c ON c.id = e.course_id WHERE e.course_id = ? "
            "ORDER BY e.date, e.session", (course_id,)).fetchall()
    return conn.execute(
        "SELECT e.*, c.code, c.color FROM exam e "
        "JOIN course c ON c.id = e.course_id WHERE c.archived = 0 "
        "ORDER BY e.date, e.session, c.code").fetchall()


def upsert_exam(conn, course_id: int, label: str, date: str,
                session: str = "") -> None:
    """One row per (course, label): re-importing corrected dates updates in
    place, the same guarantee slots and topics already have."""
    conn.execute(
        "INSERT INTO exam (course_id, label, date, session) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(course_id, label) DO UPDATE SET "
        "date = excluded.date, session = excluded.session",
        (course_id, label.strip(), date, session.strip()))
    conn.commit()


def delete_exam(conn, exam_id: int) -> None:
    conn.execute("DELETE FROM exam WHERE id = ?", (exam_id,))
    conn.commit()


# --------------------------------------------------------------- lectures --

def list_lectures(conn, course_id: int | None = None, start: str | None = None,
                  end: str | None = None):
    q = ("SELECT l.*, c.code, c.title, c.color, s.room AS room, "
         "s.label AS slot_label FROM lecture l "
         "JOIN course c ON c.id = l.course_id "
         "LEFT JOIN slot s ON s.id = l.slot_id WHERE 1=1")
    args: list = []
    if course_id is not None:
        q += " AND l.course_id = ?"
        args.append(course_id)
    else:
        # A dropped (archived) course keeps its rows so nothing is lost, but
        # its classes must not turn up in the week or on the dashboard. Asking
        # for one course by id still returns them, so its own page still works.
        q += " AND c.archived = 0"
    if start:
        q += " AND l.date >= ?"
        args.append(start)
    if end:
        q += " AND l.date <= ?"
        args.append(end)
    q += " ORDER BY l.date, l.start_time"
    return conn.execute(q, args).fetchall()


def get_lecture(conn, lecture_id: int):
    return conn.execute(
        "SELECT l.*, c.code, c.title FROM lecture l "
        "JOIN course c ON c.id = l.course_id WHERE l.id = ?",
        (lecture_id,)).fetchone()


def set_lecture_status(conn, lecture_id: int, status: str) -> None:
    conn.execute("UPDATE lecture SET status = ? WHERE id = ?",
                 (status, lecture_id))
    conn.commit()


def set_lecture_note(conn, lecture_id: int, note: str) -> None:
    conn.execute("UPDATE lecture SET note = ? WHERE id = ?", (note, lecture_id))
    conn.commit()


def create_lecture(conn, course_id: int, date: str, start_time: str,
                   end_time: str, kind: str = "L", slot_id=None,
                   status: str = "scheduled") -> int | None:
    """Insert one occurrence. Returns None if it already exists (the UNIQUE
    key is what makes schedule regeneration idempotent)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO lecture (course_id, slot_id, date, start_time, "
        "end_time, kind, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (course_id, slot_id, date, start_time, end_time, kind, status))
    conn.commit()
    return cur.lastrowid if cur.rowcount else None


def delete_lecture(conn, lecture_id: int) -> None:
    conn.execute("DELETE FROM lecture WHERE id = ?", (lecture_id,))
    conn.commit()


def delete_untouched_lectures(conn, course_id: int, on_or_after: str) -> int:
    """Drop still-'scheduled' future occurrences so a timetable edit can be
    re-materialised. Anything marked attended/skipped/cancelled survives, and
    so does a manually added one-off class (slot_id IS NULL) -- it was never
    generated from the weekly pattern, so regeneration must not eat it."""
    cur = conn.execute(
        "DELETE FROM lecture WHERE course_id = ? AND date >= ? "
        "AND status = 'scheduled' AND slot_id IS NOT NULL "
        "AND id NOT IN (SELECT lecture_id FROM lecture_topic)",
        (course_id, on_or_after))
    conn.commit()
    return cur.rowcount


# ----------------------------------------------------------------- topics --

def list_topics(conn, course_id: int):
    return conn.execute(
        "SELECT * FROM topic WHERE course_id = ? ORDER BY position, id",
        (course_id,)).fetchall()


def create_topic(conn, course_id: int, title: str, unit: str = "",
                 position: int | None = None, lec_from=None, lec_to=None) -> int:
    if position is None:
        row = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS p FROM topic "
            "WHERE course_id = ?", (course_id,)).fetchone()
        position = row["p"]
    cur = conn.execute(
        "INSERT INTO topic (course_id, title, unit, position, lec_from, lec_to) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (course_id, title.strip(), unit.strip(), position, lec_from, lec_to))
    conn.commit()
    return cur.lastrowid


def set_topic_statuses(conn, course_id: int, done_ids: set[int],
                       partial_ids: set[int] | None = None) -> None:
    """Rewrite every topic status for one course in a single transaction.

    This is what the "done till lecture N" form posts: the whole checkbox
    state, not a diff. Anything not named goes back to pending, so unticking
    is as easy as ticking.
    """
    partial_ids = partial_ids or set()
    with conn:
        for t in list_topics(conn, course_id):
            if t["id"] in done_ids:
                status = "done"
            elif t["id"] in partial_ids:
                status = "partial"
            else:
                status = "pending"
            if status != t["status"]:
                conn.execute("UPDATE topic SET status = ? WHERE id = ?",
                             (status, t["id"]))


def update_topic(conn, topic_id: int, **fields) -> None:
    allowed = {"title", "unit", "position", "status", "note", "lec_from",
               "lec_to"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    cols = ", ".join(f"{k} = ?" for k in sets)
    conn.execute(f"UPDATE topic SET {cols} WHERE id = ?",
                 (*sets.values(), topic_id))
    conn.commit()


def delete_topic(conn, topic_id: int) -> None:
    conn.execute("DELETE FROM topic WHERE id = ?", (topic_id,))
    conn.commit()


def link_topic(conn, lecture_id: int, topic_id: int) -> None:
    conn.execute("INSERT OR IGNORE INTO lecture_topic (lecture_id, topic_id) "
                 "VALUES (?, ?)", (lecture_id, topic_id))
    conn.commit()


def unlink_topic(conn, lecture_id: int, topic_id: int) -> None:
    conn.execute("DELETE FROM lecture_topic WHERE lecture_id = ? AND topic_id = ?",
                 (lecture_id, topic_id))
    conn.commit()


def topics_for_lectures(conn, lecture_ids: list[int]) -> dict[int, list]:
    """{lecture_id: [topic rows]} for a batch of lectures."""
    if not lecture_ids:
        return {}
    marks = ",".join("?" * len(lecture_ids))
    rows = conn.execute(
        f"SELECT lt.lecture_id, t.* FROM lecture_topic lt "
        f"JOIN topic t ON t.id = lt.topic_id "
        f"WHERE lt.lecture_id IN ({marks}) ORDER BY t.position", lecture_ids)
    out: dict[int, list] = {}
    for r in rows:
        out.setdefault(r["lecture_id"], []).append(r)
    return out


def last_covered(conn, course_id: int):
    """The most recent (lecture, topic) pair actually taught — this is what
    'till where I have done' reports."""
    return conn.execute(
        "SELECT l.date, l.id AS lecture_id, t.title, t.unit, t.id AS topic_id "
        "FROM lecture_topic lt "
        "JOIN lecture l ON l.id = lt.lecture_id "
        "JOIN topic t ON t.id = lt.topic_id "
        "WHERE l.course_id = ? AND l.status != 'cancelled' "
        "ORDER BY l.date DESC, l.start_time DESC, t.position DESC LIMIT 1",
        (course_id,)).fetchone()
