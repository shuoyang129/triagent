from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from dataclasses import dataclass
import math


class AgentRole(StrEnum):
    CONTROLLER = "controller"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    VERIFIER = "verifier"


class AgentStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    UNAVAILABLE = "unavailable"
    INVALID_OUTPUT = "invalid_output"

@dataclass(frozen=True)
class CostEstimate:
    estimated_usd: float | None
    zero_cost_enforced: bool = False
    def __post_init__(self):
        if self.estimated_usd is not None and (isinstance(self.estimated_usd,bool) or not isinstance(self.estimated_usd,(int,float)) or not math.isfinite(self.estimated_usd) or self.estimated_usd < 0): raise ValueError("cost estimate must be finite non-boolean and nonnegative")

    @classmethod
    def enforced_zero(cls) -> "CostEstimate": return cls(0.0, True)
    @classmethod
    def unknown(cls) -> "CostEstimate": return cls(None, False)


class AgentCapabilities(BaseModel):
    model_config = ConfigDict(frozen=True)

    available: bool
    installed: bool | None = None
    version: str | None = None
    authenticated: bool | None = False
    headless: bool = False
    ready: bool | None = None


class AgentRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: AgentRole
    agent_identity: str = "unspecified"
    handoff_file: Path | None = None
    task_file: Path
    workdir: Path
    output_schema: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    read_only: bool = False


class AgentResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: AgentStatus
    summary: str = ""
    data: dict[str, object] = Field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""
    actual_usd: float | None = Field(default=None, ge=0)


class AgentAdapter(ABC):
    identity: str = "unknown"
    allowed_roles: frozenset[AgentRole] = frozenset()

    def __setattr__(self, name, value):
        if name in {"identity", "allowed_roles"}:
            raise AttributeError(f"{name} is an immutable adapter capability")
        super().__setattr__(name, value)

    def estimate_cost(self, request: AgentRequest | None) -> CostEstimate:
        return CostEstimate.unknown()
    @abstractmethod
    def capabilities(self) -> AgentCapabilities:
        raise NotImplementedError

    @abstractmethod
    def run(self, request: AgentRequest) -> AgentResult:
        raise NotImplementedError
