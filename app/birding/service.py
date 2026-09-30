from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction

SCHEMA = """
CREATE TABLE IF NOT EXISTS bird_routes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK(capacity > 0),
    wheelchair_accessible INTEGER NOT NULL DEFAULT 0 CHECK(wheelchair_accessible IN (0,1)),
    difficulty TEXT NOT NULL CHECK(difficulty IN ('easy','moderate','hard')),
    description TEXT NOT NULL DEFAULT '',
    is_closed INTEGER NOT NULL DEFAULT 0 CHECK(is_closed IN (0,1)),
    closed_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bird_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    start_at TEXT NOT NULL,
    capacity_per_route INTEGER NOT NULL CHECK(capacity_per_route > 0),
    accessible_capacity INTEGER NOT NULL CHECK(accessible_capacity >= 0),
    waitlist_capacity INTEGER NOT NULL CHECK(waitlist_capacity >= 0),
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','cancelled','transferred')),
    closure_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bird_event_routes (
    event_id INTEGER NOT NULL REFERENCES bird_events(id) ON DELETE CASCADE,
    route_id INTEGER NOT NULL REFERENCES bird_routes(id) ON DELETE RESTRICT,
    PRIMARY KEY(event_id, route_id)
);
CREATE TABLE IF NOT EXISTS bird_registrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES bird_events(id),
    primary_name TEXT NOT NULL,
    contact TEXT NOT NULL,
    party_size INTEGER NOT NULL CHECK(party_size >= 1),
    needs_accessible INTEGER NOT NULL DEFAULT 0 CHECK(needs_accessible IN (0,1)),
    preferred_routes_json TEXT NOT NULL DEFAULT '[]',
    idempotency_key TEXT NOT NULL,
    source_registration_id INTEGER REFERENCES bird_registrations(id),
    status TEXT NOT NULL CHECK(status IN (
        'confirmed','waitlisted','cancelled','checked_in','transferred_out','event_cancelled'
    )),
    waitlist_rank INTEGER,
    route_id INTEGER REFERENCES bird_routes(id),
    confirmed_at TEXT,
    cancelled_at TEXT,
    checked_in_at TEXT,
    cancel_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(event_id, idempotency_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bird_reg_active_contact ON bird_registrations(event_id, contact)
    WHERE status IN ('confirmed','waitlisted','checked_in');
CREATE INDEX IF NOT EXISTS idx_bird_reg_event ON bird_registrations(event_id, status, waitlist_rank);
CREATE TABLE IF NOT EXISTS bird_companions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    registration_id INTEGER NOT NULL REFERENCES bird_registrations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    needs_accessible INTEGER NOT NULL DEFAULT 0 CHECK(needs_accessible IN (0,1)),
    position INTEGER NOT NULL,
    UNIQUE(registration_id, position)
);
CREATE TABLE IF NOT EXISTS bird_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    registration_id INTEGER NOT NULL REFERENCES bird_registrations(id) ON DELETE CASCADE,
    event_id INTEGER NOT NULL REFERENCES bird_events(id),
    type TEXT NOT NULL CHECK(type IN (
        'confirmation','waitlist','promotion','cancellation',
        'route_cancelled','event_transfer','event_cancelled'
    )),
    channel TEXT NOT NULL DEFAULT 'sms',
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','delivered','failed')),
    sent_at TEXT,
    acknowledged_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bird_notif_reg ON bird_notifications(registration_id, id);
CREATE INDEX IF NOT EXISTS idx_bird_notif_pending ON bird_notifications(status, event_id);
CREATE TABLE IF NOT EXISTS bird_event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES bird_events(id),
    registration_id INTEGER,
    action TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    actor TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bird_log_event ON bird_event_log(event_id, id);
"""

ACTIVE_STATUSES = ("confirmed", "checked_in")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)


