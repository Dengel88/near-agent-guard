"""HTTP API.

Agents:  POST /api/v1/payments            (Authorization: Bearer <agent key>)
         GET  /api/v1/payments/{id}
Owner:   /api/owner/*  approvals, ledger, policy, audit log (X-Owner-Token if OWNER_TOKEN is set)
Demo:    /api/demo/*   the naive AP agent reading an inbox, wired to the Guard
Pages:   /  live demo,  /console  owner console
"""

from __future__ import annotations

import secrets
import time
from dataclasses import asdict
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .agent import email_text, run_agent, scenarios_for
from .config import ROOT, Agent, Settings, get_settings
from .guard import Guard, GuardError, Notifier
from .llm import make_llm
from .near import NearRpc, Treasury
from .oneclick import LiveOneClick, MockOneClick, OneClick
from .policy import COMMITTED, PaymentRequest, day_start
from .store import Store

WEB = ROOT / "web"
SANDBOX_COOKIE = "guard_sandbox"


class PaymentIn(BaseModel):
    amount_usd: float = Field(gt=0, le=1_000_000)
    chain: str = Field(min_length=2, max_length=10)
    address: str = Field(min_length=2, max_length=128)
    token: str = Field(default="USDC", max_length=12)
    purpose: str = Field(default="", max_length=500)
    context: str = Field(default="", max_length=8000, description="Text that led to this payment (email, invoice, chat). Untrusted.")
    idempotency_key: str = Field(default="", max_length=64)


class Decision(BaseModel):
    reason: str = Field(default="", max_length=200)


class EmailIn(BaseModel):
    scenario_id: str = ""
    sender: str = Field(default="", max_length=200, alias="from")
    subject: str = Field(default="", max_length=200)
    body: str = Field(default="", max_length=4000)


class GuardIn(EmailIn):
    call: dict[str, Any] | None = None


