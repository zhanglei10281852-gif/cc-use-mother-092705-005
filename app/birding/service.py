from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction

# 报名记录状态：
#   confirmed          已确认占位
#   waitlisted         候补中
#   checked_in         已签到（终态占位，不可被递补覆盖）
#   cancelled          报名者主动取消
#   cancelled_by_closure 路线保护关闭后整场取消
# 场次状态：scheduled / closed_pending / cancelled / transferred
# 路线状态：open / closed

SCHEMA = """
CREATE TABLE IF NOT EXISTS bird_routes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK(capacity > 0),
    accessible INTEGER NOT NULL DEFAULT 0 CHECK(accessible IN (0,1)),
    terrain_level INTEGER NOT NULL DEFAULT 1 CHECK(terrain_level BETWEEN 1 AND 3),
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
    closed_reason TEXT NOT NULL DEFAULT '',
    closed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bird_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    route_code TEXT NOT NULL REFERENCES bird_routes(code),
    start_at TEXT NOT NULL,
    leader TEXT NOT NULL DEFAULT '',
    capacity INTEGER NOT NULL CHECK(capacity > 0),
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'scheduled'
        CHECK(status IN ('scheduled','closed_pending','cancelled','transferred')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bird_registrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_code TEXT NOT NULL REFERENCES bird_sessions(code),
    applicant_code TEXT NOT NULL,
    applicant_name TEXT NOT NULL,
    contact TEXT NOT NULL DEFAULT '',
    needs_accessible INTEGER NOT NULL DEFAULT 0 CHECK(needs_accessible IN (0,1)),
    party_size INTEGER NOT NULL CHECK(party_size BETWEEN 1 AND 50),
    party_note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN
        ('confirmed','waitlisted','checked_in','cancelled','cancelled_by_closure')),
    waitlist_seq INTEGER,
    idempotency_key TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(session_code, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_bird_reg_session ON bird_registrations(session_code, status);
CREATE INDEX IF NOT EXISTS idx_bird_reg_applicant ON bird_registrations(applicant_code, session_code);
CREATE TABLE IF NOT EXISTS bird_registration_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    registration_id INTEGER NOT NULL REFERENCES bird_registrations(id) ON DELETE CASCADE,
    session_code TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bird_history_reg ON bird_registration_history(registration_id, id);
CREATE TABLE IF NOT EXISTS bird_route_closures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_code TEXT NOT NULL REFERENCES bird_routes(code),
    reason TEXT NOT NULL,
    closed_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'closed' CHECK(status IN ('closed','reopened')),
    created_at TEXT NOT NULL,
    reopened_at TEXT
);
CREATE TABLE IF NOT EXISTS bird_session_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_code TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('transfer','cancel')),
    target_session_code TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    decided_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bird_decisions_session ON bird_session_decisions(session_code, id);
CREATE TABLE IF NOT EXISTS bird_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_code TEXT NOT NULL,
    registration_id INTEGER REFERENCES bird_registrations(id),
    recipient_code TEXT NOT NULL,
    recipient_name TEXT NOT NULL,
    contact TEXT NOT NULL DEFAULT '',
    ntype TEXT NOT NULL CHECK(ntype IN
        ('confirmed','waitlisted','promoted','cancelled',
         'session_cancelled','session_transferred')),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    ack_status TEXT NOT NULL DEFAULT 'pending' CHECK(ack_status IN ('pending','acknowledged')),
    batch_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    acknowledged_at TEXT,
    acknowledger TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_bird_notify_session ON bird_notifications(session_code, id);
CREATE INDEX IF NOT EXISTS idx_bird_notify_status ON bird_notifications(ack_status, id);
"""

ACTIVE_STATUSES = ("confirmed", "waitlisted", "checked_in")
OCCUPYING_STATUSES = ("confirmed", "checked_in")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


