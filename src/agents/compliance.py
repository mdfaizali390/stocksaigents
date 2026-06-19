"""Compliance Agent — pure rule engine.

No LLM in the decision path. Reads the Constitution and the proposed trade,
emits BLOCK on any hard-rule violation, otherwise INFO. The summary/reasoning
strings are templated, not LLM-generated, so the same input always yields the
same output.
"""

from __future__ import annotations

from decimal import Decimal

from src.agents.base import AgentContext, BaseAgent
from src.models import AgentReport, Evidence, ProposedTrade


class ComplianceAgent(BaseAgent):
    name = "compliance"

    async def run(self, context: AgentContext) -> AgentReport:
        if context.proposed_trade is None:
            return self._info("No concrete trade to evaluate.")

        trade = context.proposed_trade
        constitution = context.constitution
        violations: list[tuple[str, str, dict]] = []

        # 1. Asset class
        if trade.asset_class in constitution.blocked_asset_classes:
            violations.append((
                "asset_class_blocked",
                f"Asset class '{trade.asset_class}' is in the blocked list.",
                {"asset_class": trade.asset_class},
            ))
        elif trade.asset_class not in constitution.allowed_asset_classes:
            violations.append((
                "asset_class_not_allowed",
                f"Asset class '{trade.asset_class}' is not in the allowed list.",
                {
                    "asset_class": trade.asset_class,
                    "allowed": list(constitution.allowed_asset_classes),
                },
            ))

        # 2. Order type
        if trade.order_type in constitution.blocked_order_types:
            violations.append((
                "order_type_blocked",
                f"Order type '{trade.order_type}' is in the blocked list.",
                {"order_type": trade.order_type},
            ))
        elif trade.order_type not in constitution.allowed_order_types:
            violations.append((
                "order_type_not_allowed",
                f"Order type '{trade.order_type}' is not in the allowed list.",
                {
                    "order_type": trade.order_type,
                    "allowed": list(constitution.allowed_order_types),
                },
            ))

        # 3. Trade size — only checkable if we know the portfolio total.
        size_evidence: dict | None = None
        if context.portfolio is not None and context.portfolio.total_value > 0:
            notional = trade.estimated_notional
            pct = (notional / context.portfolio.total_value) * Decimal(100)
            limit = Decimal(str(constitution.position_limits.max_single_trade_pct))
            size_evidence = {
                "estimated_notional": str(notional),
                "portfolio_total": str(context.portfolio.total_value),
                "trade_pct": float(pct),
                "max_single_trade_pct": float(limit),
            }
            if pct > limit:
                violations.append((
                    "trade_size_exceeded",
                    (
                        f"Trade size {pct:.2f}% of portfolio exceeds the "
                        f"{limit:.2f}% max_single_trade_pct limit."
                    ),
                    size_evidence,
                ))

        # 4. Tradability — block if the brokerage says the symbol is halted
        # or restricted. We accept any of: tradable=False, halted=True,
        # state != "active". Caller passes raw dict so we're permissive.
        if trade.tradability is not None:
            t = trade.tradability
            tradable = t.get("tradable")
            halted = t.get("halted") or t.get("is_halted")
            state = t.get("state")
            if tradable is False or halted or (state and state != "active"):
                reasons = []
                if tradable is False:
                    reasons.append("tradable=false")
                if halted:
                    reasons.append("halted")
                if state and state != "active":
                    reasons.append(f"state={state}")
                violations.append((
                    "not_tradable",
                    f"Brokerage reports {trade.ticker} is not tradable ({', '.join(reasons)}).",
                    {"tradability": t},
                ))

        evidence = self._evidence(constitution, trade, size_evidence, violations)

        if violations:
            block_codes = [code for code, _, _ in violations]
            block_msgs = [msg for _, msg, _ in violations]
            return AgentReport(
                agent_name=self.name,
                signal="BLOCK",
                confidence=1.0,
                summary=f"Trade blocked: {len(violations)} compliance violation(s).",
                reasoning=" ".join(block_msgs),
                evidence=evidence,
                blocking=True,
                blocking_reason="; ".join(block_msgs),
                metadata={"violation_codes": block_codes},
            )

        return AgentReport(
            agent_name=self.name,
            signal="INFO",
            confidence=1.0,
            summary="Trade passes all compliance checks.",
            reasoning=(
                f"Asset class '{trade.asset_class}' allowed, "
                f"order type '{trade.order_type}' allowed, "
                f"trade size within limit, symbol tradable."
            ),
            evidence=evidence,
            blocking=False,
            metadata={
                "human_approval_required": constitution.approval.human_approval_required,
            },
        )

    @staticmethod
    def _info(summary: str) -> AgentReport:
        return AgentReport(
            agent_name="compliance",
            signal="INFO",
            confidence=1.0,
            summary=summary,
            reasoning=summary,
        )

    @staticmethod
    def _evidence(
        constitution,
        trade: ProposedTrade,
        size_evidence: dict | None,
        violations: list[tuple[str, str, dict]],
    ) -> list[Evidence]:
        items = [
            Evidence(
                source="constitution",
                description="Asset class policy",
                data={
                    "allowed": list(constitution.allowed_asset_classes),
                    "blocked": list(constitution.blocked_asset_classes),
                },
            ),
            Evidence(
                source="constitution",
                description="Order type policy",
                data={
                    "allowed": list(constitution.allowed_order_types),
                    "blocked": list(constitution.blocked_order_types),
                },
            ),
            Evidence(
                source="proposed_trade",
                description=f"{trade.side} {trade.quantity} {trade.ticker} as {trade.order_type}",
                data={
                    "ticker": trade.ticker,
                    "side": trade.side,
                    "order_type": trade.order_type,
                    "quantity": str(trade.quantity),
                    "asset_class": trade.asset_class,
                    "estimated_price": str(trade.estimated_price),
                },
            ),
        ]
        if size_evidence is not None:
            items.append(
                Evidence(
                    source="constitution",
                    description="Trade size vs. portfolio",
                    data=size_evidence,
                )
            )
        for code, msg, data in violations:
            items.append(
                Evidence(
                    source="compliance_rule",
                    description=f"[{code}] {msg}",
                    data=data,
                )
            )
        return items
