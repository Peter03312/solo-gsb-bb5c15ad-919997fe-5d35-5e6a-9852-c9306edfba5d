"""HTTP 请求/响应模型（字段 snake_case，原因码附中文说明）。"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


class PassageIn(BaseModel):
    id: int = Field(..., ge=1, description="通道编号（正整数，全图唯一）")
    src: int = Field(..., ge=0, description="起点节点编号")
    dst: int = Field(..., ge=0, description="终点节点编号")
    minutes: int = Field(..., gt=0, description="通行分钟数（正整数）")
    capacity: int = Field(..., gt=0, description="每分钟通行人数（正整数）")

    @model_validator(mode="after")
    def _no_self_loop(self):
        if self.src == self.dst:
            raise ValueError("通道起点与终点不能相同")
        return self


class ClosureIn(BaseModel):
    edge_id: int = Field(..., description="被封闭的通道编号")
    start_minute: int = Field(..., ge=0, description="封闭开始分钟（含）")
    end_minute: int = Field(..., ge=0, description="封闭结束分钟（不含）")

    @model_validator(mode="after")
    def _half_open(self):
        if self.end_minute <= self.start_minute:
            raise ValueError("封闭时段需为左闭右开：end_minute 必须大于 start_minute")
        return self


class GroupIn(BaseModel):
    id: int = Field(..., ge=1, description="人群组编号（正整数，唯一）")
    size: int = Field(..., gt=0, description="人数（正整数，整组不可拆分）")
    origin: int = Field(..., ge=0, description="起点节点编号")
    earliest_departure: int = Field(..., ge=0, description="最早出发分钟")
    priority: int = Field(..., ge=0, description="优先级，数值越大越优先")
    deadline: int = Field(..., ge=0, description="规划截止分钟（到达须不晚于此）")

    @model_validator(mode="after")
    def _deadline_ok(self):
        if self.deadline < self.earliest_departure:
            raise ValueError("规划截止分钟不能早于最早出发分钟")
        return self


class ScenarioIn(BaseModel):
    passages: list[PassageIn] = Field(default_factory=list)
    exits: list[int] = Field(default_factory=list, description="出口节点编号列表")
    closures: list[ClosureIn] = Field(default_factory=list)
    groups: list[GroupIn] = Field(default_factory=list)


class StepOut(BaseModel):
    edge_id: int
    from_node: int
    to_node: int
    enter_minute: int
    exit_minute: int


class GroupPlanOut(BaseModel):
    group_id: int
    status: str
    exit_id: Optional[int] = None
    departure_minute: Optional[int] = None
    arrival_minute: Optional[int] = None
    route: list[int] = Field(default_factory=list, description="通道编号序列")
    steps: list[StepOut] = Field(default_factory=list)
    reason: Optional[str] = None
    detail: Optional[str] = None


class PlanResponse(BaseModel):
    current_minute: int
    groups: list[GroupPlanOut]


class ConflictOut(BaseModel):
    group_id: int
    edge_id: int
    enter_minute: int
    exit_minute: int
    closure: list[int] = Field(description="与之相交的封闭时段 [start,end)")


# --------------------------------------------------------------------------
# 编排修订（变更审计）
# --------------------------------------------------------------------------

class RevisionSummary(BaseModel):
    revision: int = Field(..., description="全局递增修订号")
    operation: str = Field(
        ..., description="触发操作：scenario | groups | closures | tick")
    minute: int = Field(..., description="提交时的当前分钟")


class RevisionListResponse(BaseModel):
    revisions: list[RevisionSummary]


class FieldChange(BaseModel):
    before: Any = None
    after: Any = None


class GroupChangeOut(BaseModel):
    group_id: int
    change: str = Field(
        ..., description="added（新增）| removed（移除）| changed（字段变化）")
    before: Optional[GroupPlanOut] = Field(
        None, description="提交前该组编排（added 时为 null）")
    after: Optional[GroupPlanOut] = Field(
        None, description="提交后该组编排（removed 时为 null）")
    fields: dict[str, FieldChange] = Field(
        default_factory=dict,
        description="逐字段前后值（仅 changed 时非空）；比较字段：status、"
                    "exit_id、departure_minute、arrival_minute、route、"
                    "steps、reason、detail")


class RevisionDetailResponse(BaseModel):
    revision: int = Field(..., description="全局递增修订号")
    operation: str = Field(..., description="触发操作")
    minute: int = Field(..., description="提交时的当前分钟")
    before: PlanResponse = Field(..., description="提交前的完整编排快照")
    after: PlanResponse = Field(
        ..., description="提交后的完整编排快照（即当时的编排）")
    changes: list[GroupChangeOut] = Field(
        default_factory=list,
        description="逐组前后差异：新增、移除或状态/出发与到达分钟/路线/"
                    "失败原因等字段变化；未变化的组不在其中")
