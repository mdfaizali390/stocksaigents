from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from src.constitution.schema import Constitution
from src.data.brokerage.base import Portfolio
from src.models import AgentReport, Intent, ProposedTrade


@dataclass
class AgentContext:
    """Per-query inputs handed to every agent.

    `market` is intentionally a free-form dict for the POC — each agent fetches
    the data it needs from src/data/* using the ticker. We tighten this once
    overlap between agents is clear.
    """

    intent: Intent
    constitution: Constitution
    portfolio: Portfolio | None = None
    proposed_trade: ProposedTrade | None = None
    market: dict[str, Any] = field(default_factory=dict)


class BaseAgent(ABC):
    name: str

    @abstractmethod
    async def run(self, context: AgentContext) -> AgentReport: ...
