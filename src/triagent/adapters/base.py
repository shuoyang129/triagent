from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


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
    task_file: Path
    workdir: Path
    output_schema: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)


class AgentResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: AgentStatus
    summary: str = ""
    data: dict[str, object] = Field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""


class AgentAdapter(ABC):
    @abstractmethod
    def capabilities(self) -> AgentCapabilities:
        raise NotImplementedError

    @abstractmethod
    def run(self, request: AgentRequest) -> AgentResult:
        raise NotImplementedError
