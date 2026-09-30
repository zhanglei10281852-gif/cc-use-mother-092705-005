from __future__ import annotations

from fastapi import APIRouter, Query

from app.birding.schemas import (
    Acknowledgement,
    ClosureDecision,
    RegistrationCancel,
    RegistrationCreate,
    RouteClose,
    RouteCreate,
    SessionCreate,
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


@router.post("/routes/{code}/close")
def close_route(code: str, payload: RouteClose):
    return service().close_route(code, payload.reason, payload.closed_by)


@router.post("/routes/{code}/reopen")
def reopen_route(code: str):
    return service().reopen_route(code)


# ---------- 场次 ----------

@router.post("/sessions", status_code=201)
def create_session(payload: SessionCreate):
    return service().create_session(payload.model_dump())


@router.get("/sessions")
def list_sessions():
    return {"sessions": service().list_sessions()}


@router.get("/sessions/{code}")
def get_session(code: str):
    return service().get_session(code)


@router.get("/sessions/{code}/timeline")
def session_timeline(code: str):
    return service().timeline(code)


@router.post("/sessions/{code}/closure-decision")
def decide_closure(code: str, payload: ClosureDecision):
    return service().decide_closure(code, payload.model_dump())


# ---------- 报名 ----------

@router.post("/sessions/{code}/registrations", status_code=201)
def register(code: str, payload: RegistrationCreate):
    result = service().register(code, payload.model_dump())
    return result


@router.get("/sessions/{code}/registrations")
def list_registrations(code: str):
    return service().list_registrations(code)


@router.get("/registrations/{registration_id}")
def get_registration(registration_id: int):
    return service().get_registration(registration_id)


@router.post("/registrations/{registration_id}/cancel")
def cancel_registration(registration_id: int, payload: RegistrationCancel):
    return service().cancel_registration(registration_id, payload.reason, payload.cancelled_by)


@router.post("/registrations/{registration_id}/check-in")
def check_in(registration_id: int):
    return service().check_in(registration_id)


# ---------- 通知与回执 ----------

@router.get("/notifications")
def list_notifications(
    session_code: str | None = Query(default=None),
    ack_status: str | None = Query(default=None, pattern="^(pending|acknowledged)$"),
    recipient_code: str | None = Query(default=None),
):
    return {"notifications": service().list_notifications(session_code, ack_status, recipient_code)}


@router.post("/notifications/{notification_id}/acknowledge")
def acknowledge(notification_id: int, payload: Acknowledgement):
    return service().acknowledge(notification_id, payload.acknowledger)
