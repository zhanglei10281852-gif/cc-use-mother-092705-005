from __future__ import annotations

import uuid


def make_route(client, code=None, *, capacity=10, accessible=False, terrain_level=1):
    code = code or f"R-{uuid.uuid4().hex[:8]}"
    resp = client.post("/api/birding/routes", json={
        "code": code, "name": f"路线-{code}", "capacity": capacity,
        "accessible": accessible, "terrain_level": terrain_level,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


def make_session(client, code, route_code, *, capacity=10, start_at="2026-10-04T08:00:00+00:00", title=None):
    resp = client.post("/api/birding/sessions", json={
        "code": code, "title": title or f"场次-{code}", "route_code": route_code,
        "start_at": start_at, "leader": "志愿者小队", "capacity": capacity,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


def reg(client, session_code, applicant_code, **extra):
    payload = {"applicant_code": applicant_code, "applicant_name": f"访客{applicant_code}",
               "contact": f"{applicant_code}@example.com"}
    payload.update(extra)
    return client.post(f"/api/birding/sessions/{session_code}/registrations", json=payload)


# ---------- 路线能力与报名确认 ----------

def test_route_and_session_capacity_is_checked(client):
    # 路线承载 6，现场名额 10 → 有效名额取较小值 6
    make_route(client, "CAP-R", capacity=6)
    session = make_session(client, "CAP-S", "CAP-R", capacity=10)
    assert session["effective_capacity"] == 6

    r1 = reg(client, "CAP-S", "A1", party_size=4)
    assert r1.status_code == 201 and r1.json()["status"] == "confirmed"
    # 再来 3 人放不下（4+3>6），进入候补
    r2 = reg(client, "CAP-S", "A2", party_size=3)
    assert r2.status_code == 201
    body = r2.json()
    assert body["status"] == "waitlisted" and body["waitlist_seq"] == 1


def test_accessible_route_capability_enforced(client):
    make_route(client, "STAIR-R", capacity=10, accessible=False)
    make_session(client, "STAIR-S", "STAIR-R", capacity=10)
    resp = reg(client, "STAIR-S", "W1", needs_accessible=True)
    assert resp.status_code == 422
    assert "无障碍" in resp.json()["error"]["message"]

    make_route(client, "FLAT-R", capacity=10, accessible=True)
    make_session(client, "FLAT-S", "FLAT-R", capacity=10)
    ok = reg(client, "FLAT-S", "W2", needs_accessible=True, party_size=2)
    assert ok.status_code == 201 and ok.json()["needs_accessible"] is True


def test_duplicate_occupation_rejected(client):
    make_route(client, "DUP-R", capacity=10)
    make_session(client, "DUP-S", "DUP-R", capacity=10)
    assert reg(client, "DUP-S", "D1").status_code == 201
    again = reg(client, "DUP-S", "D1")
    assert again.status_code == 409
    assert "重复占位" in again.json()["error"]["message"]


def test_idempotent_replay_returns_same_registration(client):
    make_route(client, "IDEM-R", capacity=10)
    make_session(client, "IDEM-S", "IDEM-R", capacity=10)
    payload = {"applicant_code": "I1", "applicant_name": "幂等访客", "idempotency_key": "key-1001"}
    first = client.post("/api/birding/sessions/IDEM-S/registrations", json=payload)
    second = client.post("/api/birding/sessions/IDEM-S/registrations", json=payload)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert second.json()["replayed"] is True
    listing = client.get("/api/birding/sessions/IDEM-S/registrations").json()
    assert len(listing["registrations"]) == 1


# ---------- 候补晋升 ----------

def test_cancel_promotes_waitlist_in_priority_order_with_party_size(client):
    make_route(client, "WL-R", capacity=5)
    make_session(client, "WL-S", "WL-R", capacity=5)
    assert reg(client, "WL-S", "P1", party_size=3).json()["status"] == "confirmed"
    assert reg(client, "WL-S", "P2", party_size=2).json()["status"] == "confirmed"
    # 候补队首是 2 人组（缺 2 席），其后是 1 人组（缺 1 席）
    big = reg(client, "WL-S", "P3", party_size=2)
    small = reg(client, "WL-S", "P4", party_size=1)
    assert big.json()["waitlist_seq"] == 1
    assert small.json()["waitlist_seq"] == 2

    # P1(3人) 退出，释放 3 席：队首 2 人组先晋升，随后 1 人组也晋升
    cancel = client.post(f"/api/birding/registrations/{regs_id(client,'WL-S','P1')}/cancel",
                         json={"reason": "临时有事"})
    promoted = cancel.json()["promoted"]
    assert [p["applicant_code"] for p in promoted] == ["P3", "P4"]
    statuses = {item["applicant_code"]: item["status"]
                for item in client.get("/api/birding/sessions/WL-S/registrations").json()["registrations"]}
    assert statuses["P3"] == "confirmed" and statuses["P4"] == "confirmed"


def test_head_of_line_not_skipped_when_party_does_not_fit(client):
    make_route(client, "HOL-R", capacity=4)
    make_session(client, "HOL-S", "HOL-R", capacity=4)
    reg(client, "HOL-S", "H1", party_size=3)  # 占 3，剩 1
    reg(client, "HOL-S", "H2", party_size=1)  # 占满
    head = reg(client, "HOL-S", "H3", party_size=2)   # 候补#1，需 2 席
    tail = reg(client, "HOL-S", "H4", party_size=1)   # 候补#2，需 1 席
    assert head.json()["waitlist_seq"] == 1 and tail.json()["waitlist_seq"] == 2

    # H2(1人) 退出仅释放 1 席：队首 2 人组放不下，不得跳过它去晋升 H4
    cancel = client.post(f"/api/birding/registrations/{regs_id(client,'HOL-S','H2')}/cancel", json={})
    assert cancel.json()["promoted"] == []
    statuses = {item["applicant_code"]: item["status"]
                for item in client.get("/api/birding/sessions/HOL-S/registrations").json()["registrations"]}
    assert statuses["H3"] == "waitlisted" and statuses["H4"] == "waitlisted"


def regs_id(client, session_code, applicant_code):
    items = client.get(f"/api/birding/sessions/{session_code}/registrations").json()["registrations"]
    return next(item["id"] for item in items if item["applicant_code"] == applicant_code)


# ---------- 终态保护 ----------

def test_checked_in_is_terminal_and_not_overwritten(client):
    make_route(client, "TERM-R", capacity=3)
    make_session(client, "TERM-S", "TERM-R", capacity=3)
    reg(client, "TERM-S", "T1", party_size=2)
    reg(client, "TERM-S", "T2", party_size=1)
    w = reg(client, "TERM-S", "T3", party_size=1)
    assert w.json()["status"] == "waitlisted"

    t1_id = regs_id(client, "TERM-S", "T1")
    # T1 签到后成为终态
    checkin = client.post(f"/api/birding/registrations/{t1_id}/check-in")
    assert checkin.status_code == 200 and checkin.json()["status"] == "checked_in"
    # 重复签到幂等
    assert client.post(f"/api/birding/registrations/{t1_id}/check-in").json()["replayed"] is True
    # 已签到不可取消
    forbidden = client.post(f"/api/birding/registrations/{t1_id}/cancel", json={})
    assert forbidden.status_code == 409

    # 取消 T2 释放 1 席，T3 正常递补；T1 的 checked_in 终态不被触碰
    t2_id = regs_id(client, "TERM-S", "T2")
    promoted = client.post(f"/api/birding/registrations/{t2_id}/cancel", json={}).json()["promoted"]
    assert [p["applicant_code"] for p in promoted] == ["T3"]
    statuses = {item["applicant_code"]: item["status"]
                for item in client.get("/api/birding/sessions/TERM-S/registrations").json()["registrations"]}
    assert statuses["T1"] == "checked_in" and statuses["T3"] == "confirmed"


def test_cancel_replay_is_idempotent(client):
    make_route(client, "CX-R", capacity=2)
    make_session(client, "CX-S", "CX-R", capacity=2)
    reg(client, "CX-S", "C1")
    reg(client, "CX-S", "C2")
    cid = regs_id(client, "CX-S", "C2")
    first = client.post(f"/api/birding/registrations/{cid}/cancel", json={"reason": "第一次"})
    second = client.post(f"/api/birding/registrations/{cid}/cancel", json={"reason": "重复请求"})
    assert first.status_code == second.status_code == 200
    assert second.json()["status"] == "cancelled"
    assert second.json()["replayed"] is True
    # 只应产生一次取消通知（另有 confirmed、cancelled 各一）
    notes = client.get("/api/birding/notifications", params={"recipient_code": "C2"}).json()["notifications"]
    assert [n["ntype"] for n in notes].count("cancelled") == 1


# ---------- 路线保护关闭：整场取消 ----------

def test_route_closure_cancel_notifies_every_affected_person(client):
    make_route(client, "CLOSE-R", capacity=5)
    make_session(client, "CLOSE-S", "CLOSE-R", capacity=5)
    reg(client, "CLOSE-S", "K1", party_size=2)
    reg(client, "CLOSE-S", "K2")
    reg(client, "CLOSE-S", "K3")
    # K2 已签到（终态），仍属受影响者但记录不被改成取消
    k2_id = regs_id(client, "CLOSE-S", "K2")
    client.post(f"/api/birding/registrations/{k2_id}/check-in")
    # 主动取消者 K4 不应再收到关闭通知
    reg(client, "CLOSE-S", "K4")
    k4_id = regs_id(client, "CLOSE-S", "K4")
    client.post(f"/api/birding/registrations/{k4_id}/cancel", json={})

    closed = client.post("/api/birding/routes/CLOSE-R/close", json={"reason": "进入繁殖期封闭管理"})
    assert closed.status_code == 200 and closed.json()["frozen_sessions"] == 1
    # 关闭待定期间不能继续报名
    blocked = reg(client, "CLOSE-S", "K9")
    assert blocked.status_code == 409

    decision = client.post("/api/birding/sessions/CLOSE-S/closure-decision",
                           json={"action": "cancel", "reason": "保护期延长"})
    assert decision.status_code == 200
    assert decision.json()["session"]["status"] == "cancelled"

    statuses = {item["applicant_code"]: item["status"]
                for item in client.get("/api/birding/sessions/CLOSE-S/registrations").json()["registrations"]}
    assert statuses["K1"] == "cancelled_by_closure"
    assert statuses["K3"] == "cancelled_by_closure"
    assert statuses["K2"] == "checked_in"  # 终态保留
    assert statuses["K4"] == "cancelled"

    notes = client.get("/api/birding/notifications", params={"session_code": "CLOSE-S"}).json()["notifications"]
    cancel_notes = [n for n in notes if n["ntype"] == "session_cancelled"]
    assert {n["recipient_code"] for n in cancel_notes} == {"K1", "K2", "K3"}
    assert all(n["ack_status"] == "pending" for n in cancel_notes)


# ---------- 路线保护关闭：整场转移 ----------

def test_route_closure_transfer_moves_people_with_checks(client):
    make_route(client, "OLD-R", capacity=6, accessible=False)
    make_route(client, "NEW-R", capacity=20, accessible=True)
    make_session(client, "OLD-S", "OLD-R", capacity=6)
    make_session(client, "NEW-S", "NEW-R", capacity=20, start_at="2026-10-05T08:00:00+00:00")

    # 容量 6：M1=3、M2=1、M3=2 恰好占满，M4(2人) 进入候补
    reg(client, "OLD-S", "M1", party_size=3)
    reg(client, "OLD-S", "M2")
    assert reg(client, "OLD-S", "M3", party_size=2).json()["status"] == "confirmed"
    assert client.get("/api/birding/sessions/OLD-S").json()["occupied"] == 6
    assert reg(client, "OLD-S", "M4", party_size=2).json()["status"] == "waitlisted"
    m2_id = regs_id(client, "OLD-S", "M2")
    client.post(f"/api/birding/registrations/{m2_id}/check-in")  # 已签到终态

    client.post("/api/birding/routes/OLD-R/close", json={"reason": "栖息地修复"})
    decision = client.post("/api/birding/sessions/OLD-S/closure-decision", json={
        "action": "transfer", "target_session_code": "NEW-S", "reason": "改走替代湿地路线",
    })
    assert decision.status_code == 200, decision.text
    assert decision.json()["session"]["status"] == "transferred"

    target = client.get("/api/birding/sessions/NEW-S").json()
    # 占用转移：M1=3,M2=1,M3=2 共 6；承接场次容量 20 有余量，候补 M4 随即递补
    assert target["occupied"] == 8
    moved = {item["applicant_code"]: item for item in
             client.get("/api/birding/sessions/NEW-S/registrations").json()["registrations"]}
    assert moved["M1"]["status"] == "confirmed"
    assert moved["M2"]["status"] == "checked_in"  # 终态保留
    assert moved["M3"]["status"] == "confirmed"
    assert moved["M4"]["status"] == "confirmed"  # 富余名额下按候补序号递补

    # 转移通知逐人发出并挂在源场次轨迹上
    notes = client.get("/api/birding/notifications", params={"session_code": "OLD-S"}).json()["notifications"]
    transferred = [n for n in notes if n["ntype"] == "session_transferred"]
    assert {n["recipient_code"] for n in transferred} == {"M1", "M2", "M3", "M4"}


def test_transfer_blocked_by_capacity_accessibility_and_duplicate(client):
    make_route(client, "B-OLD", capacity=10, accessible=True)
    make_route(client, "B-SMALL", capacity=2, accessible=True)
    make_route(client, "B-FLAT-NO", capacity=20, accessible=False)
    make_session(client, "B-OLD-S", "B-OLD", capacity=10)
    make_session(client, "B-SMALL-S", "B-SMALL", capacity=2)
    make_session(client, "B-NO-S", "B-FLAT-NO", capacity=20)

    reg(client, "B-OLD-S", "B1", party_size=4)
    reg(client, "B-OLD-S", "B2", needs_accessible=True)
    client.post("/api/birding/routes/B-OLD/close", json={"reason": "封路"})

    # 承接场次名额不足
    small = client.post("/api/birding/sessions/B-OLD-S/closure-decision",
                        json={"action": "transfer", "target_session_code": "B-SMALL-S"})
    assert small.status_code == 409 and "名额不足" in small.json()["error"]["message"]

    # 承接路线无无障碍能力
    no_access = client.post("/api/birding/sessions/B-OLD-S/closure-decision",
                            json={"action": "transfer", "target_session_code": "B-NO-S"})
    assert no_access.status_code == 422 and "无障碍" in no_access.json()["error"]["message"]


def test_transfer_blocked_by_existing_occupation(client):
    make_route(client, "D-OLD", capacity=10, accessible=True)
    make_route(client, "D-NEW", capacity=10, accessible=True)
    make_session(client, "D-OLD-S", "D-OLD", capacity=10)
    make_session(client, "D-NEW-S", "D-NEW", capacity=10)
    reg(client, "D-OLD-S", "D1")
    # D1 在承接场次已另有占位
    reg(client, "D-NEW-S", "D1")
    client.post("/api/birding/routes/D-OLD/close", json={"reason": "水位上涨"})
    resp = client.post("/api/birding/sessions/D-OLD-S/closure-decision",
                       json={"action": "transfer", "target_session_code": "D-NEW-S"})
    assert resp.status_code == 409
    assert resp.json()["error"]["context"]["applicants"] == ["D1"]


# ---------- 通知回执与轨迹 ----------

def test_notification_acknowledgement_and_timeline(client):
    make_route(client, "TL-R", capacity=2)
    make_session(client, "TL-S", "TL-R", capacity=2)
    reg(client, "TL-S", "Z1")
    reg(client, "TL-S", "Z2")
    reg(client, "TL-S", "Z3")  # 候补
    z1_id = regs_id(client, "TL-S", "Z1")
    promoted = client.post(f"/api/birding/registrations/{z1_id}/cancel", json={}).json()["promoted"]
    assert [p["applicant_code"] for p in promoted] == ["Z3"]

    notes = client.get("/api/birding/notifications", params={"recipient_code": "Z3"}).json()["notifications"]
    types = [n["ntype"] for n in notes]
    assert "waitlisted" in types and "promoted" in types
    promoted_note = next(n for n in notes if n["ntype"] == "promoted")

    ack = client.post(f"/api/birding/notifications/{promoted_note['id']}/acknowledge",
                      json={"acknowledger": "Z3"})
    assert ack.status_code == 200 and ack.json()["ack_status"] == "acknowledged"
    # 重复回执幂等
    again = client.post(f"/api/birding/notifications/{promoted_note['id']}/acknowledge",
                        json={"acknowledger": "Z3"})
    assert again.json()["replayed"] is True

    timeline = client.get("/api/birding/sessions/TL-S/timeline").json()
    assert timeline["notification_summary"]["total"] >= 4
    assert timeline["notification_summary"]["acknowledged"] >= 1
    actions = [h["action"] for h in timeline["history"]]
    assert "registered_waitlisted" in actions
    assert "promoted" in actions
    assert "cancel" in actions
