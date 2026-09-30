from __future__ import annotations


def _route(client, code, **overrides):
    payload = {
        "code": code,
        "name": f"路线-{code}",
        "capacity": 30,
        "wheelchair_accessible": False,
        "difficulty": "easy",
    }
    payload.update(overrides)
    response = client.post("/api/birding/routes", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _event(client, code, route_codes, *, capacity=10, accessible=0, waitlist=5, **overrides):
    payload = {
        "code": code,
        "title": f"场次-{code}",
        "start_at": "2026-10-11T08:00:00+08:00",
        "route_codes": route_codes,
        "capacity_per_route": capacity,
        "accessible_capacity": accessible,
        "waitlist_capacity": waitlist,
    }
    payload.update(overrides)
    response = client.post("/api/birding/events", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _register(client, event_code, contact, key, **overrides):
    payload = {
        "event_code": event_code,
        "primary_name": f"用户-{contact}",
        "contact": contact,
        "party_size": 1,
        "companions": [],
        "idempotency_key": key,
    }
    payload.update(overrides)
    return client.post("/api/birding/registrations", json=payload)


def test_create_event_validates_route_capacity_and_schema(client):
    _route(client, "R1", capacity=5)
    # 场次名额不能超过路线承载
    response = client.post(
        "/api/birding/events",
        json={
            "code": "E-BAD",
            "title": "超限场次",
            "start_at": "2026-10-11T08:00:00+08:00",
            "route_codes": ["R1"],
            "capacity_per_route": 10,
            "accessible_capacity": 0,
            "waitlist_capacity": 2,
        },
    )
    assert response.status_code == 422
    assert "最大承载" in response.json()["error"]["message"]

    # 无障碍名额不能大于总名额（pydantic 校验）
    response = client.post(
        "/api/birding/events",
        json={
            "code": "E-BAD2",
            "title": "名额错误",
            "start_at": "2026-10-11T08:00:00+08:00",
            "route_codes": ["R1"],
            "capacity_per_route": 3,
            "accessible_capacity": 4,
            "waitlist_capacity": 2,
        },
    )
    assert response.status_code == 422

    event = _event(client, "E1", ["R1"], capacity=5)
    assert event["routes"][0]["code"] == "R1"
    assert event["waitlist_count"] == 0


def test_registration_confirms_and_waitlists_by_whole_party(client):
    _route(client, "R1")
    _route(client, "R2")
    _event(client, "E1", ["R1", "R2"], capacity=3, waitlist=2)

    # 一组 3 人整组占满 R1
    response = _register(
        client,
        "E1",
        "fam@x.cn",
        "k1",
        primary_name="家庭组",
        party_size=3,
        companions=[
            {"name": "孩子", "requires_accessible": False},
            {"name": "长辈", "requires_accessible": False},
        ],
        preferred_route_codes=["R1"],
    )
    assert response.status_code == 201, response.text
    first = response.json()
    assert first["status"] == "confirmed"
    assert first["route_code"] == "R1"
    assert first["notifications"][0]["type"] == "confirmation"
    assert len(first["companions"]) == 2

    # 单人：R1 只剩 0 个名额，自动分配到 R2
    response = _register(client, "E1", "solo@x.cn", "k2", preferred_route_codes=["R1"])
    assert response.status_code == 201
    assert response.json()["status"] == "confirmed"
    assert response.json()["route_code"] == "R2"

    # R2 也满（容量3，只占1，还能放2人）
    _register(client, "E1", "a@x.cn", "k3", preferred_route_codes=["R2"])
    _register(client, "E1", "b@x.cn", "k4", preferred_route_codes=["R2"])
    # 2 人组无法整组放入任一开放路线 -> 候补
    response = _register(
        client,
        "E1",
        "pair@x.cn",
        "k5",
        party_size=2,
        companions=[{"name": "同伴", "requires_accessible": False}],
    )
    assert response.status_code == 201
    waiting = response.json()
    assert waiting["status"] == "waitlisted"
    assert waiting["waitlist_rank"] == 1
    assert waiting["notifications"][0]["type"] == "waitlist"

    # 候补也满后拒绝
    _register(client, "E1", "c@x.cn", "k6", party_size=2, companions=[{"name": "x", "requires_accessible": False}])
    response = _register(client, "E1", "d@x.cn", "k7", party_size=2, companions=[{"name": "y", "requires_accessible": False}])
    assert response.status_code == 409
    assert "候补" in response.json()["error"]["message"]


def test_duplicate_request_is_idempotent_and_contact_cannot_double_hold(client):
    _route(client, "R1")
    _event(client, "E1", ["R1"], capacity=5, waitlist=2)

    body = {
        "event_code": "E1",
        "primary_name": "张三",
        "contact": "zhang@x.cn",
        "party_size": 1,
        "companions": [],
        "idempotency_key": "same-key",
    }
    first = client.post("/api/birding/registrations", json=body)
    assert first.status_code == 201
    # 同样的请求重复到达：返回同一记录，不产生新占位、新通知
    replay = client.post("/api/birding/registrations", json=body)
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["idempotent_replayed"] is True
    assert len(replay.json()["notifications"]) == 1

    # 换 key、同联系人仍在活跃状态 -> 拒绝重复占位
    response = _register(client, "E1", "zhang@x.cn", "different-key")
    assert response.status_code == 409

    # party_size 与同行人数不一致 -> 422
    response = client.post(
        "/api/birding/registrations",
        json={**body, "contact": "other@x.cn", "idempotency_key": "k-bad", "party_size": 2, "companions": []},
    )
    assert response.status_code == 422


def test_cancel_promotes_waitlist_in_strict_rank_order(client):
    _route(client, "R1")
    _event(client, "E1", ["R1"], capacity=2, waitlist=5)

    r1 = _register(client, "E1", "one@x.cn", "k1").json()
    _register(client, "E1", "two@x.cn", "k2")
    w1 = _register(client, "E1", "w1@x.cn", "k3").json()
    w2 = _register(client, "E1", "w2@x.cn", "k4").json()
    assert w1["waitlist_rank"] == 1 and w2["waitlist_rank"] == 2

    # 第一位确认者退出 -> 候补第 1 位晋升，第 2 位不动
    result = client.post(f"/api/birding/registrations/{r1['id']}/cancel", json={"reason": "临时有事"})
    assert result.status_code == 200
    promotions = result.json()["promotions"]
    assert [p["registration_id"] for p in promotions] == [w1["id"]]

    promoted = client.get(f"/api/birding/registrations/{w1['id']}").json()
    assert promoted["status"] == "confirmed"
    assert promoted["notifications"][-1]["type"] == "promotion"
    still_waiting = client.get(f"/api/birding/registrations/{w2['id']}").json()
    assert still_waiting["status"] == "waitlisted"

    # 已取消不能重复取消
    again = client.post(f"/api/birding/registrations/{r1['id']}/cancel", json={"reason": "x"})
    assert again.status_code == 409

    # 已晋升者退出：候补第 2 位晋升；晋升本身释放的名额不会被“跳过”逻辑影响
    result = client.post(f"/api/birding/registrations/{w1['id']}/cancel", json={"reason": "生病"})
    assert [p["registration_id"] for p in result.json()["promotions"]] == [w2["id"]]


def test_checked_in_record_is_immune_to_cancel_and_promotion_overwrite(client):
    _route(client, "R1")
    _event(client, "E1", ["R1"], capacity=1, waitlist=5)

    holder = _register(client, "E1", "hold@x.cn", "k1").json()
    waiter = _register(client, "E1", "wait@x.cn", "k2").json()
    assert waiter["status"] == "waitlisted"

    # 现场签到
    checked = client.post(f"/api/birding/registrations/{holder['id']}/check-in", json={})
    assert checked.status_code == 200
    assert checked.json()["status"] == "checked_in"

    # 已签到不能取消
    assert client.post(f"/api/birding/registrations/{holder['id']}/cancel", json={}).status_code == 409
    # 重复签到同样被拒
    assert client.post(f"/api/birding/registrations/{holder['id']}/check-in", json={}).status_code == 409
    # 候补者不能直接签到
    assert client.post(f"/api/birding/registrations/{waiter['id']}/check-in", json={}).status_code == 409
    # 候补者没有被错误晋升：名额仍被 checked_in 占用
    assert client.get(f"/api/birding/registrations/{waiter['id']}").json()["status"] == "waitlisted"


def test_accessible_route_capacity_governs_allocation(client):
    _route(client, "PLAIN", wheelchair_accessible=False)
    _route(client, "STEPFREE", wheelchair_accessible=True)
    _event(client, "E1", ["PLAIN", "STEPFREE"], capacity=4, accessible=1, waitlist=3)

    # 需要无障碍的报名只能落到 STEPFREE
    need = _register(
        client,
        "E1",
        "wheel@x.cn",
        "k1",
        requires_accessible=True,
        preferred_route_codes=["PLAIN"],  # 即使偏好普通路线也必须分配无障碍路线
    ).json()
    assert need["route_code"] == "STEPFREE"

    # 第二组有无障碍同伴：STEPFREE 无障碍配额仅 1 -> 进候补
    second = _register(
        client,
        "E1",
        "wheel2@x.cn",
        "k2",
        party_size=2,
        companions=[{"name": "轮椅使用者", "requires_accessible": True}],
    ).json()
    assert second["status"] == "waitlisted"

    # 普通名额仍充足：普通报名可进 STEPFREE
    normal = _register(client, "E1", "norm@x.cn", "k3", preferred_route_codes=["STEPFREE"]).json()
    assert normal["status"] == "confirmed"
    assert normal["route_code"] == "STEPFREE"


def test_route_closure_cancels_event_and_notifies_everyone(client):
    _route(client, "R1")
    _route(client, "R2")
    _event(client, "E1", ["R1", "R2"], capacity=2, waitlist=3)

    on_r1 = _register(client, "E1", "a@x.cn", "k1", preferred_route_codes=["R1"]).json()
    on_r2 = _register(client, "E1", "b@x.cn", "k2", preferred_route_codes=["R2"]).json()
    waiting = _register(client, "E1", "c@x.cn", "k3", party_size=2,
                        companions=[{"name": "家人", "requires_accessible": False}]).json()
    assert waiting["status"] == "waitlisted"

    # 先关闭 R2：受影响者收到取消通知，但场次仍有开放路线
    result = client.post(
        "/api/birding/events/E1/routes/R2/closure",
        json={"route_code": "R2", "action": "cancel", "reason": "水鸟繁殖期封路"},
    )
    assert result.status_code == 200
    body = result.json()
    assert body["affected_count"] == 1
    assert body["results"][0]["registration_id"] == on_r2["id"]
    event_after = client.get("/api/birding/events/E1").json()
    assert event_after["status"] == "open"

    # 再关闭 R1：整场取消，确认者与候补都收到通知
    result = client.post(
        "/api/birding/events/E1/routes/R1/closure",
        json={"route_code": "R1", "action": "cancel", "reason": "水位上涨"},
    )
    assert result.status_code == 200
    event_after = client.get("/api/birding/events/E1").json()
    assert event_after["status"] == "cancelled"

    cancelled_r1 = client.get(f"/api/birding/registrations/{on_r1['id']}").json()
    cancelled_wait = client.get(f"/api/birding/registrations/{waiting['id']}").json()
    assert cancelled_r1["status"] == "event_cancelled"
    assert cancelled_wait["status"] == "event_cancelled"
    assert cancelled_r1["notifications"][-1]["type"] == "route_cancelled"
    assert cancelled_wait["notifications"][-1]["type"] == "event_cancelled"

    # 投递 + 回执：每条通知都能留下确认状态
    dispatch = client.post("/api/birding/notifications/dispatch?event_code=E1")
    assert dispatch.json()["dispatched"] >= 4
    notifications = client.get("/api/birding/notifications?event_code=E1").json()["notifications"]
    target = next(n for n in notifications if n["registration_id"] == on_r1["id"] and n["type"] == "route_cancelled")
    ack = client.post(f"/api/birding/notifications/{target['id']}/ack")
    assert ack.status_code == 200
    assert ack.json()["acknowledged_at"] is not None
    # 重复回执幂等
    assert client.post(f"/api/birding/notifications/{target['id']}/ack").json()["acknowledged_at"] == ack.json()["acknowledged_at"]

    # 已取消/已关闭场次不接受新报名
    assert _register(client, "E1", "new@x.cn", "k9").status_code == 409
    # 轨迹完整可查
    timeline = client.get("/api/birding/events/E1/timeline").json()
    actions = [log["action"] for log in timeline["logs"]]
    assert "event.create" in actions
    assert "route.close_cancel" in actions
    assert "event.cancel" in actions
    assert "notification.ack" in actions


def test_route_closure_transfers_event_with_confirmations_and_waitlist(client):
    _route(client, "R1")
    _route(client, "R2")
    _event(client, "SAT", ["R1"], capacity=2, waitlist=3)
    _event(client, "SUN", ["R2"], capacity=5, waitlist=5)

    moved = _register(client, "SAT", "move@x.cn", "k1", preferred_route_codes=["R1"]).json()
    waiting = _register(client, "SAT", "w@x.cn", "k2", party_size=2,
                        companions=[{"name": "同伴", "requires_accessible": False}]).json()
    assert waiting["status"] == "waitlisted"

    result = client.post(
        "/api/birding/events/SAT/routes/R1/closure",
        json={
            "route_code": "R1",
            "action": "transfer",
            "target_event_code": "SUN",
            "reason": "栖息地围蔽保护",
        },
    )
    assert result.status_code == 200
    body = result.json()
    assert body["results"][0]["outcome"] == "transferred"
    new_id = body["results"][0]["new_registration_id"]

    new_reg = client.get(f"/api/birding/registrations/{new_id}").json()
    assert new_reg["status"] == "confirmed"
    assert new_reg["route_code"] == "R2"
    assert new_reg["source_registration_id"] == moved["id"]
    assert new_reg["notifications"][-1]["type"] == "confirmation"

    old = client.get(f"/api/birding/registrations/{moved['id']}").json()
    assert old["status"] == "transferred_out"
    assert old["notifications"][-1]["type"] == "event_transfer"

    # 候补整组转入目标场次候补，序号顺延
    sunday_regs = client.get("/api/birding/events/SUN/registrations").json()["registrations"]
    transferred_wait = [r for r in sunday_regs if r["source_registration_id"] == waiting["id"]]
    assert len(transferred_wait) == 1
    assert transferred_wait[0]["status"] == "waitlisted"

    sat = client.get("/api/birding/events/SAT").json()
    assert sat["status"] == "transferred"

    # 转移后 SUN 释放名额时，转入的候补可以正常晋升（不受源场次记录干扰）
    client.post(f"/api/birding/registrations/{new_id}/cancel", json={"reason": "时间冲突"})
    # SUN: 原本只有转入确认者(2人? 不，move 是1人) 占1；取消后候补 w 是2人组，R2容量5 -> 可晋升
    promoted_wait = client.get(f"/api/birding/registrations/{transferred_wait[0]['id']}").json()
    assert promoted_wait["status"] == "confirmed"
    assert promoted_wait["notifications"][-1]["type"] == "promotion"


def test_transfer_falls_back_to_cancel_when_target_has_no_room(client):
    _route(client, "R1")
    _route(client, "R2")
    _event(client, "SAT", ["R1"], capacity=5, waitlist=2)
    _event(client, "SUN", ["R2"], capacity=2, waitlist=2)

    # 目标场次先被占满（含无障碍约束时同样适用，这里用普通容量）
    _register(client, "SUN", "x1@x.cn", "s1", party_size=2, companions=[{"name": "p", "requires_accessible": False}])
    victim = _register(client, "SAT", "victim@x.cn", "k9").json()

    result = client.post(
        "/api/birding/events/SAT/routes/R1/closure",
        json={"route_code": "R1", "action": "transfer", "target_event_code": "SUN", "reason": "保护"},
    )
    assert result.status_code == 200
    assert result.json()["results"][0]["outcome"] == "transfer_unavailable_cancelled"
    record = client.get(f"/api/birding/registrations/{victim['id']}").json()
    assert record["status"] == "event_cancelled"
    assert record["notifications"][-1]["type"] == "event_cancelled"


def test_cancel_requires_matching_route_in_path(client):
    _route(client, "R1")
    _event(client, "E1", ["R1"], capacity=2)
    response = client.post(
        "/api/birding/events/E1/routes/R1/closure",
        json={"route_code": "OTHER", "action": "cancel"},
    )
    assert response.status_code == 422
