from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class RouteCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(..., min_length=1, max_length=80)
    capacity: int = Field(..., ge=1, le=500)
    wheelchair_accessible: bool = False
    difficulty: str = Field(default="easy", pattern=r"^(easy|moderate|hard)$")
    description: str = Field(default="", max_length=400)


class EventCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(..., min_length=1, max_length=120)
    start_at: str = Field(..., min_length=5, max_length=40)
    route_codes: list[str] = Field(..., min_length=1, max_length=20)
    capacity_per_route: int = Field(..., ge=1, le=500)
    accessible_capacity: int = Field(
        default=0,
        ge=0,
        le=500,
        description="无障碍名额（占用所选路线的无障碍配额，0 表示不开放）",
    )
    waitlist_capacity: int = Field(default=20, ge=0, le=1000)
    notes: str = Field(default="", max_length=400)

    @model_validator(mode="after")
    def _check_capacities(self) -> "EventCreate":
        if self.accessible_capacity > self.capacity_per_route:
            raise ValueError("accessible_capacity 不能大于 capacity_per_route")
        return self


class CompanionIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    requires_accessible: bool = False


class RegistrationCreate(BaseModel):
    event_code: str = Field(..., min_length=1, max_length=40)
    primary_name: str = Field(..., min_length=1, max_length=60)
    contact: str = Field(..., min_length=2, max_length=80)
    requires_accessible: bool = False
    party_size: int = Field(default=1, ge=1, le=20)
    companions: list[CompanionIn] = Field(default_factory=list, max_length=19)
    preferred_route_codes: list[str] = Field(default_factory=list, max_length=20)
    idempotency_key: str = Field(..., min_length=1, max_length=80)

    @model_validator(mode="after")
    def _check_party(self) -> "RegistrationCreate":
        # party_size 必须等于主报名人 + 同行人数，保证名额按整组占用
        if self.party_size != len(self.companions) + 1:
            raise ValueError("party_size 必须等于 1 + companions 数量")
        return self


class CancelRequest(BaseModel):
    reason: str = Field(default="", max_length=200)


class CheckInRequest(BaseModel):
    pass


class NotificationAck(BaseModel):
    pass


class RouteClosureRequest(BaseModel):
    route_code: str = Field(..., min_length=1, max_length=40)
    action: str = Field(..., pattern=r"^(transfer|cancel)$")
    target_event_code: str | None = Field(default=None, max_length=40)
    reason: str = Field(default="生态保护需要，路线临时关闭", max_length=300)

    @model_validator(mode="after")
    def _check_target(self) -> "RouteClosureRequest":
        if self.action == "transfer" and not self.target_event_code:
            raise ValueError("转移必须指定 target_event_code")
        return self


class MessageSend(BaseModel):
    channel: str = Field(default="sms", pattern=r"^(sms|email|voice)$")
