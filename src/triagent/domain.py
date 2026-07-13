from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    ROBOT_SAFETY = "robot-safety"


class TaskState(StrEnum):
    SPEC = "SPEC"
    IMPLEMENT = "IMPLEMENT"
    VERIFY = "VERIFY"
    REVIEW = "REVIEW"
    REPAIR = "REPAIR"
    APPROVAL = "APPROVAL"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    WAITING_FOR_VISUAL_APPROVAL = "WAITING_FOR_VISUAL_APPROVAL"
    WAITING_FOR_GUI = "WAITING_FOR_GUI"
    PAUSED_BUDGET = "PAUSED_BUDGET"
    FAILED_RECOVERABLE = "FAILED_RECOVERABLE"
    FAILED_FINAL = "FAILED_FINAL"


class OutcomeFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    severity: Literal["BLOCKER","MAJOR","MINOR","NOTE"]
    code: StrictStr = Field(min_length=1,max_length=100)
    message: StrictStr = Field(min_length=1,max_length=500)

class StageOutcome(BaseModel):
    """Narrow persisted result. Raw model output and reasoning are intentionally absent."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    stage: Literal["setup", "implement", "verify", "review"]
    status: Literal["passed", "failed", "unknown"]
    summary: Literal["completed","verified","clean","requires-repair","setup-failed","execution-failed","unknown"]
    diagnostic: StrictStr | None = Field(default=None,max_length=500)
    evidence: list[StrictStr] = Field(default_factory=list, max_length=50)
    artifacts: list[StrictStr] = Field(default_factory=list, max_length=50)
    findings: list[OutcomeFinding] = Field(default_factory=list,max_length=50)
    rollback: StrictStr = Field(default="unknown/missing", max_length=1000)



class ReviewSeverity(StrEnum):
    BLOCKER = "BLOCKER"
    MAJOR = "MAJOR"
    MINOR = "MINOR"
    NOTE = "NOTE"


class Budget(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_agent_calls: int = Field(default=20, ge=0)
    max_minutes: int = Field(default=60, ge=0)
    max_usd: float = Field(default=0.0, ge=0)


class TaskSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    goal: str = Field(min_length=1)
    scope: list[str] = Field(min_length=1)
    acceptance: list[str] = Field(min_length=1)
    risk: RiskLevel = RiskLevel.LOW
    visual_check: Literal["required", "optional", "none"] = "none"
    forbidden: list[str] = Field(default_factory=list)
    budget: Budget = Field(default_factory=Budget)

    @model_validator(mode="after")
    def enforce_robot_visual_check(self) -> TaskSpec:
        if self.risk is RiskLevel.ROBOT_SAFETY and self.visual_check != "required":
            object.__setattr__(self, "visual_check", "required")
        return self
