"""SQLite 持久化层。

资源（车辆、器材、志愿者、客房）都是"有期限的资源"：
active_until / credential_until 之后不再参与排程。

同一资源在同一时间窗只承诺给一支队伍/一个人，靠 assignments 表的
重叠窗口查询 + 事务保证；无障碍车辆的双承诺禁止在引擎层额外强制。
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


SCHEMA = """
CREATE TABLE IF NOT EXISTS commands (
    message_id   TEXT PRIMARY KEY,
    command_type TEXT NOT NULL,
    payload      TEXT NOT NULL,
    response     TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS teams (
    team_id TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    city    TEXT NOT NULL,
    sport   TEXT NOT NULL,
    leader  TEXT
);

CREATE TABLE IF NOT EXISTS persons (
    person_id             TEXT PRIMARY KEY,
    name                  TEXT NOT NULL,
    team_id               TEXT,
    city                  TEXT NOT NULL,
    category              TEXT,
    needs_json           TEXT NOT NULL DEFAULT '[]',
    status                TEXT NOT NULL DEFAULT '待报到',
    classification_status TEXT NOT NULL DEFAULT 'pending',
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS venues (
    venue_id    TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    city        TEXT NOT NULL,
    features_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS hotels (
    hotel_id TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    city     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
    room_id     TEXT PRIMARY KEY,
    hotel_id    TEXT NOT NULL,
    features_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id  TEXT PRIMARY KEY,
    city        TEXT NOT NULL,
    kind        TEXT NOT NULL,            -- accessible | commuter
    features_json TEXT NOT NULL DEFAULT '[]',
    seats       INTEGER NOT NULL DEFAULT 1,
    active_until TEXT,                    -- 有期限资源；过期不可排
    service_status TEXT NOT NULL DEFAULT 'in_service'  -- in_service | broken
);

CREATE TABLE IF NOT EXISTS equipment (
    item_id      TEXT PRIMARY KEY,
    city         TEXT NOT NULL,
    eq_type      TEXT NOT NULL,
    active_until TEXT,
    service_status TEXT NOT NULL DEFAULT 'in_service'
);

CREATE TABLE IF NOT EXISTS volunteers (
    volunteer_id    TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    city            TEXT NOT NULL,
    skills_json     TEXT NOT NULL DEFAULT '[]',
    credential_until TEXT
);

CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    venue_id        TEXT NOT NULL,
    city            TEXT NOT NULL,
    sport           TEXT NOT NULL,
    stage           TEXT NOT NULL,         -- training | competition
    title           TEXT NOT NULL,
    scheduled_start TEXT NOT NULL,
    scheduled_end   TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'on_schedule',  -- on_schedule | delayed | finished | cancelled
    delay_minutes   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS team_events (
    team_id  TEXT NOT NULL,
    event_id TEXT NOT NULL,
    PRIMARY KEY (team_id, event_id)
);

CREATE TABLE IF NOT EXISTS segments (
    segment_id    TEXT PRIMARY KEY,
    person_id     TEXT,                    -- 队伍级段可为空
    team_id       TEXT NOT NULL,
    event_id      TEXT,                    -- 训练/比赛段关联的赛程
    kind          TEXT NOT NULL,           -- checkin|classification|training|competition|lodging|transfer
    city          TEXT NOT NULL,
    venue_id      TEXT,
    hotel_id      TEXT,
    planned_start TEXT NOT NULL,
    planned_end   TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'planned',  -- planned|covered|gap|cancelled
    version       INTEGER NOT NULL DEFAULT 1,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assignments (
    assignment_id   TEXT PRIMARY KEY,
    segment_id      TEXT NOT NULL,
    resource_kind   TEXT NOT NULL,         -- vehicle|room|equipment|volunteer
    resource_id     TEXT NOT NULL,
    city            TEXT NOT NULL,
    window_start    TEXT NOT NULL,
    window_end      TEXT NOT NULL,
    seats_taken     INTEGER NOT NULL DEFAULT 1,
    team_id         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'scheduled', -- scheduled|fulfilled|released
    medical_override INTEGER NOT NULL DEFAULT 0,
    override_reason  TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assign_resource
    ON assignments(resource_kind, resource_id, status);
CREATE INDEX IF NOT EXISTS idx_assign_segment ON assignments(segment_id);

CREATE TABLE IF NOT EXISTS gaps (
    gap_id        TEXT PRIMARY KEY,
    segment_id    TEXT NOT NULL,
    resource_kind TEXT NOT NULL,
    reason        TEXT NOT NULL,
    detected_at   TEXT NOT NULL,
    resolved_at   TEXT
);

CREATE TABLE IF NOT EXISTS changes (
    change_id     TEXT PRIMARY KEY,
    trigger_type  TEXT NOT NULL,  -- classification_change|schedule_delay|equipment_failure|medical_override|manual
    trigger_ref   TEXT,
    reason        TEXT,
    actor         TEXT,
    impact_json   TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    notification_id TEXT PRIMARY KEY,
    change_id       TEXT,
    channel         TEXT NOT NULL,
    recipient       TEXT NOT NULL,        -- 原安排负责人/角色
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL,
    delivered_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS handoffs (
    handoff_id        TEXT PRIMARY KEY,
    person_id         TEXT NOT NULL,
    team_id           TEXT NOT NULL,
    from_city         TEXT NOT NULL,
    to_city           TEXT NOT NULL,
    outgoing_segment_id TEXT,
    incoming_segment_id TEXT,
    needs_json        TEXT NOT NULL DEFAULT '[]',
    status            TEXT NOT NULL DEFAULT 'pending', -- pending|accepted|closed
    opened_at         TEXT NOT NULL,
    accepted_at       TEXT
);
"""


class Store:
    """薄持久化层：一个文件一个连接，串行化写事务。"""

    def __init__(self, path=":memory:"):
        self.path = path
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.lock = threading.RLock()
        self.init_db()

    def init_db(self):
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self):
        with self.lock:
            self.conn.close()

    # ---------- 杂项 ----------

    @staticmethod
    def _dumps(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _loads(value, default):
        if value is None:
            return default
        return json.loads(value)

    def execute(self, sql, params=()):
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def query(self, sql, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    # ---------- 幂等 ----------

    def get_command(self, message_id):
        return self.query_one("SELECT * FROM commands WHERE message_id=?", (message_id,))

    def save_command(self, message_id, command_type, payload, response):
        with self.lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO commands(message_id, command_type, payload, response, created_at)"
                " VALUES(?,?,?,?,?)",
                (message_id, command_type, self._dumps(payload), self._dumps(response), now_iso()),
            )
            self.conn.commit()

    # ---------- 登记 ----------

    def upsert_team(self, team_id, name, city, sport, leader=None):
        self.execute(
            "INSERT INTO teams(team_id,name,city,sport,leader) VALUES(?,?,?,?,?) "
            "ON CONFLICT(team_id) DO UPDATE SET name=excluded.name,city=excluded.city,"
            "sport=excluded.sport,leader=COALESCE(excluded.leader,teams.leader)",
            (team_id, name, city, sport, leader),
        )

    def get_team(self, team_id):
        return self.query_one("SELECT * FROM teams WHERE team_id=?", (team_id,))

    def list_teams(self):
        return self.query("SELECT * FROM teams ORDER BY team_id")

    def upsert_person(self, person_id, name, team_id, city, category, needs, status=None):
        existing = self.get_person(person_id)
        ts = now_iso()
        if existing:
            self.execute(
                "UPDATE persons SET name=?,team_id=?,city=?,category=?,needs_json=?,"
                "status=COALESCE(?,status),updated_at=? WHERE person_id=?",
                (name, team_id, city, category, self._dumps(needs), status, ts, person_id),
            )
        else:
            self.execute(
                "INSERT INTO persons(person_id,name,team_id,city,category,needs_json,status,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (person_id, name, team_id, city, category, self._dumps(needs), status or "待报到", ts, ts),
            )

    def get_person(self, person_id):
        return self.query_one("SELECT * FROM persons WHERE person_id=?", (person_id,))

    def list_persons(self, team_id=None):
        if team_id:
            return self.query("SELECT * FROM persons WHERE team_id=? ORDER BY person_id", (team_id,))
        return self.query("SELECT * FROM persons ORDER BY person_id")

    def set_person_status(self, person_id, status, classification_status=None):
        if classification_status is not None:
            self.execute(
                "UPDATE persons SET status=?,classification_status=?,updated_at=? WHERE person_id=?",
                (status, classification_status, now_iso(), person_id),
            )
        else:
            self.execute(
                "UPDATE persons SET status=?,updated_at=? WHERE person_id=?",
                (status, now_iso(), person_id),
            )

    # ---------- 资源 ----------

    def upsert_venue(self, venue_id, name, city, features):
        self.execute(
            "INSERT INTO venues(venue_id,name,city,features_json) VALUES(?,?,?,?) "
            "ON CONFLICT(venue_id) DO UPDATE SET name=excluded.name,city=excluded.city,"
            "features_json=excluded.features_json",
            (venue_id, name, city, self._dumps(features)),
        )

    def get_venue(self, venue_id):
        return self.query_one("SELECT * FROM venues WHERE venue_id=?", (venue_id,))

    def list_venues(self, city=None):
        if city:
            return self.query("SELECT * FROM venues WHERE city=?", (city,))
        return self.query("SELECT * FROM venues")

    def upsert_hotel(self, hotel_id, name, city):
        self.execute(
            "INSERT INTO hotels(hotel_id,name,city) VALUES(?,?,?) "
            "ON CONFLICT(hotel_id) DO UPDATE SET name=excluded.name,city=excluded.city",
            (hotel_id, name, city),
        )

    def add_room(self, room_id, hotel_id, features):
        self.execute(
            "INSERT INTO rooms(room_id,hotel_id,features_json) VALUES(?,?,?) "
            "ON CONFLICT(room_id) DO UPDATE SET hotel_id=excluded.hotel_id,features_json=excluded.features_json",
            (room_id, hotel_id, self._dumps(features)),
        )

    def list_rooms(self, hotel_id=None):
        if hotel_id:
            return self.query(
                "SELECT r.*, h.city AS city, h.name AS hotel_name FROM rooms r "
                "JOIN hotels h ON h.hotel_id=r.hotel_id WHERE r.hotel_id=?",
                (hotel_id,),
            )
        return self.query(
            "SELECT r.*, h.city AS city, h.name AS hotel_name FROM rooms r JOIN hotels h ON h.hotel_id=r.hotel_id"
        )

    def upsert_vehicle(self, vehicle_id, city, kind, features, seats, active_until, service_status="in_service"):
        self.execute(
            "INSERT INTO vehicles(vehicle_id,city,kind,features_json,seats,active_until,service_status)"
            " VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(vehicle_id) DO UPDATE SET city=excluded.city,kind=excluded.kind,"
            "features_json=excluded.features_json,seats=excluded.seats,active_until=excluded.active_until,"
            "service_status=excluded.service_status",
            (vehicle_id, city, kind, self._dumps(sorted(features)), seats, active_until, service_status),
        )

    def get_vehicle(self, vehicle_id):
        return self.query_one("SELECT * FROM vehicles WHERE vehicle_id=?", (vehicle_id,))

    def list_vehicles(self, city=None):
        if city:
            return self.query("SELECT * FROM vehicles WHERE city=?", (city,))
        return self.query("SELECT * FROM vehicles")

    def upsert_equipment(self, item_id, city, eq_type, active_until, service_status="in_service"):
        self.execute(
            "INSERT INTO equipment(item_id,city,eq_type,active_until,service_status) VALUES(?,?,?,?,?) "
            "ON CONFLICT(item_id) DO UPDATE SET city=excluded.city,eq_type=excluded.eq_type,"
            "active_until=excluded.active_until,service_status=excluded.service_status",
            (item_id, city, eq_type, active_until, service_status),
        )

    def get_equipment(self, item_id):
        return self.query_one("SELECT * FROM equipment WHERE item_id=?", (item_id,))

    def list_equipment(self, city=None, eq_type=None):
        sql = "SELECT * FROM equipment WHERE 1=1"
        params = []
        if city:
            sql += " AND city=?"
            params.append(city)
        if eq_type:
            sql += " AND eq_type=?"
            params.append(eq_type)
        return self.query(sql, params)

    def upsert_volunteer(self, volunteer_id, name, city, skills, credential_until):
        self.execute(
            "INSERT INTO volunteers(volunteer_id,name,city,skills_json,credential_until) VALUES(?,?,?,?,?) "
            "ON CONFLICT(volunteer_id) DO UPDATE SET name=excluded.name,city=excluded.city,"
            "skills_json=excluded.skills_json,credential_until=excluded.credential_until",
            (volunteer_id, name, city, self._dumps(sorted(skills)), credential_until),
        )

    def list_volunteers(self, city=None):
        if city:
            return self.query("SELECT * FROM volunteers WHERE city=?", (city,))
        return self.query("SELECT * FROM volunteers")

    # ---------- 赛程 ----------

    def upsert_event(self, event_id, venue_id, city, sport, stage, title, start, end):
        self.execute(
            "INSERT INTO events(event_id,venue_id,city,sport,stage,title,scheduled_start,scheduled_end)"
            " VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(event_id) DO UPDATE SET venue_id=excluded.venue_id,city=excluded.city,"
            "sport=excluded.sport,stage=excluded.stage,title=excluded.title,"
            "scheduled_start=excluded.scheduled_start,scheduled_end=excluded.scheduled_end",
            (event_id, venue_id, city, sport, stage, title, start, end),
        )

    def get_event(self, event_id):
        return self.query_one("SELECT * FROM events WHERE event_id=?", (event_id,))

    def list_events(self, city=None):
        if city:
            return self.query("SELECT * FROM events WHERE city=? ORDER BY scheduled_start", (city,))
        return self.query("SELECT * FROM events ORDER BY scheduled_start")

    def add_team_event(self, team_id, event_id):
        self.execute(
            "INSERT OR IGNORE INTO team_events(team_id,event_id) VALUES(?,?)", (team_id, event_id)
        )

    def teams_for_event(self, event_id):
        return self.query(
            "SELECT t.* FROM teams t JOIN team_events te ON te.team_id=t.team_id WHERE te.event_id=?",
            (event_id,),
        )

    def events_for_team(self, team_id):
        return self.query(
            "SELECT e.* FROM events e JOIN team_events te ON te.event_id=e.event_id "
            "WHERE te.team_id=? ORDER BY e.scheduled_start",
            (team_id,),
        )

    # ---------- 行程段 ----------

    def upsert_segment(self, segment_id, team_id, kind, city, start, end,
                       person_id=None, venue_id=None, hotel_id=None, event_id=None):
        existing = self.get_segment(segment_id)
        if existing:
            version = existing["version"] + 1
            self.execute(
                "UPDATE segments SET person_id=?,team_id=?,event_id=?,kind=?,city=?,venue_id=?,hotel_id=?,"
                "planned_start=?,planned_end=?,version=?,updated_at=? WHERE segment_id=?",
                (person_id, team_id, event_id, kind, city, venue_id, hotel_id, start, end, version, now_iso(),
                 segment_id),
            )
        else:
            self.execute(
                "INSERT INTO segments(segment_id,person_id,team_id,event_id,kind,city,venue_id,hotel_id,"
                "planned_start,planned_end,status,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (segment_id, person_id, team_id, event_id, kind, city, venue_id, hotel_id,
                 start, end, "planned", now_iso()),
            )

    def get_segment(self, segment_id):
        return self.query_one("SELECT * FROM segments WHERE segment_id=?", (segment_id,))

    def list_segments(self, team_id=None, person_id=None, city=None):
        sql = "SELECT * FROM segments WHERE 1=1"
        params = []
        if team_id:
            sql += " AND team_id=?"
            params.append(team_id)
        if person_id:
            sql += " AND person_id=?"
            params.append(person_id)
        if city:
            sql += " AND city=?"
            params.append(city)
        sql += " ORDER BY planned_start"
        return self.query(sql, params)

    def set_segment_status(self, segment_id, status):
        self.execute("UPDATE segments SET status=?,updated_at=? WHERE segment_id=?",
                     (status, now_iso(), segment_id))

    def shift_segment(self, segment_id, new_start, new_end):
        self.execute(
            "UPDATE segments SET planned_start=?,planned_end=?,version=version+1,updated_at=? "
            "WHERE segment_id=?",
            (new_start, new_end, now_iso(), segment_id),
        )

    # ---------- 分配 ----------

    def active_assignments_for(self, resource_kind, resource_id, start, end):
        """与给定窗口重叠的、仍然有效的分配（scheduled/fulfilled）。"""
        return self.query(
            "SELECT * FROM assignments WHERE resource_kind=? AND resource_id=? "
            "AND status IN ('scheduled','fulfilled') "
            "AND window_start < ? AND window_end > ?",
            (resource_kind, resource_id, end, start),
        )

    def add_assignment(self, assignment_id, segment_id, resource_kind, resource_id, city,
                       start, end, team_id, seats_taken=1, medical_override=0, reason=None):
        self.execute(
            "INSERT INTO assignments(assignment_id,segment_id,resource_kind,resource_id,city,"
            "window_start,window_end,seats_taken,team_id,status,medical_override,override_reason,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (assignment_id, segment_id, resource_kind, resource_id, city, start, end,
             seats_taken, team_id, "scheduled", medical_override, reason, now_iso()),
        )

    def release_assignment(self, assignment_id):
        self.execute("UPDATE assignments SET status='released' WHERE assignment_id=?", (assignment_id,))

    def list_assignments(self, segment_id=None, status=None):
        sql = "SELECT * FROM assignments WHERE 1=1"
        params = []
        if segment_id:
            sql += " AND segment_id=?"
            params.append(segment_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY window_start"
        return self.query(sql, params)

    def active_assignments_for_segment(self, segment_id):
        return self.query(
            "SELECT * FROM assignments WHERE segment_id=? AND status IN ('scheduled','fulfilled')",
            (segment_id,),
        )

    def mark_assignment_fulfilled(self, assignment_id):
        self.execute("UPDATE assignments SET status='fulfilled' WHERE assignment_id=?", (assignment_id,))

    # ---------- 空档 ----------

    def open_gap(self, gap_id, segment_id, resource_kind, reason):
        self.execute(
            "INSERT INTO gaps(gap_id,segment_id,resource_kind,reason,detected_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(gap_id) DO UPDATE SET reason=excluded.reason,resolved_at=NULL",
            (gap_id, segment_id, resource_kind, reason, now_iso()),
        )

    def resolve_gaps_for(self, segment_id, resource_kind=None):
        if resource_kind:
            self.execute(
                "UPDATE gaps SET resolved_at=? WHERE segment_id=? AND resource_kind=? AND resolved_at IS NULL",
                (now_iso(), segment_id, resource_kind),
            )
        else:
            self.execute(
                "UPDATE gaps SET resolved_at=? WHERE segment_id=? AND resolved_at IS NULL",
                (now_iso(), segment_id),
            )

    def list_open_gaps(self, city=None):
        sql = ("SELECT g.*, s.city AS city, s.team_id AS team_id FROM gaps g "
               "JOIN segments s ON s.segment_id=g.segment_id WHERE g.resolved_at IS NULL")
        params = []
        if city:
            sql += " AND s.city=?"
            params.append(city)
        return self.query(sql, params)

    # ---------- 改派审计 ----------

    def record_change(self, change_id, trigger_type, trigger_ref, reason, actor, impact):
        self.execute(
            "INSERT INTO changes(change_id,trigger_type,trigger_ref,reason,actor,impact_json,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (change_id, trigger_type, trigger_ref, reason, actor, self._dumps(impact), now_iso()),
        )

    def list_changes(self, limit=100):
        rows = self.query("SELECT * FROM changes ORDER BY created_at DESC, change_id DESC LIMIT ?", (limit,))
        result = []
        for row in rows:
            item = dict(row)
            item["impact"] = self._loads(item.pop("impact_json"), {})
            result.append(item)
        return result

    # ---------- 通知 ----------

    def add_notification(self, notification_id, recipient, subject, body, change_id=None, channel="internal"):
        self.execute(
            "INSERT INTO notifications(notification_id,change_id,channel,recipient,subject,body,delivered_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (notification_id, change_id, channel, recipient, subject, body, now_iso()),
        )

    def list_notifications(self, change_id=None):
        if change_id:
            return self.query("SELECT * FROM notifications WHERE change_id=? ORDER BY delivered_at",
                              (change_id,))
        return self.query("SELECT * FROM notifications ORDER BY delivered_at DESC")

    # ---------- 跨城交接 ----------

    def open_handoff(self, handoff_id, person_id, team_id, from_city, to_city,
                     needs, outgoing_segment_id=None):
        self.execute(
            "INSERT INTO handoffs(handoff_id,person_id,team_id,from_city,to_city,"
            "outgoing_segment_id,needs_json,status,opened_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(handoff_id) DO UPDATE SET from_city=excluded.from_city,to_city=excluded.to_city,"
            "outgoing_segment_id=excluded.outgoing_segment_id,needs_json=excluded.needs_json",
            (handoff_id, person_id, team_id, from_city, to_city, outgoing_segment_id,
             self._dumps(needs), "pending", now_iso()),
        )

    def accept_handoff(self, handoff_id, incoming_segment_id):
        self.execute(
            "UPDATE handoffs SET status='accepted',incoming_segment_id=?,accepted_at=? "
            "WHERE handoff_id=?",
            (incoming_segment_id, now_iso(), handoff_id),
        )

    def close_handoff(self, handoff_id):
        self.execute("UPDATE handoffs SET status='closed' WHERE handoff_id=?", (handoff_id,))

    def get_handoff(self, handoff_id):
        return self.query_one("SELECT * FROM handoffs WHERE handoff_id=?", (handoff_id,))

    def list_handoffs(self, status=None):
        if status:
            return self.query("SELECT * FROM handoffs WHERE status=? ORDER BY opened_at", (status,))
        return self.query("SELECT * FROM handoffs ORDER BY opened_at")

    # ---------- 工具 ----------

    @staticmethod
    def shift_iso(iso_text, minutes):
        base = datetime.strptime(iso_text, "%Y-%m-%dT%H:%M")
        return (base + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M")
