from __future__ import annotations

from fastapi import APIRouter, Query

from app.birding.schemas import (
    CancelRequest,
    EventCreate,
    RegistrationCreate,
    RouteClosureRequest,
    RouteCreate,
)
from app.birding.service import BirdingService

router = APIRouter(prefix="/api/birding", tags=["观鸟导赏报名"])


def service() -> BirdingService:
    return BirdingService()


# ---------- 路线 ----------

@router.post("/routes", status_code=201)
def create_route(payload: RouteCreate):
    return service().create_route(payload.model_dump())


@router.get("/routes")
def list_routes():
    return {"routes": service().list_routes()}


# ---------- 场次 ----------

@router.post("/events", status_code=201)
def create_event(payload: EventCreate):
    return service().create_event(payload.model_dump())


@router.get("/events")
def list_events():
    return {"events": service().list_events()}


@router.get("/events/{event_code}")
def get_event(event_code: str):
    return service().get_event(event_code)


@router.get("/events/{event_code}/registrations")
def list_event_registrations(event_code: str):
    return {"registrations": service().list_event_registrations(event_code)}


@router.get("/events/{event_code}/timeline")
def event_timeline(event_code: str):
    return service().event_timeline(event_code)


# ---------- 报名 ----------

@router.post("/registrations", status_code=201)
def register(payload: RegistrationCreate):
    return service().register(payload.model_dump())


@router.get("/registrations/{registration_id}")
def get_registration(registration_id: int):
    return service().get_registration(registration_id)


@router.post("/registrations/{registration_id}/cancel")
def cancel_registration(registration_id: int, payload: CancelRequest):
    return service().cancel_registration(registration_id, payload.reason)


@router.post("/registrations/{registration_id}/check-in", status_code=200)
def check_in(registration_id: int):
    return service().check_in(registration_id)


# ---------- 路线保护关闭 ----------

@router.post("/events/{event_code}/routes/{route_code}/closure")
def close_event_route(event_code: str, route_code: str, payload: RouteClosureRequest):
    # 路径与请求体中的路线必须一致，避免误操作其他路线
    if payload.route_code != route_code:
        from app.core.errors import ValidationError

        raise ValidationError(
            "路径路线与请求体路线不一致",
            context={"path_route_code": route_code, "body_route_code": payload.route_code},
        )
    return service().close_event_route(
        event_code=event_code,
        route_code=route_code,
        action=payload.action,
        reason=payload.reason,
        target_event_code=payload.target_event_code,
    )


# ---------- 通知 ----------

@router.post("/notifications/dispatch")
def dispatch_pending(event_code: str | None = Query(default=None)):
    return service().dispatch_pending(event_code)


@router.post("/notifications/{notification_id}/ack")
def acknowledge_notification(notification_id: int):
    return service().acknowledge_notification(notification_id)


@router.get("/notifications")
def list_notifications(event_code: str | None = Query(default=None)):
    return {"notifications": service().list_notifications(event_code)}