class BirdingService:
    """观鸟导赏场次、报名、候补递补与路线关闭通知的事务服务。"""

    def __init__(self, connection: sqlite3.Connection | None = None) -> None:
        self.connection = connection or get_connection()
        ensure_schema()

    # ---------- 基础查询 ----------

    def _must_route(self, conn: sqlite3.Connection, code: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM bird_routes WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError(f"路线 {code} 不存在", context={"route_code": code})
        return row

    def _must_event(self, conn: sqlite3.Connection, code: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM bird_events WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError(f"场次 {code} 不存在", context={"event_code": code})
        return row

    def _event_route_rows(self, conn: sqlite3.Connection, event_id: int) -> list[sqlite3.Row]:
        return conn.execute(
            """
            SELECT r.* FROM bird_event_routes er
            JOIN bird_routes r ON r.id = er.route_id
            WHERE er.event_id=? ORDER BY r.id
            """,
            (event_id,),
        ).fetchall()

    def _route_occupancy(self, conn: sqlite3.Connection, event_id: int, route_id: int) -> tuple[int, int]:
        """返回 (普通占用人数, 无障碍占用组数)。"""
        row = conn.execute(
            f"""
            SELECT COALESCE(SUM(party_size),0) AS used,
                   COALESCE(SUM(needs_accessible),0) AS accessible_used
            FROM bird_registrations
            WHERE event_id=? AND route_id=? AND status IN ({','.join('?' * len(ACTIVE_STATUSES))})
            """,
            (event_id, route_id, *ACTIVE_STATUSES),
        ).fetchone()
        return int(row["used"]), int(row["accessible_used"])

    def _allocate_route(
        self,
        conn: sqlite3.Connection,
        event: sqlite3.Row,
        party_size: int,
        needs_accessible: bool,
        preferred_codes: list[str],
    ) -> int | None:
        """在未关闭路线中为整组人找一条承载得住的路线，优先偏好路线。"""
        routes = self._event_route_rows(conn, event["id"])
        candidates: list[sqlite3.Row] = []
        for route in routes:
            if route["is_closed"]:
                continue
            if needs_accessible and not route["wheelchair_accessible"]:
                continue
            used, accessible_used = self._route_occupancy(conn, event["id"], route["id"])
            if used + party_size > event["capacity_per_route"]:
                continue
            if needs_accessible and accessible_used + 1 > event["accessible_capacity"]:
                continue
            candidates.append(route)
        if not candidates:
            return None
        preference = {code: index for index, code in enumerate(preferred_codes)}
        candidates.sort(key=lambda r: (preference.get(r["code"], len(preference)), r["id"]))
        return int(candidates[0]["id"])

    # ---------- 通知 ----------

    def _notify(
        self,
        conn: sqlite3.Connection,
        registration_id: int,
        event_id: int,
        ntype: str,
        payload: dict[str, Any],
    ) -> int:
        cursor = conn.execute(
            """
            INSERT INTO bird_notifications(registration_id,event_id,type,payload_json,created_at)
            VALUES(?,?,?,?,?)
            """,
            (registration_id, event_id, ntype, json.dumps(payload, ensure_ascii=False), _now()),
        )
        return int(cursor.lastrowid)

    def _log(
        self,
        conn: sqlite3.Connection,
        event_id: int,
        action: str,
        detail: dict[str, Any] | None = None,
        registration_id: int | None = None,
        actor: str = "system",
    ) -> None:
        conn.execute(
            """
            INSERT INTO bird_event_log(event_id,registration_id,action,detail_json,actor,created_at)
            VALUES(?,?,?,?,?,?)
            """,
            (event_id, registration_id, action, json.dumps(detail or {}, ensure_ascii=False), actor, _now()),
        )

    # ---------- 路线与场次 ----------

    def create_route(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO bird_routes(code,name,capacity,wheelchair_accessible,difficulty,description,created_at)
                    VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        payload["code"],
                        payload["name"],
                        payload["capacity"],
                        1 if payload["wheelchair_accessible"] else 0,
                        payload["difficulty"],
                        payload["description"],
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"路线 {payload['code']} 已存在", context={"route_code": payload["code"]}) from exc
            return dict(conn.execute("SELECT * FROM bird_routes WHERE id=?", (cursor.lastrowid,)).fetchone())

    def list_routes(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM bird_routes ORDER BY id").fetchall()]

    def create_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as conn:
            routes = [self._must_route(conn, code) for code in dict.fromkeys(payload["route_codes"])]
            for route in routes:
                if route["is_closed"]:
                    raise ValidationError(
                        f"路线 {route['code']} 已因保护关闭，不能排入新场次",
                        context={"route_code": route["code"]},
                    )
                if payload["capacity_per_route"] > route["capacity"]:
                    raise ValidationError(
                        f"路线 {route['code']} 最大承载 {route['capacity']} 人，无法支撑该场次名额",
                        context={"route_code": route["code"], "route_capacity": route["capacity"]},
                    )
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO bird_events(code,title,start_at,capacity_per_route,accessible_capacity,
                        waitlist_capacity,notes,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        payload["code"],
                        payload["title"],
                        payload["start_at"],
                        payload["capacity_per_route"],
                        payload["accessible_capacity"],
                        payload["waitlist_capacity"],
                        payload["notes"],
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"场次 {payload['code']} 已存在", context={"event_code": payload["code"]}) from exc
            event_id = int(cursor.lastrowid)
            conn.executemany(
                "INSERT INTO bird_event_routes(event_id,route_id) VALUES(?,?)",
                [(event_id, route["id"]) for route in routes],
            )
            self._log(
                conn,
                event_id,
                "event.create",
                {"route_codes": [route["code"] for route in routes], **payload},
                actor="planner",
            )
            return self.get_event(payload["code"])

    def list_events(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM bird_events ORDER BY id").fetchall()]

    def get_event(self, code: str) -> dict[str, Any]:
        event = self._must_event(self.connection, code)
        result = dict(event)
        result["routes"] = [dict(route) for route in self._event_route_rows(self.connection, event["id"])]
        waitlisted = self.connection.execute(
            "SELECT COUNT(*) AS c FROM bird_registrations WHERE event_id=? AND status='waitlisted'",
            (event["id"],),
        ).fetchone()["c"]
        result["waitlist_count"] = int(waitlisted)
        return result

    # ---------- 报名 ----------

    def register(self, payload: dict[str, Any]) -> dict[str, Any]:
        companions = payload.get("companions", [])
        needs_accessible = bool(payload["requires_accessible"]) or any(c["requires_accessible"] for c in companions)
        now = _now()
        with transaction(immediate=True) as conn:
            event = self._must_event(conn, payload["event_code"])
            if event["status"] != "open":
                raise ConflictError(
                    f"场次 {event['code']} 已{event['status']}，停止报名",
                    context={"event_code": event["code"], "event_status": event["status"]},
                )
            existing = conn.execute(
                "SELECT * FROM bird_registrations WHERE event_id=? AND idempotency_key=?",
                (event["id"], payload["idempotency_key"]),
            ).fetchone()
            if existing is not None:
                # 重复到达的报名请求：幂等返回首次结果，不重复占位
                return self._registration_detail(conn, existing, replayed=True)
            duplicate = conn.execute(
                "SELECT id FROM bird_registrations WHERE event_id=? AND contact=? "
                "AND status IN ('confirmed','waitlisted','checked_in')",
                (event["id"], payload["contact"]),
            ).fetchone()
            if duplicate is not None:
                raise ConflictError(
                    "该联系人已报名本场活动，请勿重复占位",
                    context={"event_code": event["code"], "contact": payload["contact"]},
                )
            party_size = int(payload["party_size"])
            preferred = payload.get("preferred_route_codes", [])
            route_id = self._allocate_route(conn, event, party_size, needs_accessible, preferred)
            if route_id is not None:
                status, waitlist_rank = "confirmed", None
            else:
                queued = conn.execute(
                    "SELECT COUNT(*) AS c FROM bird_registrations WHERE event_id=? AND status='waitlisted'",
                    (event["id"],),
                ).fetchone()["c"]
                if queued >= event["waitlist_capacity"]:
                    raise ConflictError(
                        "本场名额与候补均已满",
                        context={"event_code": event["code"], "waitlist_capacity": event["waitlist_capacity"]},
                    )
                # 序号只增不复用，保证候补顺序在晋升/取消后仍可追溯
                max_rank = conn.execute(
                    "SELECT COALESCE(MAX(waitlist_rank),0) AS m FROM bird_registrations WHERE event_id=?",
                    (event["id"],),
                ).fetchone()["m"]
                status, waitlist_rank = "waitlisted", int(max_rank) + 1
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO bird_registrations(event_id,primary_name,contact,party_size,needs_accessible,
                        preferred_routes_json,idempotency_key,status,waitlist_rank,route_id,confirmed_at,
                        created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        event["id"],
                        payload["primary_name"],
                        payload["contact"],
                        party_size,
                        1 if needs_accessible else 0,
                        json.dumps(preferred, ensure_ascii=False),
                        payload["idempotency_key"],
                        status,
                        waitlist_rank,
                        route_id,
                        now if status == "confirmed" else None,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("报名重复或名额状态已变化，请刷新后重试") from exc
            reg_id = int(cursor.lastrowid)
            conn.executemany(
                "INSERT INTO bird_companions(registration_id,name,needs_accessible,position) VALUES(?,?,?,?)",
                [
                    (reg_id, companion["name"], 1 if companion["requires_accessible"] else 0, position)
                    for position, companion in enumerate(companions, start=1)
                ],
            )
            reg = conn.execute("SELECT * FROM bird_registrations WHERE id=?", (reg_id,)).fetchone()
            if status == "confirmed":
                route = conn.execute("SELECT code FROM bird_routes WHERE id=?", (route_id,)).fetchone()
                self._notify(
                    conn,
                    reg_id,
                    event["id"],
                    "confirmation",
                    {
                        "event_code": event["code"],
                        "title": event["title"],
                        "start_at": event["start_at"],
                        "route_code": route["code"],
                        "party_size": party_size,
                        "needs_accessible": needs_accessible,
                    },
                )
                self._log(conn, event["id"], "registration.confirm", {"route_code": route["code"], "party_size": party_size}, reg_id)
            else:
                self._notify(
                    conn,
                    reg_id,
                    event["id"],
                    "waitlist",
                    {"event_code": event["code"], "waitlist_rank": waitlist_rank, "party_size": party_size},
                )
                self._log(conn, event["id"], "registration.waitlist", {"waitlist_rank": waitlist_rank}, reg_id)
            return self._registration_detail(conn, reg)

    def _companions(self, conn: sqlite3.Connection, reg_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT name,needs_accessible,position FROM bird_companions WHERE registration_id=? ORDER BY position",
                (reg_id,),
            ).fetchall()
        ]

    def _notifications(self, conn: sqlite3.Connection, reg_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM bird_notifications WHERE registration_id=? ORDER BY id", (reg_id,)
            ).fetchall()
        ]

    def _registration_detail(
        self, conn: sqlite3.Connection, reg: sqlite3.Row, *, replayed: bool = False
    ) -> dict[str, Any]:
        result = dict(reg)
        result["preferred_routes"] = json.loads(result.pop("preferred_routes_json") or "[]")
        result["companions"] = self._companions(conn, reg["id"])
        result["notifications"] = self._notifications(conn, reg["id"])
        route = conn.execute("SELECT code,name FROM bird_routes WHERE id=?", (reg["route_id"],)).fetchone()
        result["route_code"] = route["code"] if route else None
        result["idempotent_replayed"] = replayed
        return result

    def get_registration(self, registration_id: int) -> dict[str, Any]:
        reg = self.connection.execute("SELECT * FROM bird_registrations WHERE id=?", (registration_id,)).fetchone()
        if reg is None:
            raise NotFoundError("报名记录不存在", context={"registration_id": registration_id})
        return self._registration_detail(self.connection, reg)

    def list_event_registrations(self, event_code: str) -> list[dict[str, Any]]:
        event = self._must_event(self.connection, event_code)
        rows = self.connection.execute(
            "SELECT * FROM bird_registrations WHERE event_id=? ORDER BY id", (event["id"],)
        ).fetchall()
        return [self._registration_detail(self.connection, row) for row in rows]

    # ---------- 退出与候补递补 ----------

    def cancel_registration(self, registration_id: int, reason: str, actor: str = "participant") -> dict[str, Any]:
        with transaction(immediate=True) as conn:
            reg = conn.execute("SELECT * FROM bird_registrations WHERE id=?", (registration_id,)).fetchone()
            if reg is None:
                raise NotFoundError("报名记录不存在", context={"registration_id": registration_id})
            if reg["status"] in ("cancelled", "event_cancelled"):
                raise ConflictError("该记录已取消，不能重复操作", context={"status": reg["status"]})
            if reg["status"] == "checked_in":
                raise ConflictError("该记录已签到，不能取消", context={"status": reg["status"]})
            if reg["status"] == "transferred_out":
                raise ConflictError("该记录已随整场转移，不能在原场次取消", context={"status": reg["status"]})
            freed_route = reg["route_id"] if reg["status"] == "confirmed" else None
            now = _now()
            conn.execute(
                "UPDATE bird_registrations SET status='cancelled',cancelled_at=?,cancel_reason=?,"
                "route_id=NULL,updated_at=? WHERE id=?",
                (now, reason, now, registration_id),
            )
            event = conn.execute("SELECT * FROM bird_events WHERE id=?", (reg["event_id"],)).fetchone()
            self._notify(
                conn,
                registration_id,
                event["id"],
                "cancellation",
                {"event_code": event["code"], "reason": reason, "prior_status": reg["status"]},
            )
            self._log(
                conn,
                event["id"],
                "registration.cancel",
                {"reason": reason, "prior_status": reg["status"], "freed_route_id": freed_route},
                registration_id,
                actor=actor,
            )
            promotions: list[dict[str, Any]] = []
            if freed_route is not None:
                promotions = self._promote_waitlist(conn, event)
            detail = self._registration_detail(
                conn, conn.execute("SELECT * FROM bird_registrations WHERE id=?", (registration_id,)).fetchone()
            )
            detail["promotions"] = promotions
            return detail

    def _promote_waitlist(self, conn: sqlite3.Connection, event: sqlite3.Row) -> list[dict[str, Any]]:
        """严格按候补序号递补：只晋升 waitlisted 记录，签到/取消/已晋升者一律不动。"""
        promoted: list[dict[str, Any]] = []
        waiting = conn.execute(
            "SELECT * FROM bird_registrations WHERE event_id=? AND status='waitlisted' "
            "ORDER BY waitlist_rank ASC, id ASC",
            (event["id"],),
        ).fetchall()
        now = _now()
        for candidate in waiting:
            preferred = json.loads(candidate["preferred_routes_json"] or "[]")
            route_id = self._allocate_route(
                conn, event, candidate["party_size"], bool(candidate["needs_accessible"]), preferred
            )
            if route_id is None:
                # 队首组整组放不下时，严格保留其优先级，不跳过、不拆分
                break
            conn.execute(
                "UPDATE bird_registrations SET status='confirmed',route_id=?,confirmed_at=?,updated_at=? WHERE id=?",
                (route_id, now, now, candidate["id"]),
            )
            route = conn.execute("SELECT code FROM bird_routes WHERE id=?", (route_id,)).fetchone()
            self._notify(
                conn,
                candidate["id"],
                event["id"],
                "promotion",
                {
                    "event_code": event["code"],
                    "title": event["title"],
                    "start_at": event["start_at"],
                    "route_code": route["code"],
                    "party_size": candidate["party_size"],
                    "waitlist_rank": candidate["waitlist_rank"],
                },
            )
            self._log(
                conn,
                event["id"],
                "waitlist.promote",
                {"route_code": route["code"], "waitlist_rank": candidate["waitlist_rank"]},
                candidate["id"],
            )
            promoted.append({"registration_id": candidate["id"], "route_code": route["code"], "waitlist_rank": candidate["waitlist_rank"]})
        return promoted

    def check_in(self, registration_id: int, actor: str = "volunteer") -> dict[str, Any]:
        with transaction(immediate=True) as conn:
            reg = conn.execute("SELECT * FROM bird_registrations WHERE id=?", (registration_id,)).fetchone()
            if reg is None:
                raise NotFoundError("报名记录不存在", context={"registration_id": registration_id})
            if reg["status"] == "checked_in":
                raise ConflictError("该记录已签到", context={"status": reg["status"]})
            if reg["status"] != "confirmed":
                raise ConflictError(
                    "只有已确认的报名可以签到，候补或已取消记录不能签到",
                    context={"status": reg["status"]},
                )
            now = _now()
            conn.execute(
                "UPDATE bird_registrations SET status='checked_in',checked_in_at=?,updated_at=? WHERE id=?",
                (now, now, registration_id),
            )
            event = conn.execute("SELECT * FROM bird_events WHERE id=?", (reg["event_id"],)).fetchone()
            self._log(conn, event["id"], "registration.check_in", {}, registration_id, actor=actor)
            return self._registration_detail(
                conn, conn.execute("SELECT * FROM bird_registrations WHERE id=?", (registration_id,)).fetchone()
            )

    # ---------- 通知回执 ----------

    def dispatch_pending(self, event_code: str | None = None) -> dict[str, Any]:
        """模拟短信网关投递：把待发送通知置为 delivered 并产生回执时间。"""
        now = _now()
        with transaction(immediate=True) as conn:
            if event_code is not None:
                event = self._must_event(conn, event_code)
                rows = conn.execute(
                    "SELECT * FROM bird_notifications WHERE status='pending' AND event_id=? ORDER BY id",
                    (event["id"],),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM bird_notifications WHERE status='pending' ORDER BY id").fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE bird_notifications SET status='delivered',sent_at=? WHERE id=?",
                    (now, row["id"]),
                )
            return {"dispatched": len(rows), "notification_ids": [row["id"] for row in rows]}

    def acknowledge_notification(self, notification_id: int) -> dict[str, Any]:
        with transaction(immediate=True) as conn:
            row = conn.execute("SELECT * FROM bird_notifications WHERE id=?", (notification_id,)).fetchone()
            if row is None:
                raise NotFoundError("通知不存在", context={"notification_id": notification_id})
            if row["acknowledged_at"] is None:
                now = _now()
                conn.execute("UPDATE bird_notifications SET acknowledged_at=? WHERE id=?", (now, notification_id))
                event = conn.execute("SELECT * FROM bird_events WHERE id=?", (row["event_id"],)).fetchone()
                self._log(
                    conn,
                    event["id"],
                    "notification.ack",
                    {"notification_id": notification_id, "type": row["type"]},
                    row["registration_id"],
                    actor="participant",
                )
                row = conn.execute("SELECT * FROM bird_notifications WHERE id=?", (notification_id,)).fetchone()
            return dict(row)

    def list_notifications(self, event_code: str | None = None) -> list[dict[str, Any]]:
        if event_code is None:
            rows = self.connection.execute(
                """
                SELECT n.*, r.primary_name, r.contact FROM bird_notifications n
                JOIN bird_registrations r ON r.id = n.registration_id
                ORDER BY n.id
                """
            ).fetchall()
        else:
            event = self._must_event(self.connection, event_code)
            rows = self.connection.execute(
                """
                SELECT n.*, r.primary_name, r.contact FROM bird_notifications n
                JOIN bird_registrations r ON r.id = n.registration_id
                WHERE n.event_id=? ORDER BY n.id
                """,
                (event["id"],),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------- 路线保护关闭：整场转移或取消 ----------

    def close_event_route(
        self,
        event_code: str,
        route_code: str,
        action: str,
        reason: str,
        target_event_code: str | None,
        actor: str = "ranger",
    ) -> dict[str, Any]:
        with transaction(immediate=True) as conn:
            event = self._must_event(conn, event_code)
            route = self._must_route(conn, route_code)
            linked = conn.execute(
                "SELECT 1 FROM bird_event_routes WHERE event_id=? AND route_id=?",
                (event["id"], route["id"]),
            ).fetchone()
            if linked is None:
                raise ValidationError(
                    f"路线 {route_code} 不属于场次 {event_code}",
                    context={"event_code": event_code, "route_code": route_code},
                )
            target_event = None
            if action == "transfer":
                if not target_event_code:
                    raise ValidationError("转移必须提供 target_event_code")
                target_event = self._must_event(conn, target_event_code)
                if target_event["status"] != "open":
                    raise ConflictError(
                        f"目标场次 {target_event['code']} 状态为 {target_event['status']}，无法转入",
                        context={"target_event_code": target_event_code, "status": target_event["status"]},
                    )
            # 路线全局关闭：此后任何场次的名额分配都不会再选到它
            conn.execute(
                "UPDATE bird_routes SET is_closed=1,closed_reason=? WHERE id=?",
                (reason, route["id"]),
            )
            affected = conn.execute(
                "SELECT * FROM bird_registrations WHERE event_id=? AND route_id=? AND status IN ('confirmed','checked_in')",
                (event["id"], route["id"]),
            ).fetchall()
            results: list[dict[str, Any]] = []
            now = _now()
            if action == "cancel":
                for reg in affected:
                    conn.execute(
                        "UPDATE bird_registrations SET status='event_cancelled',cancelled_at=?,"
                        "cancel_reason=?,updated_at=? WHERE id=?",
                        (now, reason, now, reg["id"]),
                    )
                    self._notify(
                        conn,
                        reg["id"],
                        event["id"],
                        "route_cancelled",
                        {
                            "event_code": event["code"],
                            "route_code": route["code"],
                            "reason": reason,
                            "decision": "cancel",
                            "primary_name": reg["primary_name"],
                        },
                    )
                    self._log(conn, event["id"], "route.close_cancel", {"route_code": route["code"], "reason": reason}, reg["id"], actor)
                    results.append({"registration_id": reg["id"], "outcome": "cancelled"})
                self._maybe_finish_event(conn, event, action="cancel", reason=reason, actor=actor)
                # 关闭路线腾出的只是关闭路线本身，但其他开放路线若有空缺仍应按序递补
                refreshed = conn.execute("SELECT * FROM bird_events WHERE id=?", (event["id"],)).fetchone()
                promotions = self._promote_waitlist(conn, refreshed) if refreshed["status"] == "open" else []
            else:
                for reg in affected:
                    preferred = json.loads(reg["preferred_routes_json"] or "[]")
                    already_active = conn.execute(
                        "SELECT 1 FROM bird_registrations WHERE event_id=? AND contact=? "
                        "AND status IN ('confirmed','waitlisted','checked_in')",
                        (target_event["id"], reg["contact"]),
                    ).fetchone()
                    target_route_id = None
                    if already_active is None:
                        target_route_id = self._allocate_route(
                            conn, target_event, reg["party_size"], bool(reg["needs_accessible"]), preferred
                        )
                    outcome_reg_id: int | None = None
                    if target_route_id is not None:
                        target_route = conn.execute("SELECT code FROM bird_routes WHERE id=?", (target_route_id,)).fetchone()
                        cursor = conn.execute(
                            """
                            INSERT INTO bird_registrations(event_id,primary_name,contact,party_size,needs_accessible,
                                preferred_routes_json,idempotency_key,source_registration_id,status,route_id,
                                confirmed_at,created_at,updated_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                target_event["id"],
                                reg["primary_name"],
                                reg["contact"],
                                reg["party_size"],
                                reg["needs_accessible"],
                                reg["preferred_routes_json"],
                                f"transfer:{reg['id']}:{target_event['id']}",
                                reg["id"],
                                "confirmed",
                                target_route_id,
                                now,
                                now,
                                now,
                            ),
                        )
                        new_id = int(cursor.lastrowid)
                        companions = conn.execute(
                            "SELECT name,needs_accessible,position FROM bird_companions WHERE registration_id=? ORDER BY position",
                            (reg["id"],),
                        ).fetchall()
                        conn.executemany(
                            "INSERT INTO bird_companions(registration_id,name,needs_accessible,position) VALUES(?,?,?,?)",
                            [(new_id, c["name"], c["needs_accessible"], c["position"]) for c in companions],
                        )
                        self._notify(
                            conn,
                            new_id,
                            target_event["id"],
                            "confirmation",
                            {
                                "event_code": target_event["code"],
                                "title": target_event["title"],
                                "start_at": target_event["start_at"],
                                "route_code": target_route["code"],
                                "party_size": reg["party_size"],
                                "transferred_from": event["code"],
                            },
                        )
                        outcome_reg_id = new_id
                        outcome = "transferred"
                        detail = {"target_event_code": target_event["code"], "target_route_code": target_route["code"], "new_registration_id": new_id}
                    else:
                        outcome = "transfer_unavailable_cancelled"
                        detail = {"target_event_code": target_event["code"], "reason": "目标场次无满足承载与无障碍要求的名额"}
                    # 源记录：转移成功则标记 transferred_out；失败则取消并说明，确保受影响者都收到通知
                    conn.execute(
                        "UPDATE bird_registrations SET status=?,route_id=NULL,cancelled_at=?,cancel_reason=?,updated_at=? WHERE id=?",
                        (
                            "transferred_out" if outcome == "transferred" else "event_cancelled",
                            now,
                            reason if outcome == "transferred" else f"{reason}；目标场次名额不足",
                            now,
                            reg["id"],
                        ),
                    )
                    self._notify(
                        conn,
                        reg["id"],
                        event["id"],
                        "event_transfer" if outcome == "transferred" else "event_cancelled",
                        {
                            "source_event_code": event["code"],
                            "route_code": route["code"],
                            "reason": reason,
                            "decision": "transfer",
                            **detail,
                        },
                    )
                    self._log(conn, event["id"], "route.close_transfer", detail, reg["id"], actor)
                    results.append(
                        {"registration_id": reg["id"], "outcome": outcome, "new_registration_id": outcome_reg_id}
                    )
                self._maybe_finish_event(
                    conn,
                    event,
                    action="transfer",
                    reason=reason,
                    actor=actor,
                    target_event_code=target_event["code"],
                )
                refreshed = conn.execute("SELECT * FROM bird_events WHERE id=?", (event["id"],)).fetchone()
                promotions = self._promote_waitlist(conn, refreshed) if refreshed["status"] == "open" else []
            return {
                "event_code": event["code"],
                "route_code": route["code"],
                "action": action,
                "target_event_code": target_event_code,
                "reason": reason,
                "affected_count": len(affected),
                "results": results,
                "promotions": promotions,
            }

    def _maybe_finish_event(
        self,
        conn: sqlite3.Connection,
        event: sqlite3.Row,
        *,
        action: str,
        reason: str,
        actor: str,
        target_event_code: str | None = None,
    ) -> None:
        """若该场次已没有开放路线，整场取消/转移：候补者同样必须收到通知。"""
        open_routes = [r for r in self._event_route_rows(conn, event["id"]) if not r["is_closed"]]
        if open_routes:
            return
        now = _now()
        final_status = "transferred" if action == "transfer" else "cancelled"
        conn.execute(
            "UPDATE bird_events SET status=?,closure_reason=?,updated_at=? WHERE id=?",
            (final_status, reason, now, event["id"]),
        )
        waiting = conn.execute(
            "SELECT * FROM bird_registrations WHERE event_id=? AND status='waitlisted' ORDER BY waitlist_rank",
            (event["id"],),
        ).fetchall()
        if action == "cancel":
            for reg in waiting:
                conn.execute(
                    "UPDATE bird_registrations SET status='event_cancelled',cancelled_at=?,cancel_reason=?,updated_at=? WHERE id=?",
                    (now, reason, now, reg["id"]),
                )
                self._notify(
                    conn,
                    reg["id"],
                    event["id"],
                    "event_cancelled",
                    {"event_code": event["code"], "reason": reason, "decision": "cancel", "had_waitlist_rank": reg["waitlist_rank"]},
                )
                self._log(conn, event["id"], "event.cancel_waitlist", {"waitlist_rank": reg["waitlist_rank"]}, reg["id"], actor)
        else:
            target_event = self._must_event(conn, target_event_code)
            tail = conn.execute(
                "SELECT COALESCE(MAX(waitlist_rank),0) AS m FROM bird_registrations WHERE event_id=? AND status='waitlisted'",
                (target_event["id"],),
            ).fetchone()["m"]
            for offset, reg in enumerate(waiting, start=1):
                already_active = conn.execute(
                    "SELECT 1 FROM bird_registrations WHERE event_id=? AND contact=? "
                    "AND status IN ('confirmed','waitlisted','checked_in')",
                    (target_event["id"], reg["contact"]),
                ).fetchone()
                if already_active is not None:
                    # 联系人在目标场次已有有效报名：不重复占位，仅通知源场次取消
                    conn.execute(
                        "UPDATE bird_registrations SET status='event_cancelled',cancelled_at=?,"
                        "cancel_reason=?,updated_at=? WHERE id=?",
                        (now, f"{reason}；目标场次已有有效报名", now, reg["id"]),
                    )
                    self._notify(
                        conn,
                        reg["id"],
                        event["id"],
                        "event_cancelled",
                        {
                            "source_event_code": event["code"],
                            "decision": "transfer_skipped_duplicate",
                            "target_event_code": target_event["code"],
                            "reason": reason,
                        },
                    )
                    self._log(
                        conn,
                        event["id"],
                        "event.transfer_waitlist_skip_duplicate",
                        {"target_event_code": target_event["code"]},
                        reg["id"],
                        actor,
                    )
                    continue
                new_rank = int(tail) + offset
                cursor = conn.execute(
                    """
                    INSERT INTO bird_registrations(event_id,primary_name,contact,party_size,needs_accessible,
                        preferred_routes_json,idempotency_key,source_registration_id,status,waitlist_rank,
                        created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        target_event["id"],
                        reg["primary_name"],
                        reg["contact"],
                        reg["party_size"],
                        reg["needs_accessible"],
                        reg["preferred_routes_json"],
                        f"transfer-waitlist:{reg['id']}:{target_event['id']}",
                        reg["id"],
                        "waitlisted",
                        new_rank,
                        now,
                        now,
                    ),
                )
                new_id = int(cursor.lastrowid)
                self._notify(
                    conn,
                    new_id,
                    target_event["id"],
                    "waitlist",
                    {
                        "event_code": target_event["code"],
                        "waitlist_rank": new_rank,
                        "transferred_from": event["code"],
                    },
                )
                conn.execute(
                    "UPDATE bird_registrations SET status='transferred_out',updated_at=? WHERE id=?",
                    (now, reg["id"]),
                )
                self._notify(
                    conn,
                    reg["id"],
                    event["id"],
                    "event_transfer",
                    {
                        "source_event_code": event["code"],
                        "decision": "transfer",
                        "target_event_code": target_event["code"],
                        "new_registration_id": new_id,
                        "waitlist_rank": new_rank,
                        "reason": reason,
                    },
                )
                self._log(
                    conn,
                    event["id"],
                    "event.transfer_waitlist",
                    {"target_event_code": target_event["code"], "new_waitlist_rank": new_rank},
                    reg["id"],
                    actor,
                )
        self._log(
            conn,
            event["id"],
            f"event.{action}",
            {"reason": reason, "target_event_code": target_event_code},
            actor=actor,
        )

    # ---------- 轨迹 ----------

    def event_timeline(self, event_code: str) -> dict[str, Any]:
        event = self._must_event(self.connection, event_code)
        logs = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM bird_event_log WHERE event_id=? ORDER BY id", (event["id"],)
            ).fetchall()
        ]
        notifications = [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT n.*, r.primary_name, r.contact FROM bird_notifications n
                JOIN bird_registrations r ON r.id = n.registration_id
                WHERE n.event_id=? ORDER BY n.id
                """,
                (event["id"],),
            ).fetchall()
        ]
        registrations = self.list_event_registrations(event_code)
        return {
            "event": self.get_event(event_code),
            "registrations": registrations,
            "notifications": notifications,
            "logs": logs,
        }
