"""观鸟导赏报名端到端演示：场次、候补晋升、通知回执、整场转移与取消轨迹。

用法：
    python tools/birding_demo.py
脚本使用独立的临时 SQLite 数据库，不影响正式数据。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def _show(title: str, payload) -> None:
    print(f"\n=== {title} ===")
    if isinstance(payload, (dict, list)):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(payload)


def main() -> None:
    tmp_dir = tempfile.mkdtemp(prefix="birding-demo-")
    os.environ["TOWNSHIP_DATABASE_PATH"] = str(Path(tmp_dir) / "demo.db")

    from fastapi.testclient import TestClient

    from app.database import close_connection
    from app.main import app

    with TestClient(app) as client:
        # 1. 路线：普通堤岸线 + 无障碍木栈道 + 河滨小径（专供取消演练场）
        for route in [
            {"code": "DIKE", "name": "堤岸观鸟线", "capacity": 20, "difficulty": "moderate"},
            {"code": "BOARDWALK", "name": "无障碍木栈道", "capacity": 15, "wheelchair_accessible": True},
            {"code": "RIVER", "name": "河滨小径", "capacity": 20, "difficulty": "easy"},
        ]:
            client.post("/api/birding/routes", json=route)

        # 2. 三场不同条件的活动：周六（小名额+无障碍配额）、周日备选场、单路线封闭演练场
        events = [
            {
                "code": "SAT-AM",
                "title": "周六上午湿地观鸟导赏",
                "start_at": "2026-10-17T08:30:00+08:00",
                "route_codes": ["DIKE", "BOARDWALK"],
                "capacity_per_route": 3,
                "accessible_capacity": 1,
                "waitlist_capacity": 5,
            },
            {
                "code": "SUN-AM",
                "title": "周日上午备选导赏",
                "start_at": "2026-10-18T08:30:00+08:00",
                "route_codes": ["BOARDWALK"],
                "capacity_per_route": 8,
                "accessible_capacity": 3,
                "waitlist_capacity": 5,
            },
            {
                "code": "MON-PM",
                "title": "周一下午取消演练场",
                "start_at": "2026-10-19T14:00:00+08:00",
                "route_codes": ["RIVER"],
                "capacity_per_route": 3,
                "waitlist_capacity": 3,
            },
        ]
        for event in events:
            client.post("/api/birding/events", json=event)

        # 3. 报名：亲子二人组占满 DIKE 名额；轮椅使用者走木栈道；再来人进入候补
        regs = {}

        def register(key: str, **payload):
            body = {
                "event_code": "SAT-AM",
                "primary_name": key,
                "contact": f"{key}@example.cn",
                "party_size": 1,
                "companions": [],
                "idempotency_key": key,
            }
            body.update(payload)
            response = client.post("/api/birding/registrations", json=body)
            assert response.status_code == 201, response.text
            regs[key] = response.json()
            return response.json()

        register("family", primary_name="李家庭", party_size=2,
                 companions=[{"name": "李小娃", "requires_accessible": False}],
                 preferred_route_codes=["DIKE"])
        register("family2", primary_name="王家庭", party_size=1, preferred_route_codes=["DIKE"])
        register("wheelchair", primary_name="陈先生", requires_accessible=True,
                 preferred_route_codes=["DIKE"])
        register("solo", primary_name="张同学")
        w1 = register("wait1", primary_name="候补一号", party_size=2,
                      companions=[{"name": "家人", "requires_accessible": False}])
        w2 = register("wait2", primary_name="候补二号")
        _show(
            "报名后候补队列",
            {
                "family": {"status": regs["family"]["status"], "route": regs["family"]["route_code"]},
                "wheelchair": {"status": regs["wheelchair"]["status"], "route": regs["wheelchair"]["route_code"]},
                "wait1": {"status": w1["status"], "rank": w1["waitlist_rank"]},
                "wait2": {"status": w2["status"], "rank": w2["waitlist_rank"]},
            },
        )

        # 4. 重复报名请求幂等
        replay_body = {
            "event_code": "SAT-AM",
            "primary_name": "李家庭",
            "contact": "family@example.cn",
            "party_size": 2,
            "companions": [{"name": "李小娃", "requires_accessible": False}],
            "preferred_route_codes": ["DIKE"],
            "idempotency_key": "family",
        }
        replay = client.post("/api/birding/registrations", json=replay_body).json()
        _show("重复请求幂等返回", {"replayed": replay["idempotent_replayed"], "same_id": replay["id"] == regs["family"]["id"]})

        # 5. 亲子组退出 -> 候补严格按序晋升
        cancel = client.post(
            f"/api/birding/registrations/{regs['family']['id']}/cancel",
            json={"reason": "孩子临时发烧"},
        ).json()
        _show("亲子组退出触发的候补晋升", cancel["promotions"])

        # 签到记录不可被取消或覆盖
        client.post(f"/api/birding/registrations/{regs['family2']['id']}/check-in", json={})
        immune = client.post(
            f"/api/birding/registrations/{regs['family2']['id']}/cancel", json={"reason": "x"}
        )
        _show("已签到记录拒绝取消", {"http_status": immune.status_code, "error": immune.json()["error"]["code"]})

        # 6. 通知投递 + 受影响者回执
        dispatch = client.post("/api/birding/notifications/dispatch?event_code=SAT-AM").json()
        notifications = client.get("/api/birding/notifications?event_code=SAT-AM").json()["notifications"]
        promotion_notice = next(n for n in notifications if n["type"] == "promotion")
        ack = client.post(f"/api/birding/notifications/{promotion_notice['id']}/ack").json()
        _show(
            "通知回执轨迹",
            {
                "dispatched": dispatch["dispatched"],
                "ack_notification_id": promotion_notice["id"],
                "acknowledged_at": ack["acknowledged_at"],
            },
        )

        # 7. 路线保护关闭：周六 DIKE 整场转移到周日场
        transfer = client.post(
            "/api/birding/events/SAT-AM/routes/DIKE/closure",
            json={
                "route_code": "DIKE",
                "action": "transfer",
                "target_event_code": "SUN-AM",
                "reason": "濒危水鸟繁殖期围蔽",
            },
        ).json()
        _show("DIKE 关闭 -> 整场转移决定", {k: transfer[k] for k in ("action", "target_event_code", "affected_count", "results", "promotions")})

        # 8. 周一演练场：先占满 3 个名额，再让两人组进入候补；唯一路线关闭 -> 整场取消
        for index in range(3):
            resp = client.post(
                "/api/birding/registrations",
                json={
                    "event_code": "MON-PM",
                    "primary_name": f"周一报名者{index + 1}",
                    "contact": f"mon{index}@example.cn",
                    "party_size": 1,
                    "companions": [],
                    "idempotency_key": f"mon{index}",
                },
            )
            assert resp.status_code == 201, resp.text
        mon_w = client.post(
            "/api/birding/registrations",
            json={
                "event_code": "MON-PM",
                "primary_name": "周一候补",
                "contact": "monw@example.cn",
                "party_size": 2,
                "companions": [{"name": "同伴", "requires_accessible": False}],
                "idempotency_key": "monw",
            },
        ).json()
        assert mon_w["status"] == "waitlisted", mon_w
        closure = client.post(
            "/api/birding/events/MON-PM/routes/RIVER/closure",
            json={"route_code": "RIVER", "action": "cancel", "reason": "水位突涨，堤岸封闭"},
        )
        _show("唯一路线关闭 -> 整场取消", {"http_status": closure.status_code, "body": closure.json()})

        # 9. 完整轨迹
        timeline = client.get("/api/birding/events/MON-PM/timeline").json()
        _show(
            "周一演练场完整轨迹",
            {
                "event_status": timeline["event"]["status"],
                "registrations": [
                    {"id": r["id"], "name": r["primary_name"], "status": r["status"], "rank": r["waitlist_rank"]}
                    for r in timeline["registrations"]
                ],
                "notifications": [
                    {"to": n["primary_name"], "type": n["type"], "status": n["status"]}
                    for n in timeline["notifications"]
                ],
                "log_actions": [log["action"] for log in timeline["logs"]],
            },
        )

    close_connection()
    print("\n演示完成。")


if __name__ == "__main__":
    main()