class BirdingService:
    """观鸟导赏报名服务：路线能力、名额策略、候补递补与关闭通知。"""

    def __init__(self, connection: sqlite3.Connection | None = None):
        self.connection = connection or get_connection()
        ensure_schema()

    # ---------- 路线 ----------

    def create_route(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO bird_routes(code,name,capacity,accessible,terrain_level,notes,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (
                        payload["code"], payload["name"], payload["capacity"],
                        1 if payload["accessible"] else 0, payload["terrain_level"],
                        payload.get("notes", ""), now, now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("路线编号已存在", context={"code": payload["code"]}) from exc
            return dict(connection.execute("SELECT * FROM bird_routes WHERE id=?", (cursor.lastrowid,)).fetchone())

    def list_routes(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM bird_routes ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def _route(self, connection: sqlite3.Connection, code: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM bird_routes WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("路线不存在", context={"code": code})
        return row

    def close_route(self, code: str, reason: str, closed_by: str) -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            route = self._route(connection, code)
            if route["status"] == "closed":
                raise ConflictError("路线已处于关闭状态", context={"code": code})
            connection.execute(
                "UPDATE bird_routes SET status='closed', closed_reason=?, closed_at=?, updated_at=? WHERE code=?",
                (reason, now, now, code),
            )
            connection.execute(
                "INSERT INTO bird_route_closures(route_code,reason,closed_by,created_at) VALUES(?,?,?,?)",
                (code, reason, closed_by, now),
            )
            affected = connection.execute(
                "UPDATE bird_sessions SET status='closed_pending', updated_at=? "
                "WHERE route_code=? AND status='scheduled'",
                (now, code),
            ).rowcount
            return {"route": dict(connection.execute("SELECT * FROM bird_routes WHERE code=?", (code,)).fetchone()),
                    "frozen_sessions": affected, "closed_at": now}

    def reopen_route(self, code: str) -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            route = self._route(connection, code)
            if route["status"] != "closed":
                raise ConflictError("路线当前未关闭", context={"code": code})
            connection.execute(
                "UPDATE bird_routes SET status='open', closed_reason='', closed_at=NULL, updated_at=? WHERE code=?",
                (now, code),
            )
            connection.execute(
                "UPDATE bird_route_closures SET status='reopened', reopened_at=? "
                "WHERE route_code=? AND status='closed'",
                (now, code),
            )
            connection.execute(
                "UPDATE bird_sessions SET status='scheduled', updated_at=? "
                "WHERE route_code=? AND status='closed_pending'",
                (now, code),
            )
            return dict(connection.execute("SELECT * FROM bird_routes WHERE code=?", (code,)).fetchone())

    # ---------- 场次 ----------

    def create_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            route = connection.execute("SELECT * FROM bird_routes WHERE code=?", (payload["route_code"],)).fetchone()
            if route is None:
                raise NotFoundError("路线不存在", context={"code": payload["route_code"]})
            try:
                cursor = connection.execute(
                    "INSERT INTO bird_sessions(code,title,route_code,start_at,leader,capacity,notes,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        payload["code"], payload["title"], payload["route_code"], payload["start_at"],
                        payload.get("leader", ""), payload["capacity"], payload.get("notes", ""), now, now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("场次编号已存在", context={"code": payload["code"]}) from exc
            return self._session_view(
                connection.execute("SELECT * FROM bird_sessions WHERE id=?", (cursor.lastrowid,)).fetchone(),
                connection,
            )

    def list_sessions(self) -> list[dict[str, Any]]:
        result = []
        for row in self.connection.execute("SELECT * FROM bird_sessions ORDER BY start_at, id").fetchall():
            result.append(self._session_view(row, self.connection))
        return result

    def get_session(self, code: str) -> dict[str, Any]:
        with transaction() as connection:
            row = connection.execute("SELECT * FROM bird_sessions WHERE code=?", (code,)).fetchone()
            if row is None:
                raise NotFoundError("场次不存在", context={"code": code})
            return self._session_view(row, connection)

    def _session_row(self, connection: sqlite3.Connection, code: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM bird_sessions WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("场次不存在", context={"code": code})
        return row

    def _occupied(self, connection: sqlite3.Connection, session_code: str) -> int:
        row = connection.execute(
            f"SELECT COALESCE(SUM(party_size),0) AS n FROM bird_registrations "
            f"WHERE session_code=? AND status IN ({','.join('?' for _ in OCCUPYING_STATUSES)})",
            (session_code, *OCCUPYING_STATUSES),
        ).fetchone()
        return int(row["n"])

    def _session_view(self, session: sqlite3.Row, connection: sqlite3.Connection) -> dict[str, Any]:
        route = connection.execute("SELECT * FROM bird_routes WHERE code=?", (session["route_code"],)).fetchone()
        data = dict(session)
        data["route_accessible"] = bool(route["accessible"])
        data["route_capacity"] = route["capacity"]
        data["effective_capacity"] = min(int(session["capacity"]), int(route["capacity"]))
        data["occupied"] = self._occupied(connection, session["code"])
        data["waitlisted"] = connection.execute(
            "SELECT COUNT(*) AS n FROM bird_registrations WHERE session_code=? AND status='waitlisted'",
            (session["code"],),
        ).fetchone()["n"]
        return data

    # ---------- 报名 ----------

    def register(self, session_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            session = self._session_row(connection, session_code)
            if session["status"] != "scheduled":
                raise ConflictError("该场次当前不接受报名",
                                    context={"session": session_code, "status": session["status"]})
            route = self._route(connection, session["route_code"])

            # 重复请求：携带相同幂等键时原样返回既有记录，不产生第二条占位
            idem_key = payload.get("idempotency_key")
            if idem_key:
                replay = connection.execute(
                    "SELECT * FROM bird_registrations WHERE session_code=? AND idempotency_key=?",
                    (session_code, idem_key),
                ).fetchone()
                if replay is not None:
                    view = self._registration_view(replay, connection)
                    view["replayed"] = True
                    return view

            # 路线能力检查：需要无障碍路线的报名者不能安排到无能力路线
            if payload["needs_accessible"] and not route["accessible"]:
                raise ValidationError("该路线不支持无障碍通行，请选择无障碍路线场次",
                                      context={"route": route["code"], "accessible": False})

            # 重复占位检查：同一报名者在同一场次只允许一条生效中的记录
            duplicate = connection.execute(
                f"SELECT * FROM bird_registrations WHERE session_code=? AND applicant_code=? "
                f"AND status IN ({','.join('?' for _ in ACTIVE_STATUSES)})",
                (session_code, payload["applicant_code"], *ACTIVE_STATUSES),
            ).fetchone()
            if duplicate is not None:
                raise ConflictError("该报名者在此场次已有生效中的报名记录，请勿重复占位",
                                    context={"registration_id": duplicate["id"], "status": duplicate["status"]})

            effective_capacity = min(int(session["capacity"]), int(route["capacity"]))
            occupied = self._occupied(connection, session_code)
            party = int(payload["party_size"])
            if occupied + party <= effective_capacity:
                status, seq = "confirmed", None
                ntype, title = "confirmed", f"【观鸟导赏】{session['title']} 报名确认"
                body = (f"您报名的 {session['start_at']}「{session['title']}」已确认，"
                        f"同行 {party} 人，路线：{route['name']}。请准时集合。")
            else:
                status = "waitlisted"
                seq_row = connection.execute(
                    "SELECT COALESCE(MAX(waitlist_seq),0)+1 AS next_seq FROM bird_registrations WHERE session_code=?",
                    (session_code,),
                ).fetchone()
                seq = int(seq_row["next_seq"])
                ntype, title = "waitlisted", f"【观鸟导赏】{session['title']} 进入候补"
                body = (f"您报名的 {session['start_at']}「{session['title']}」当前名额已满，"
                        f"候补序号 {seq}，将按报名先后顺序递补并通知您。")

            cursor = connection.execute(
                "INSERT INTO bird_registrations(session_code,applicant_code,applicant_name,contact,"
                "needs_accessible,party_size,party_note,status,waitlist_seq,idempotency_key,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    session_code, payload["applicant_code"], payload["applicant_name"], payload.get("contact", ""),
                    1 if payload["needs_accessible"] else 0, party, payload.get("party_note", ""),
                    status, seq, idem_key, now, now,
                ),
            )
            reg_id = cursor.lastrowid
            connection.execute(
                "INSERT INTO bird_registration_history(registration_id,session_code,action,actor,detail_json,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (reg_id, session_code, f"registered_{status}", payload["applicant_code"],
                 json.dumps({"party_size": party, "waitlist_seq": seq}, ensure_ascii=False), now),
            )
            self._notify(connection, session_code, reg_id, payload["applicant_code"], payload["applicant_name"],
                         payload.get("contact", ""), ntype, title, body, now)
            return self._registration_view(
                connection.execute("SELECT * FROM bird_registrations WHERE id=?", (reg_id,)).fetchone(), connection)

    def list_registrations(self, session_code: str) -> dict[str, Any]:
        with transaction() as connection:
            session = self._session_row(connection, session_code)
            rows = connection.execute(
                "SELECT * FROM bird_registrations WHERE session_code=? "
                "ORDER BY CASE status WHEN 'checked_in' THEN 0 WHEN 'confirmed' THEN 1 ELSE 2 END, "
                "waitlist_seq, id",
                (session_code,),
            ).fetchall()
            return {"session": self._session_view(session, connection),
                    "registrations": [self._registration_view(row, connection) for row in rows]}

    def get_registration(self, registration_id: int) -> dict[str, Any]:
        with transaction() as connection:
            row = connection.execute("SELECT * FROM bird_registrations WHERE id=?", (registration_id,)).fetchone()
            if row is None:
                raise NotFoundError("报名记录不存在", context={"id": registration_id})
            return self._registration_view(row, connection, include_history=True)

    def _registration_view(self, row: sqlite3.Row, connection: sqlite3.Connection,
                           include_history: bool = False) -> dict[str, Any]:
        data = dict(row)
        data["needs_accessible"] = bool(row["needs_accessible"])
        if include_history:
            data["history"] = [
                dict(item) for item in connection.execute(
                    "SELECT * FROM bird_registration_history WHERE registration_id=? ORDER BY id", (row["id"],)
                ).fetchall()
            ]
        return data

    # ---------- 取消与签到 ----------

    def cancel_registration(self, registration_id: int, reason: str, cancelled_by: str) -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM bird_registrations WHERE id=?", (registration_id,)).fetchone()
            if row is None:
                raise NotFoundError("报名记录不存在", context={"id": registration_id})
            # 已取消 / 因关闭取消：重复到达的取消请求直接回显，不改变状态
            if row["status"] in ("cancelled", "cancelled_by_closure"):
                view = self._registration_view(row, connection)
                view["replayed"] = True
                return view
            # 已签到记录为终态，不允许取消，也绝不允许后续递补覆盖
            if row["status"] == "checked_in":
                raise ConflictError("已签到的记录不能取消", context={"registration_id": registration_id})

            session = self._session_row(connection, row["session_code"])
            previous_status = row["status"]
            connection.execute(
                "UPDATE bird_registrations SET status='cancelled', updated_at=? WHERE id=?", (now, registration_id)
            )
            connection.execute(
                "INSERT INTO bird_registration_history(registration_id,session_code,action,actor,detail_json,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (registration_id, row["session_code"], "cancel", cancelled_by,
                 json.dumps({"previous_status": previous_status, "reason": reason}, ensure_ascii=False), now),
            )
            self._notify(connection, row["session_code"], registration_id, row["applicant_code"],
                         row["applicant_name"], row["contact"], "cancelled",
                         "【观鸟导赏】取消报名成功",
                         f"您已取消「{session['title']}」的报名。" +
                         (f"原因：{reason}。" if reason else ""), now)
            promoted: list[dict[str, Any]] = []
            # 仅正常场次触发递补；路线关闭待定期间冻结名额，等待整场决定
            if previous_status == "confirmed" and session["status"] == "scheduled":
                promoted = self._promote(connection, session, now)
            view = self._registration_view(
                connection.execute("SELECT * FROM bird_registrations WHERE id=?", (registration_id,)).fetchone(),
                connection, include_history=True)
            view["promoted"] = promoted
            return view

    def check_in(self, registration_id: int) -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM bird_registrations WHERE id=?", (registration_id,)).fetchone()
            if row is None:
                raise NotFoundError("报名记录不存在", context={"id": registration_id})
            if row["status"] == "checked_in":
                view = self._registration_view(row, connection)
                view["replayed"] = True
                return view
            if row["status"] != "confirmed":
                raise ConflictError("只有已确认的报名可以签到",
                                    context={"registration_id": registration_id, "status": row["status"]})
            connection.execute(
                "UPDATE bird_registrations SET status='checked_in', updated_at=? WHERE id=?", (now, registration_id)
            )
            connection.execute(
                "INSERT INTO bird_registration_history(registration_id,session_code,action,actor,detail_json,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (registration_id, row["session_code"], "check_in", "volunteer", "{}", now),
            )
            return self._registration_view(
                connection.execute("SELECT * FROM bird_registrations WHERE id=?", (registration_id,)).fetchone(),
                connection, include_history=True)

    # ---------- 候补递补 ----------

    def _promote(self, connection: sqlite3.Connection, session: sqlite3.Row, now: str) -> list[dict[str, Any]]:
        """严格按候补序号递补；队首同行人数放不下时不跳过，保证既定优先级不被破坏。"""
        route = self._route(connection, session["route_code"])
        effective_capacity = min(int(session["capacity"]), int(route["capacity"]))
        promoted: list[dict[str, Any]] = []
        while True:
            occupied = self._occupied(connection, session["code"])
            head = connection.execute(
                "SELECT * FROM bird_registrations WHERE session_code=? AND status='waitlisted' "
                "ORDER BY waitlist_seq, id LIMIT 1",
                (session["code"],),
            ).fetchone()
            if head is None:
                break
            if occupied + int(head["party_size"]) > effective_capacity:
                break
            connection.execute(
                "UPDATE bird_registrations SET status='confirmed', updated_at=? WHERE id=? AND status='waitlisted'",
                (now, head["id"]),
            )
            connection.execute(
                "INSERT INTO bird_registration_history(registration_id,session_code,action,actor,detail_json,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (head["id"], session["code"], "promoted", "system",
                 json.dumps({"from_waitlist_seq": head["waitlist_seq"]}, ensure_ascii=False), now),
            )
            self._notify(connection, session["code"], head["id"], head["applicant_code"], head["applicant_name"],
                         head["contact"], "promoted",
                         f"【观鸟导赏】{session['title']} 候补晋升确认",
                         f"好消息！您在「{session['title']}」的候补（序号 {head['waitlist_seq']}）已晋升为正式名额，"
                         f"同行 {head['party_size']} 人，请准时参加。", now)
            promoted.append({"registration_id": head["id"], "applicant_code": head["applicant_code"],
                             "waitlist_seq": head["waitlist_seq"]})
        return promoted

    # ---------- 路线保护关闭：整场转移 / 取消 ----------

    def decide_closure(self, session_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        action = payload["action"]
        with transaction(immediate=True) as connection:
            session = self._session_row(connection, session_code)
            if session["status"] != "closed_pending":
                raise ConflictError("只有因路线保护关闭而待定的场次可以执行整场转移或取消",
                                    context={"session": session_code, "status": session["status"]})
            if action == "cancel":
                return self._cancel_session(connection, session, payload, now)
            return self._transfer_session(connection, session, payload, now)

    def _cancel_session(self, connection: sqlite3.Connection, session: sqlite3.Row,
                        payload: dict[str, Any], now: str) -> dict[str, Any]:
        reason = payload.get("reason", "")
        # 已签到者保留 checked_in 终态（仍受影响、仍需通知）；已确认/候补者转为关闭取消
        affected = connection.execute(
            "SELECT * FROM bird_registrations WHERE session_code=? AND status IN ('confirmed','waitlisted')",
            (session["code"],),
        ).fetchall()
        for reg in affected:
            connection.execute(
                "UPDATE bird_registrations SET status='cancelled_by_closure', updated_at=? WHERE id=?",
                (now, reg["id"]),
            )
            connection.execute(
                "INSERT INTO bird_registration_history(registration_id,session_code,action,actor,detail_json,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (reg["id"], session["code"], "session_cancelled", payload["decided_by"],
                 json.dumps({"reason": reason}, ensure_ascii=False), now),
            )
        connection.execute(
            "UPDATE bird_sessions SET status='cancelled', updated_at=? WHERE code=?", (now, session["code"])
        )
        connection.execute(
            "INSERT INTO bird_session_decisions(session_code,action,reason,decided_by,created_at) "
            "VALUES(?,?,?,?,?)",
            (session["code"], "cancel", reason, payload["decided_by"], now),
        )
        notified = self._notify_affected(
            connection, session, "session_cancelled",
            f"【观鸟导赏】{session['title']} 因路线保护关闭整场取消",
            f"很抱歉，由于路线临时保护关闭，{session['start_at']} 的「{session['title']}」整场取消。"
            + (f"原因：{reason}。" if reason else "") + "如有疑问请联系志愿者团队。",
            now,
        )
        return {"decision": "cancel", "session": self._session_view(
            connection.execute("SELECT * FROM bird_sessions WHERE code=?", (session["code"],)).fetchone(), connection),
                "notifications": notified}

    def _transfer_session(self, connection: sqlite3.Connection, session: sqlite3.Row,
                          payload: dict[str, Any], now: str) -> dict[str, Any]:
        target_code = payload.get("target_session_code")
        if not target_code:
            raise ValidationError("整场转移必须指定承接场次", context={"target_session_code": None})
        target = connection.execute("SELECT * FROM bird_sessions WHERE code=?", (target_code,)).fetchone()
        if target is None:
            raise NotFoundError("承接场次不存在", context={"code": target_code})
        if target["code"] == session["code"]:
            raise ValidationError("承接场次不能是当前场次本身")
        if target["status"] != "scheduled":
            raise ConflictError("承接场次当前不可接收转移",
                                context={"target": target_code, "status": target["status"]})
        target_route = self._route(connection, target["route_code"])
        if target_route["status"] != "open":
            raise ConflictError("承接场次的路线同样处于关闭状态", context={"route": target_route["code"]})

        moving = connection.execute(
            "SELECT * FROM bird_registrations WHERE session_code=? "
            "AND status IN ('confirmed','checked_in','waitlisted') ORDER BY id",
            (session["code"],),
        ).fetchall()

        # 重复占位：即将转移过来的人不能在承接场次已有生效记录
        codes = [reg["applicant_code"] for reg in moving]
        conflicts: list[str] = []
        if codes:
            placeholders = ",".join("?" for _ in codes)
            conflicts = [row["applicant_code"] for row in connection.execute(
                f"SELECT DISTINCT applicant_code FROM bird_registrations "
                f"WHERE session_code=? AND status IN ({','.join('?' for _ in ACTIVE_STATUSES)}) "
                f"AND applicant_code IN ({placeholders})",
                (target_code, *ACTIVE_STATUSES, *codes),
            ).fetchall()]
        if conflicts:
            raise ConflictError("部分报名者在承接场次已有占位，无法自动转移",
                                context={"applicants": conflicts})

        # 承载检查：承接场次路线承载与现场名额必须容纳全部转移占用
        moving_occupancy = sum(int(reg["party_size"]) for reg in moving
                               if reg["status"] in OCCUPYING_STATUSES)
        target_capacity = min(int(target["capacity"]), int(target_route["capacity"]))
        target_occupied = self._occupied(connection, target_code)
        if target_occupied + moving_occupancy > target_capacity:
            raise ConflictError("承接场次名额不足，无法整场转移", context={
                "target_effective_capacity": target_capacity,
                "target_occupied": target_occupied,
                "moving_occupancy": moving_occupancy,
            })

        # 无障碍能力检查：转移者中有无障碍需求时，承接路线必须具备能力
        needs = [reg for reg in moving if reg["needs_accessible"]]
        if needs and not target_route["accessible"]:
            raise ValidationError("转移名单中含无障碍需求者，承接路线不支持无障碍通行",
                                  context={"applicants": [reg["applicant_code"] for reg in needs]})

        seq_row = connection.execute(
            "SELECT COALESCE(MAX(waitlist_seq),0) AS m FROM bird_registrations WHERE session_code=?",
            (target_code,),
        ).fetchone()
        next_seq = int(seq_row["m"])
        notified: list[dict[str, Any]] = []
        for reg in moving:
            # 已签到为终态，转移时同样保留 checked_in，不被任何流程覆盖
            if reg["status"] == "waitlisted":
                next_seq += 1
                new_status, new_seq = "waitlisted", next_seq
            else:
                new_status, new_seq = reg["status"], None
            connection.execute(
                "INSERT INTO bird_registration_history(registration_id,session_code,action,actor,detail_json,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (reg["id"], session["code"], "transferred_out", payload["decided_by"],
                 json.dumps({"target_session_code": target_code, "previous_status": reg["status"]},
                            ensure_ascii=False), now),
            )
            connection.execute(
                "UPDATE bird_registrations SET session_code=?, status=?, "
                "waitlist_seq=?, updated_at=? WHERE id=?",
                (target_code, new_status,
                 next_seq if new_status == "waitlisted" else None, now, reg["id"]),
            )
            connection.execute(
                "INSERT INTO bird_registration_history(registration_id,session_code,action,actor,detail_json,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (reg["id"], target_code, "transferred_in", payload["decided_by"],
                 json.dumps({"from_session_code": session["code"]}, ensure_ascii=False), now),
            )
            notification = self._notify(
                connection, session["code"], reg["id"], reg["applicant_code"], reg["applicant_name"], reg["contact"],
                "session_transferred",
                f"【观鸟导赏】{session['title']} 已整场转移",
                f"由于原路线临时保护关闭，您的报名已转移至「{target['title']}」"
                f"（{target['start_at']}，路线：{target_route['name']}），当前状态："
                + ("候补" if new_status == "waitlisted" else "已确认") + "。",
                now,
            )
            notified.append(notification)

        # 转移完成后若承接场次仍有名额，按合并后的候补序号统一递补：
        # 承接场次原有候补序号更早，优先于转移过来的候补，符合既定优先级
        promoted = self._promote(
            connection,
            connection.execute("SELECT * FROM bird_sessions WHERE code=?", (target_code,)).fetchone(),
            now,
        )

        connection.execute(
            "UPDATE bird_sessions SET status='transferred', updated_at=? WHERE code=?", (now, session["code"])
        )
        connection.execute(
            "INSERT INTO bird_session_decisions(session_code,action,target_session_code,reason,decided_by,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (session["code"], "transfer", target_code, payload.get("reason", ""), payload["decided_by"], now),
        )
        return {"decision": "transfer", "target_session_code": target_code,
                "promoted": promoted,
                "session": self._session_view(
                    connection.execute("SELECT * FROM bird_sessions WHERE code=?", (session["code"],)).fetchone(),
                    connection),
                "target_session": self._session_view(
                    connection.execute("SELECT * FROM bird_sessions WHERE code=?", (target_code,)).fetchone(),
                    connection),
                "notifications": notified}

    def _notify_affected(self, connection: sqlite3.Connection, session: sqlite3.Row, ntype: str,
                         title: str, body: str, now: str) -> list[dict[str, Any]]:
        # 受影响者：确认、已签到、候补（含刚转为关闭取消的记录），已主动取消者不再通知
        rows = connection.execute(
            "SELECT * FROM bird_registrations WHERE session_code=? "
            "AND status IN ('confirmed','checked_in','waitlisted','cancelled_by_closure') ORDER BY id",
            (session["code"],),
        ).fetchall()
        notified = []
        for reg in rows:
            notified.append(self._notify(connection, session["code"], reg["id"], reg["applicant_code"],
                                         reg["applicant_name"], reg["contact"], ntype, title, body, now))
        return notified

    # ---------- 通知与回执 ----------

    def _notify(self, connection: sqlite3.Connection, session_code: str, registration_id: int | None,
                recipient_code: str, recipient_name: str, contact: str, ntype: str,
                title: str, body: str, now: str, batch_key: str = "") -> dict[str, Any]:
        cursor = connection.execute(
            "INSERT INTO bird_notifications(session_code,registration_id,recipient_code,recipient_name,contact,"
            "ntype,title,body,created_at,batch_key) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (session_code, registration_id, recipient_code, recipient_name, contact, ntype, title, body, now,
             batch_key),
        )
        return dict(connection.execute("SELECT * FROM bird_notifications WHERE id=?", (cursor.lastrowid,)).fetchone())

    def list_notifications(self, session_code: str | None = None, ack_status: str | None = None,
                           recipient_code: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM bird_notifications WHERE 1=1"
        params: list[Any] = []
        if session_code:
            sql += " AND session_code=?"
            params.append(session_code)
        if ack_status:
            sql += " AND ack_status=?"
            params.append(ack_status)
        if recipient_code:
            sql += " AND recipient_code=?"
            params.append(recipient_code)
        sql += " ORDER BY id"
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    def acknowledge(self, notification_id: int, acknowledger: str) -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM bird_notifications WHERE id=?", (notification_id,)).fetchone()
            if row is None:
                raise NotFoundError("通知不存在", context={"id": notification_id})
            if row["ack_status"] != "acknowledged":
                connection.execute(
                    "UPDATE bird_notifications SET ack_status='acknowledged', acknowledged_at=?, acknowledger=? "
                    "WHERE id=?",
                    (now, acknowledger, notification_id),
                )
            view = dict(connection.execute("SELECT * FROM bird_notifications WHERE id=?", (notification_id,)).fetchone())
            view["replayed"] = row["ack_status"] == "acknowledged"
            return view

    # ---------- 轨迹 ----------

    def timeline(self, session_code: str) -> dict[str, Any]:
        with transaction() as connection:
            session = self._session_row(connection, session_code)
            decisions = [dict(row) for row in connection.execute(
                "SELECT * FROM bird_session_decisions WHERE session_code=? ORDER BY id", (session_code,)).fetchall()]
            history = [dict(row) for row in connection.execute(
                "SELECT * FROM bird_registration_history WHERE session_code=? ORDER BY id", (session_code,)).fetchall()]
            notifications = [dict(row) for row in connection.execute(
                "SELECT * FROM bird_notifications WHERE session_code=? ORDER BY id", (session_code,)).fetchall()]
            pending_acks = sum(1 for item in notifications if item["ack_status"] == "pending")
            return {"session": self._session_view(session, connection), "decisions": decisions,
                    "history": history, "notifications": notifications,
                    "notification_summary": {"total": len(notifications), "pending": pending_acks,
                                             "acknowledged": len(notifications) - pending_acks}}
