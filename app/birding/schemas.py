from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RouteCreate(BaseModel):
    code: str = Field(..., min_length=2, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(..., min_length=1, max_length=80)
    # 路线保护承载量，按人头计，是报名确认时不可突破的硬约束
    capacity: int = Field(..., ge=1, le=10000)
    accessible: bool = False
    terrain_level: int = Field(default=1, ge=1, le=3)
    notes: str = Field(default="", max_length=300)


class RouteClose(BaseModel):
    reason: str = Field(..., min_length=1, max_length=300)
    closed_by: str = Field(default="coordinator", max_length=80)


class SessionCreate(BaseModel):
    code: str = Field(..., min_length=2, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(..., min_length=1, max_length=120)
    route_code: str = Field(..., min_length=2, max_length=40)
    start_at: str = Field(..., min_length=4, max_length=40)
    leader: str = Field(default="", max_length=80)
    # 现场名额，按人头计；实际可售名额取路线承载与现场名额的较小值
    capacity: int = Field(..., ge=1, le=10000)
    notes: str = Field(default="", max_length=300)


class RegistrationCreate(BaseModel):
    applicant_code: str = Field(..., min_length=2, max_length=40, description="报名者唯一编号，如手机号")
    applicant_name: str = Field(..., min_length=1, max_length=40)
    contact: str = Field(default="", max_length=80)
    needs_accessible: bool = False
    # 同行总人数（含报名者本人），亲子同行时大于 1
    party_size: int = Field(default=1, ge=1, le=50)
    party_note: str = Field(default="", max_length=200, description="同行关系说明，如：父母二人携带一名儿童")
    idempotency_key: str | None = Field(default=None, min_length=2, max_length=80)


class RegistrationCancel(BaseModel):
    reason: str = Field(default="", max_length=300)
    cancelled_by: str = Field(default="applicant", max_length=80)


class ClosureDecision(BaseModel):
    action: Literal["transfer", "cancel"]
    # action=transfer 时指定承接场次
    target_session_code: str | None = Field(default=None, max_length=40)
    reason: str = Field(default="", max_length=300)
    decided_by: str = Field(default="coordinator", max_length=80)


class Acknowledgement(BaseModel):
    acknowledger: str = Field(default="recipient", max_length=80)
