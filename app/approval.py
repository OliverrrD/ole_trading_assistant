"""Approval gate helpers for trading actions."""

from dataclasses import dataclass
from typing import Literal


ApprovalDecision = Literal["approved", "rejected", "pending"]


@dataclass
class TradeRequest:
    action: str
    symbol: str
    quantity: int
    reason: str = ""
    amount_usd: float | None = None


class ApprovalGate:
    def __init__(self, approval_mode: str = "manual"):
        self.approval_mode = approval_mode

    def decide(self, request: TradeRequest, user_approved: bool | None = None) -> ApprovalDecision:
        if self.approval_mode == "auto":
            return "approved"
        if user_approved is None:
            return "pending"
        return "approved" if user_approved else "rejected"
