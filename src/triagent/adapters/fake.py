from __future__ import annotations

from collections.abc import Iterable

from triagent.adapters.base import (
    AgentAdapter,
    AgentCapabilities,
    AgentRequest,
    AgentResult,
    AgentStatus,
)


class FakeAgent(AgentAdapter):
    def __init__(self, results: Iterable[AgentResult]) -> None:
        self._results = list(results)
        self.requests: list[AgentRequest] = []

    @classmethod
    def succeeding(cls, summary: str = "ok") -> "FakeAgent":
        return cls([AgentResult(status=AgentStatus.SUCCEEDED, summary=summary)])

    @classmethod
    def failing(cls, summary: str = "failed") -> "FakeAgent":
        return cls([AgentResult(status=AgentStatus.FAILED, summary=summary)])

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(available=True, version="fake-1", authenticated=True, headless=True)

    def run(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        if not self._results:
            return AgentResult(status=AgentStatus.UNAVAILABLE, summary="No scripted result remains")
        return self._results.pop(0)
