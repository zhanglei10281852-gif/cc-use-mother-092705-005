from __future__ import annotations

import argparse
import json

from fastapi.testclient import TestClient

from app.database import database_path, get_connection, init_db
from app.main import app


def command_init() -> int:
    init_db()
    print(json.dumps({"database": str(database_path()), "status": "initialized"}, ensure_ascii=False))
    return 0


def command_check() -> int:
    init_db()
    connection = get_connection()
    result = {
        "database": str(database_path()),
        "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
        "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
        "tables": connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0],
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["integrity"] == "ok" and result["foreign_keys"] == 1 else 1


def command_smoke() -> int:
    with TestClient(app) as client:
        root = client.get("/")
        health = client.get("/api/system/health")
    result = {"root": root.json(), "health": health.json(), "status_codes": [root.status_code, health.status_code]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status_codes"] == [200, 200] else 1


def command_compute_demo() -> int:
    template = {
        "code": "monte-carlo-demo",
        "name": "蒙特卡洛演示",
        "algorithm": "monte-carlo",
        "parameter_schema": {
            "samples": {"type": "integer", "required": True, "minimum": 10, "maximum": 1000000},
            "seed": {"type": "integer", "required": True},
        },
        "default_parameters": {},
        "max_runtime_seconds": 60,
        "max_attempts": 3,
    }
    with TestClient(app) as client:
        created = client.post("/api/compute/templates?actor=cli-demo", json=template)
        if created.status_code not in {201, 409}:
            print(created.text)
            return 1
        task = client.post(
            "/api/compute/tasks",
            json={
                "template_code": "monte-carlo-demo",
                "project_code": "demo",
                "requested_by": "cli-user",
                "parameters": {"samples": 1000, "seed": 42},
                "priority": 80,
                "idempotency_key": "compute-demo-000001",
            },
        )
        claimed = client.post(
            "/api/compute/tasks/claim",
            json={"worker_id": "cli-worker", "capabilities": ["monte-carlo"], "lease_seconds": 60},
        )
    result = {"task": task.status_code, "claimed": claimed.status_code, "task_id": task.json().get("id")}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if task.status_code == 202 and claimed.status_code == 200 and claimed.json().get("task") else 1


def command_birding_demo() -> int:
    """观鸟导赏报名服务端到端剧情：候补晋升、通知回执、路线关闭后的整场转移/取消。"""
    import os
    from pathlib import Path

    demo_db = Path(__file__).resolve().parent.parent / "data" / "birding-demo.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(demo_db) + suffix).unlink(missing_ok=True)
    os.environ["TOWNSHIP_DATABASE_PATH"] = str(demo_db)
    from app.database import close_connection
    close_connection()

    trace: dict[str, object] = {"steps": []}

    def record(label: str, response) -> dict:
        step = {"action": label, "status_code": response.status_code, "data": response.json()}
        trace["steps"].append(step)
        assert response.status_code in {200, 201}, response.text
        return response.json()

    with TestClient(app) as client:
        # 1) 路线能力：湿地栈道（无障碍，承载4）、替代湿地（无障碍，承载30）、草海小径（承载2）
        record("create_route_wetland", client.post("/api/birding/routes", json={
            "code": "L-WET", "name": "湿地木栈道", "capacity": 4, "accessible": True, "terrain_level": 1}))
        record("create_route_alternative", client.post("/api/birding/routes", json={
            "code": "L-ALT", "name": "替代湿地环线", "capacity": 30, "accessible": True, "terrain_level": 1}))
        record("create_route_meadow", client.post("/api/birding/routes", json={
            "code": "L-MEADOW", "name": "草海小径", "capacity": 2, "accessible": False, "terrain_level": 2}))

        # 2) 不同条件的场次
        record("create_session_saturday", client.post("/api/birding/sessions", json={
            "code": "S-SAT", "title": "周六清晨观鸟导赏", "route_code": "L-WET",
            "start_at": "2026-10-10T08:00:00+08:00", "capacity": 4, "leader": "志愿者阿岚"}))
        record("create_session_alternative", client.post("/api/birding/sessions", json={
            "code": "S-ALT", "title": "周日替代湿地观鸟", "route_code": "L-ALT",
            "start_at": "2026-10-11T08:30:00+08:00", "capacity": 30, "leader": "志愿者阿岚"}))
        record("create_session_meadow", client.post("/api/birding/sessions", json={
            "code": "S-MEADOW", "title": "草海专场", "route_code": "L-MEADOW",
            "start_at": "2026-10-10T15:00:00+08:00", "capacity": 2}))

        # 3) 报名：家庭3人占3席、个人1席占满，其后两组进入候补
        register = lambda sid, code, name, **kw: client.post(
            f"/api/birding/sessions/{sid}/registrations",
            json={"applicant_code": code, "applicant_name": name,
                  "contact": f"{code}@example.com", **kw})
        wang = record("register_wang_family_3", register("S-SAT", "wang", "王女士", party_size=3,
                                                         party_note="父母二人携带一名儿童"))
        li = record("register_li_1", register("S-SAT", "li", "李先生"))
        zhao = record("register_zhao_waitlist_2", register("S-SAT", "zhao", "赵先生", party_size=2))
        qian = record("register_qian_waitlist_1", register("S-SAT", "qian", "钱同学"))
        assert zhao["status"] == qian["status"] == "waitlisted"

        # 重复请求：相同幂等键不产生第二条占位
        replay = record("register_idempotent_replay", client.post(
            "/api/birding/sessions/S-SAT/registrations",
            json={"applicant_code": "sun", "applicant_name": "孙先生", "idempotency_key": "idem-sun-1"}))
        record("register_idempotent_replay_again", client.post(
            "/api/birding/sessions/S-SAT/registrations",
            json={"applicant_code": "sun", "applicant_name": "孙先生", "idempotency_key": "idem-sun-1"}))
        # 孙先生实际在候补更后面（无名额），记录其 id 供观察；不影响主剧情
        del replay

        # 4) 李先生签到（终态）
        record("check_in_li", client.post(f"/api/birding/registrations/{li['id']}/check-in"))

        # 5) 王女士家庭退出，按候补优先级递补：赵先生(2人)→钱同学(1人) 恰好补满释放的3席
        cancel = record("cancel_wang_promotes_waitlist",
                        client.post(f"/api/birding/registrations/{wang['id']}/cancel",
                                    json={"reason": "孩子临时身体不适"}))
        trace["promotion_order"] = [p["applicant_code"] for p in cancel["promoted"]]
        assert trace["promotion_order"] == ["zhao", "qian"]

        # 6) 路线因保护临时关闭：周六场冻结，不允许继续报名与自动递补
        record("close_wetland_route", client.post("/api/birding/routes/L-WET/close",
                                                  json={"reason": "发现濒危水鸟繁殖巢，临时封闭管理"}))

        # 7) 整场转移到替代场：校验承载、无障碍能力与重复占位后逐人转移并通知
        decision = record("transfer_saturday_to_alternative", client.post(
            "/api/birding/sessions/S-SAT/closure-decision",
            json={"action": "transfer", "target_session_code": "S-ALT",
                  "reason": "原路线保护关闭，改走替代湿地环线", "decided_by": "保护站"}))
        trace["transfer_target_occupied"] = decision["target_session"]["occupied"]

        # 8) 草海专场演示整场取消：通知到每一位受影响者
        zhou1 = record("register_zhou_meadow", register("S-MEADOW", "zhou1", "周先生"))
        zhou2 = record("register_wu_meadow", register("S-MEADOW", "wu2", "吴女士"))
        record("close_meadow_route", client.post("/api/birding/routes/L-MEADOW/close",
                                                 json={"reason": "草海水位异常上涨"}))
        record("cancel_meadow_session", client.post("/api/birding/sessions/S-MEADOW/closure-decision",
                                                    json={"action": "cancel", "reason": "安全考虑",
                                                          "decided_by": "保护站"}))

        # 9) 通知回执：把周六场所有通知逐条确认
        notes = client.get("/api/birding/notifications", params={"session_code": "S-SAT"}).json()["notifications"]
        acked = 0
        for note in notes:
            ack = client.post(f"/api/birding/notifications/{note['id']}/acknowledge",
                              json={"acknowledger": note["recipient_code"]})
            assert ack.status_code == 200 and ack.json()["ack_status"] == "acknowledged"
            acked += 1
        trace["saturday_notifications_acknowledged"] = acked

        # 10) 完整轨迹
        trace["saturday_timeline"] = client.get("/api/birding/sessions/S-SAT/timeline").json()
        trace["saturday_final"] = client.get("/api/birding/sessions/S-SAT/registrations").json()
        trace["meadow_final"] = client.get("/api/birding/sessions/S-MEADOW/registrations").json()

    print(json.dumps(trace, ensure_ascii=False, indent=2))
    summary = trace["saturday_timeline"]["notification_summary"]
    ok = (trace["promotion_order"] == ["zhao", "qian"]
          and trace["saturday_final"]["session"]["status"] == "transferred"
          and trace["meadow_final"]["session"]["status"] == "cancelled"
          and summary["pending"] == 0 and summary["acknowledged"] == summary["total"])
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="compute-operations", description="科学计算任务运营服务维护入口")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="初始化 SQLite 数据库")
    subparsers.add_parser("check-db", help="检查数据库完整性")
    subparsers.add_parser("smoke", help="执行本地 API 冒烟检查")
    subparsers.add_parser("compute-demo", help="执行计算任务提交与领取演示")
    subparsers.add_parser("birding-demo", help="执行观鸟导赏报名完整剧情演示")
    args = parser.parse_args()
    return {
        "init-db": command_init,
        "check-db": command_check,
        "smoke": command_smoke,
        "compute-demo": command_compute_demo,
        "birding-demo": command_birding_demo,
    }[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
