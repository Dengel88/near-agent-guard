"""The Guard: one entry point for every payment an agent wants to make.

request -> deterministic policy -> AI screener -> decision (allow | hold | block)
allow   -> execute from the treasury (NEAR USDC directly, other chains via 1Click)
hold    -> wait for the owner (console or Telegram), expire after a TTL
block   -> never executes
Every step is appended to the hash-chained audit log.
"""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx

from .config import Agent, Settings
from .near import NearError, Treasury
from .oneclick import MockOneClick, OneClick, OneClickError
from .policy import BLOCK, COMMITTED, DECISION, HOLD, PASS, Check, PaymentRequest, day_start, evaluate, worst
from .screen import screen
from .store import Store

USDC_DECIMALS = 6
MAX_ROUTE_FEE = Decimal("0.05")   # refuse a 1Click route that costs more than 5% + $0.05 over the amount
MAX_ROUTE_FEE_FLAT = 50_000       # $0.05 in USDC units


class GuardError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _units(usd: float) -> int:
    return int((Decimal(str(usd)) * 10**USDC_DECIMALS).to_integral_value(rounding=ROUND_HALF_UP))


class Guard:
    def __init__(self, settings: Settings, store: Store, oneclick: OneClick, llm: Any = None,
                 treasury: Treasury | None = None, notifier: "Notifier | None" = None):
        self.s = settings
        self.store = store
        self.oneclick = oneclick
        self.llm = llm
        self.treasury = treasury
        self.notifier = notifier

    # --- intake ----------------------------------------------------------
    async def submit(self, agent: Agent, req: PaymentRequest, ledger: str = "") -> dict[str, Any]:
        """`ledger` scopes spending limits; the public demo gives each visitor
        its own sandbox ledger in simulated mode. Live mode uses the agent's."""
        ledger = ledger or agent.id
        idem = str(req.meta.get("idempotency_key") or "")
        if idem:
            prior = self.store.kv_get(f"idem:{ledger}:{idem}")
            if prior and (p := self.store.get_payment(prior)):
                return p

        now = time.time()
        spent_today, _ = self.store.spent_since(ledger, day_start(now), COMMITTED)
        _, attempts = self.store.spent_since(ledger, now - 3600, COMMITTED + ("blocked", "denied", "expired", "failed"))
        checks = evaluate(agent, req, spent_today, attempts)

        ai: dict[str, Any] | None = None
        if agent.policy.ai_screening:
            payee = agent.payee_at(req.chain, req.address)
            ai = await screen(self.llm, req, payee.name if payee else "")
            score = ai["risk_score"]
            res = BLOCK if score >= agent.policy.ai_block_score else HOLD if score >= agent.policy.ai_hold_score else PASS
            checks.append(Check("ai", "AI screener", res, f"risk {score}/100. {ai['summary']}"))

        verdict = worst([c.result for c in checks])
        payee = agent.payee_at(req.chain, req.address)
        p: dict[str, Any] = {
            "id": "pay_" + secrets.token_urlsafe(8).replace("-", "x").replace("_", "y"),
            "agent_id": agent.id,
            "ledger": ledger,
            "agent_name": agent.name,
            "amount_usd": round(req.amount_usd, 2),
            "recipient": {"chain": req.chain, "address": req.address, "token": req.token.upper(),
                          "payee": payee.name if payee else None},
            "purpose": req.purpose[:500],
            "context": req.context[:4000],
            "checks": [c.as_dict() for c in checks],
            "ai": ai,
            "decision": DECISION[verdict],
            "status": {PASS: "approved", HOLD: "pending_approval", BLOCK: "blocked"}[verdict],
            "reasons": [c.detail or c.label for c in checks if c.result == verdict and verdict != PASS],
            "created_at": now,
            "expires_at": now + agent.policy.approval_ttl_minutes * 60 if verdict == HOLD else None,
            "execution": {},
            "history": [],
        }
        self._event(p, "requested", {"amount_usd": p["amount_usd"], "recipient": p["recipient"], "purpose": p["purpose"]})
        self._event(p, "decided", {"decision": p["decision"], "checks": {c.id: c.result for c in checks},
                                   "risk": ai["risk_score"] if ai else None})
        self.store.save_payment(p)
        if idem:
            self.store.kv_set(f"idem:{ledger}:{idem}", p["id"])

        if verdict == PASS:
            await self.execute(p)
        elif verdict == HOLD and self.notifier:
            await self.notifier.held(p)
        return p

    # --- owner actions -----------------------------------------------------
    async def approve(self, payment_id: str, actor: str = "owner") -> dict[str, Any]:
        p = self._pending(payment_id)
        p["status"] = "approved"
        p["approved_by"] = actor
        self._event(p, "approved", {"by": actor})
        self.store.save_payment(p)
        await self.execute(p)
        return p

    async def deny(self, payment_id: str, actor: str = "owner", reason: str = "") -> dict[str, Any]:
        p = self._pending(payment_id)
        p["status"] = "denied"
        self._event(p, "denied", {"by": actor, "reason": reason[:200]})
        self.store.save_payment(p)
        return p

    def _pending(self, payment_id: str) -> dict[str, Any]:
        p = self.store.get_payment(payment_id)
        if not p:
            raise GuardError("payment not found", 404)
        self.expire(p)
        if p["status"] != "pending_approval":
            raise GuardError(f"payment is {p['status']}, not pending approval", 409)
        return p

    def expire(self, p: dict[str, Any]) -> bool:
        if p["status"] == "pending_approval" and p.get("expires_at") and time.time() > p["expires_at"]:
            p["status"] = "expired"
            self._event(p, "expired", {})
            self.store.save_payment(p)
            return True
        return False

    # --- execution -----------------------------------------------------------
    async def execute(self, p: dict[str, Any]) -> None:
        r = p["recipient"]
        units = _units(p["amount_usd"])
        p["status"] = "executing"
        try:
            if r["chain"] == "near" and r["token"] == "USDC":
                p["execution"] = {"route": "direct", "amount_units": str(units)}
                tx = await self._transfer(r["address"], units, memo=f"agent-guard {p['id']}")
                p["execution"].update(tx)
                p["status"] = "executed"
            else:
                quote = await self._quote(r, units, p["id"])
                q = quote["quote"]
                amount_in = int(q["amountIn"])
                if Decimal(amount_in) > Decimal(units) * (1 + MAX_ROUTE_FEE) + MAX_ROUTE_FEE_FLAT:
                    raise GuardError(f"route too expensive: {q['amountInFormatted']} USDC in for ${p['amount_usd']} out")
                p["execution"] = {
                    "route": "1click", "deposit_address": q["depositAddress"], "amount_in_units": q["amountIn"],
                    "amount_in": q["amountInFormatted"], "amount_out": q["amountOutFormatted"],
                    "time_estimate_s": q.get("timeEstimate"), "correlation_id": quote.get("correlationId"),
                }
                tx = await self._transfer(q["depositAddress"], amount_in, memo=f"agent-guard {p['id']}")
                p["execution"].update(tx)
                if isinstance(self.oneclick, MockOneClick):
                    await self.oneclick.submit_deposit(tx["tx_hash"], q["depositAddress"])
                elif tx.get("simulated"):
                    p["status"] = "executed"  # live quote, simulated treasury transfer
                elif tx.get("tx_hash"):
                    try:
                        await self.oneclick.submit_deposit(tx["tx_hash"], q["depositAddress"])
                    except OneClickError:
                        pass  # optional speed-up; 1Click also detects the deposit on its own
        except (GuardError, OneClickError, NearError, httpx.HTTPError) as exc:
            p["status"] = "failed"
            p["execution"]["error"] = str(exc)[:300]
            self._event(p, "failed", {"error": str(exc)[:300]})
            self.store.save_payment(p)
            return
        self._event(p, "executed" if p["status"] == "executed" else "submitted",
                    {k: v for k, v in p["execution"].items() if k in ("route", "tx_hash", "deposit_address", "amount_in", "simulated")})
        self.store.save_payment(p)

    async def _transfer(self, receiver: str, units: int, memo: str) -> dict[str, Any]:
        if self.s.execution_mode != "live":
            fake = secrets.token_hex(16)
            return {"tx_hash": "sim" + fake, "simulated": True, "from": self.s.treasury_account}
        if not self.treasury:
            raise GuardError("EXECUTION_MODE=live needs TREASURY_NEAR_ACCOUNT and TREASURY_PRIVATE_KEY")
        tx = await self.treasury.ft_transfer(self.s.usdc_contract, receiver, units, memo)
        return {**tx, "from": self.treasury.account_id}

    async def _quote(self, r: dict[str, Any], units: int, ref: str) -> dict[str, Any]:
        tokens = await self.oneclick.tokens()
        dest = next((t for t in tokens if t["blockchain"] == r["chain"] and t["symbol"].upper() == r["token"]), None)
        if not dest:
            raise GuardError(f"1Click has no {r['token']} on {r['chain']}")
        out_units = units if dest["decimals"] == USDC_DECIMALS else int(Decimal(units) * 10 ** (dest["decimals"] - USDC_DECIMALS))
        deadline = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
        return await self.oneclick.quote({
            "dry": False,
            "swapType": "EXACT_OUTPUT",
            "slippageTolerance": 100,
            "originAsset": self.s.usdc_asset_id,
            "depositType": "ORIGIN_CHAIN",
            "destinationAsset": dest["assetId"],
            "amount": str(out_units),
            "refundTo": self.treasury.account_id if self.treasury else self.s.treasury_account,
            "refundType": "ORIGIN_CHAIN",
            "recipient": r["address"],
            "recipientType": "DESTINATION_CHAIN",
            "deadline": deadline,
            "referral": "agent-guard",
        })

    async def refresh(self, p: dict[str, Any]) -> dict[str, Any]:
        if self.expire(p):
            return p
        ex = p.get("execution") or {}
        if p["status"] != "executing" or ex.get("route") != "1click":
            return p
        try:
            st = await self.oneclick.status(ex["deposit_address"])
        except OneClickError:
            return p
        ex["oneclick_status"] = st["status"]
        details = st.get("swapDetails") or {}
        if st["status"] == "SUCCESS":
            p["status"] = "executed"
            dest = details.get("destinationChainTxHashes") or []
            ex["destination_tx"] = dest[0].get("hash") if dest else None
            ex["near_tx"] = (details.get("nearTxHashes") or [None])[0]
            self._event(p, "executed", {"oneclick": "SUCCESS", "near_tx": ex["near_tx"]})
        elif st["status"] in ("REFUNDED", "FAILED"):
            p["status"] = "failed"
            ex["error"] = f"1Click {st['status']}"
            self._event(p, "failed", {"oneclick": st["status"]})
        self.store.save_payment(p)
        return p

    # --- audit ---------------------------------------------------------------
    def _event(self, p: dict[str, Any], event: str, data: dict[str, Any]) -> None:
        entry = self.store.audit(event, {"agent": p["agent_id"], **data}, payment_id=p["id"])
        p.setdefault("history", []).append({"event": event, "at": time.time(), "seq": entry["seq"], "hash": entry["hash"]})


class Notifier:
    """Telegram ping for held payments, with a link to approve in the console."""

    def __init__(self, token: str, chat_id: str, public_url: str = ""):
        self._token, self._chat, self._url = token, chat_id, public_url.rstrip("/")

    async def held(self, p: dict[str, Any]) -> None:
        r = p["recipient"]
        text = (
            f"⏸ Agent Guard held a payment\n{p['agent_name']} wants to send ${p['amount_usd']:.2f} {r['token']} "
            f"to {r['chain']}:{r['address']}\nWhy: {'; '.join(p['reasons'])[:500]}"
        )
        if self._url:
            text += f"\nDecide: {self._url}/console#{p['id']}"
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                await c.post(f"https://api.telegram.org/bot{self._token}/sendMessage",
                             json={"chat_id": self._chat, "text": text, "disable_web_page_preview": True})
        except httpx.HTTPError:
            pass  # notifications are best effort; the console always shows held payments