def create_app(settings: Settings | None = None, store: Store | None = None, oneclick: OneClick | None = None,
               llm: Any = "auto", treasury: Treasury | None = None) -> FastAPI:
    s = settings or get_settings()
    store = store or Store(s.db_path, s.turso_url, s.turso_token)
    if oneclick is None:
        oneclick = (LiveOneClick(s.oneclick_base_url, s.oneclick_api_key) if s.oneclick_mode == "live"
                    else MockOneClick(kv=store))
    if llm == "auto":
        llm = make_llm(s)
    if treasury is None and s.execution_mode == "live" and s.treasury_private_key:
        treasury = Treasury(s.treasury_account, s.treasury_private_key, NearRpc(s.near_rpc_url))
    notifier = Notifier(s.telegram_bot_token, s.telegram_chat_id, s.public_url) if s.telegram_bot_token and s.telegram_chat_id else None
    guard = Guard(s, store, oneclick, llm, treasury, notifier)

    app = FastAPI(title="Agent Guard", version="0.1.0",
                  description="A payment firewall between AI agents and money, settling through NEAR Intents.")
    app.state.guard = guard

    @app.exception_handler(GuardError)
    async def _guard_error(_: Request, exc: GuardError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, exc.status_code)

    # --- auth ----------------------------------------------------------------
    def agent_auth(authorization: str = Header(default="")) -> Agent:
        agent = s.agent_by_key(authorization.removeprefix("Bearer ").strip())
        if not agent:
            raise HTTPException(401, "unknown agent key")
        return agent

    def owner_auth(x_owner_token: str = Header(default=""), token: str = Query(default="")) -> None:
        if s.owner_token and not secrets.compare_digest((x_owner_token or token).encode(), s.owner_token.encode()):
            raise HTTPException(401, "owner token required")

    def ledger_for(agent: Agent, request: Request, response: Response) -> tuple[str, str]:
        if s.execution_mode == "live":
            return agent.id, ""
        sid = request.cookies.get(SANDBOX_COOKIE, "")
        if not sid.isalnum() or len(sid) != 12:
            sid = secrets.token_hex(6)
            response.set_cookie(SANDBOX_COOKIE, sid, max_age=7 * 86400, samesite="lax", httponly=True)
        return f"{agent.id}~{sid}", sid

    # --- pages -----------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB / "index.html")

    @app.get("/console", include_in_schema=False)
    def console() -> FileResponse:
        return FileResponse(WEB / "console.html")

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True, "storage": store.backend, "oneclick": oneclick.mode, "execution": s.execution_mode,
            "llm": getattr(llm, "model", None) or "mock", "treasury": s.treasury_account,
            "treasury_key": bool(treasury), "owner_token_required": bool(s.owner_token),
        }

    # --- agent API ---------------------------------------------------------------
    @app.post("/api/v1/payments", status_code=201)
    async def create_payment(body: PaymentIn, agent: Agent = Depends(agent_auth)) -> dict[str, Any]:
        req = PaymentRequest(amount_usd=body.amount_usd, chain=body.chain.lower(), address=body.address.strip(),
                             token=body.token, purpose=body.purpose, context=body.context,
                             meta={"idempotency_key": body.idempotency_key})
        return public(await guard.submit(agent, req))

    @app.get("/api/v1/payments/{payment_id}")
    async def get_payment(payment_id: str, agent: Agent = Depends(agent_auth)) -> dict[str, Any]:
        p = store.get_payment(payment_id)
        if not p or p["agent_id"] != agent.id:
            raise HTTPException(404, "payment not found")
        return public(await guard.refresh(p))

    # --- owner ---------------------------------------------------------------------
    @app.get("/api/owner/summary", dependencies=[Depends(owner_auth)])
    def summary(ledger: str = "") -> dict[str, Any]:
        out = []
        for a in s.agents:
            key = ledger or a.id
            spent, n = store.spent_since(key, day_start(), COMMITTED, all_ledgers=not ledger)
            out.append({"id": a.id, "name": a.name, "ledger": key, "spent_today_usd": round(spent, 2), "payments_today": n,
                        "policy": asdict(a.policy), "allowlist": [asdict(p) for p in a.allowlist], "denylist": list(a.denylist)})
        return {"agents": out, "audit": store.verify_audit(), "health": health()}

    @app.get("/api/owner/payments", dependencies=[Depends(owner_auth)])
    async def list_payments(status: str = "", ledger: str = "", limit: int = Query(default=50, le=200)) -> list[dict[str, Any]]:
        items = store.list_payments(agent_id=ledger or None, status=status or None, limit=limit)
        for p in items:
            if p["status"] in ("executing", "pending_approval"):
                await guard.refresh(p)
        return [public(p) for p in items]

    @app.get("/api/owner/payments/{payment_id}", dependencies=[Depends(owner_auth)])
    async def owner_payment(payment_id: str) -> dict[str, Any]:
        p = store.get_payment(payment_id)
        if not p:
            raise HTTPException(404, "payment not found")
        return public(await guard.refresh(p), audit=store.audit_log(payment_id))

    @app.post("/api/owner/payments/{payment_id}/approve", dependencies=[Depends(owner_auth)])
    async def approve(payment_id: str) -> dict[str, Any]:
        return public(await guard.approve(payment_id, "owner"))

    @app.post("/api/owner/payments/{payment_id}/deny", dependencies=[Depends(owner_auth)])
    async def deny(payment_id: str, body: Decision | None = None) -> dict[str, Any]:
        return public(await guard.deny(payment_id, "owner", body.reason if body else ""))

    @app.get("/api/owner/audit", dependencies=[Depends(owner_auth)])
    def audit(limit: int = Query(default=100, le=500)) -> dict[str, Any]:
        return {"verify": store.verify_audit(), "entries": store.audit_log(limit=limit)}

    # --- demo ------------------------------------------------------------------------
    @app.get("/api/demo/scenarios")
    def scenarios() -> list[dict[str, str]]:
        return scenarios_for(s.agents[0].allowlist)

    def demo_email(body: EmailIn) -> dict[str, str]:
        email = next((dict(e) for e in scenarios_for(s.agents[0].allowlist) if e["id"] == body.scenario_id), None) or {
            "from": body.sender or "unknown@example.com", "subject": body.subject, "body": body.body}
        if not (email.get("body") or "").strip():
            raise HTTPException(400, "empty email")
        return email

    async def demo_submit(email: dict[str, str], call: dict[str, Any] | None, request: Request,
                          response: Response) -> tuple[dict[str, Any] | None, str]:
        agent = s.agents[0]
        ledger, _ = ledger_for(agent, request, response)
        if not call:
            return None, ledger
        try:
            req = PaymentRequest(
                amount_usd=float(call["amount_usd"]), chain=str(call["chain"]).lower(), address=str(call["address"]).strip(),
                token=str(call.get("token") or "USDC"), purpose=str(call.get("purpose") or ""), context=email_text(email),
            )
        except (KeyError, TypeError, ValueError):
            raise HTTPException(422, "the agent produced an unusable payment call")
        return public(await guard.submit(agent, req, ledger=ledger)), ledger

    @app.post("/api/demo/agent")
    async def demo_agent(body: EmailIn) -> dict[str, Any]:
        """Step 1: the naive agent reads the email and decides what to pay."""
        return await run_agent(llm, demo_email(body))

    @app.post("/api/demo/guard")
    async def demo_guard(body: GuardIn, request: Request, response: Response) -> dict[str, Any]:
        """Step 2: the agent's tool call goes to the Guard, with the email as context."""
        payment, ledger = await demo_submit(demo_email(body), body.call, request, response)
        return {"payment": payment, "ledger": ledger}

    @app.post("/api/demo/run")
    async def demo_run(body: EmailIn, request: Request, response: Response) -> dict[str, Any]:
        """Both steps in one call (handy for curl and tests)."""
        t0 = time.time()
        email = demo_email(body)
        said = await run_agent(llm, email)
        payment, ledger = await demo_submit(email, said["call"], request, response)
        return {"agent": said, "payment": payment, "ledger": ledger, "ms": int((time.time() - t0) * 1000)}

    @app.get("/api/demo/payments/{payment_id}")
    async def demo_payment(payment_id: str) -> dict[str, Any]:
        """Read-only status for the demo page (no secrets in payments)."""
        p = store.get_payment(payment_id)
        if not p:
            raise HTTPException(404, "payment not found")
        return public(await guard.refresh(p))

    return app


def public(p: dict[str, Any], **extra: Any) -> dict[str, Any]:
    out = {k: v for k, v in p.items() if k != "context"}
    out["context_preview"] = (p.get("context") or "")[:600]
    out.update(extra)
    return out


app = create_app()
