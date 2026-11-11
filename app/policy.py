"""Deterministic policy checks. These run first and cannot be talked out of:
the AI screener (screen.py) may only make the final decision stricter."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import Agent, same_address

PASS, HOLD, BLOCK = "pass", "hold", "block"
SEVERITY = {PASS: 0, HOLD: 1, BLOCK: 2}
DECISION = {PASS: "allow", HOLD: "hold", BLOCK: "block"}

# statuses that count against spending limits
COMMITTED = ("approved", "executing", "executed", "pending_approval")

ADDRESS_RE = {
    "near": re.compile(r"^(([a-z\d]+[-_])*[a-z\d]+\.)*([a-z\d]+[-_])*[a-z\d]+$|^[0-9a-f]{64}$"),
    "eth": re.compile(r"^0x[0-9a-fA-F]{40}$"),
    "base": re.compile(r"^0x[0-9a-fA-F]{40}$"),
    "arb": re.compile(r"^0x[0-9a-fA-F]{40}$"),
    "sol": re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$"),
    "tron": re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$"),
    "btc": re.compile(r"^(bc1[02-9ac-hj-np-z]{11,71}|[13][1-9A-HJ-NP-Za-km-z]{25,34})$"),
}


@dataclass
class Check:
    id: str
    label: str
    result: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "result": self.result, "detail": self.detail}


@dataclass
class PaymentRequest:
    amount_usd: float
    chain: str
    address: str
    token: str = "USDC"
    purpose: str = ""
    context: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def day_start(now: float | None = None) -> float:
    d = datetime.fromtimestamp(now or time.time(), tz=timezone.utc)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def evaluate(agent: Agent, req: PaymentRequest, spent_today: float, tx_last_hour: int) -> list[Check]:
    p = agent.policy
    out: list[Check] = []
    add = lambda *a: out.append(Check(*a))  # noqa: E731

    if req.amount_usd <= 0 or round(req.amount_usd, 2) != round(req.amount_usd, 6):
        add("amount", "Amount is valid", BLOCK, f"${req.amount_usd} is not a positive amount in cents")
    else:
        add("amount", "Amount is valid", PASS, f"${req.amount_usd:.2f}")

    rx = ADDRESS_RE.get(req.chain)
    if rx is None or req.chain not in p.allowed_chains:
        add("chain", "Chain is allowed", BLOCK, f"{req.chain!r} is not in {list(p.allowed_chains)}")
    else:
        add("chain", "Chain is allowed", PASS, req.chain)
    if req.token.upper() not in p.allowed_tokens:
        add("token", "Token is allowed", BLOCK, f"{req.token} is not in {list(p.allowed_tokens)}")
    if rx is not None and not rx.match(req.address):
        add("address", "Address is well-formed", BLOCK, f"not a valid {req.chain} address")

    if any(same_address(d, req.address) for d in agent.denylist):
        add("denylist", "Recipient is not denylisted", BLOCK, "address is on the denylist")

    if req.amount_usd > p.per_tx_limit_usd:
        add("per_tx", "Per-payment limit", BLOCK, f"${req.amount_usd:.2f} > ${p.per_tx_limit_usd:.2f} cap")
    else:
        add("per_tx", "Per-payment limit", PASS, f"${req.amount_usd:.2f} ≤ ${p.per_tx_limit_usd:.2f}")

    total = spent_today + req.amount_usd
    if total > p.daily_limit_usd:
        add("daily", "Daily budget", BLOCK, f"${spent_today:.2f} used today, this would make ${total:.2f} > ${p.daily_limit_usd:.2f}")
    else:
        add("daily", "Daily budget", PASS, f"${total:.2f} of ${p.daily_limit_usd:.2f} today")

    if tx_last_hour >= p.max_tx_per_hour:
        add("rate", "Payment rate", BLOCK, f"{tx_last_hour} payments in the last hour (max {p.max_tx_per_hour})")

    known = agent.payee_at(req.chain, req.address)
    if known:
        add("payee", "Known payee", PASS, f"{known.name}")
    else:
        add("payee", "Known payee", BLOCK if p.unknown_recipient == "block" else HOLD, "recipient is not on the allowlist")

    # A known vendor's name with someone else's address = invoice-redirection fraud.
    text = f"{req.purpose}\n{req.context}".lower()
    for payee in agent.allowlist:
        if any(re.search(rf"\b{re.escape(a)}\b", text) for a in payee.aliases) and not (
            payee.chain == req.chain and same_address(payee.address, req.address)
        ):
            add("impersonation", "Payee matches its known address", HOLD,
                f"mentions {payee.name}, but {payee.name} is paid at {payee.chain}:{payee.address}")
            break

    if req.amount_usd > p.approval_above_usd:
        add("approval", "Auto-approve threshold", HOLD, f"${req.amount_usd:.2f} > ${p.approval_above_usd:.2f} needs a human")
    else:
        add("approval", "Auto-approve threshold", PASS, f"≤ ${p.approval_above_usd:.2f}")
    return out


def worst(results: list[str]) -> str:
    return max(results, key=lambda r: SEVERITY[r], default=PASS)
