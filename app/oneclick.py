"""NEAR Intents 1Click API client.

Live: https://1click.chaindefuser.com (GET /v0/tokens, POST /v0/quote,
GET /v0/status, POST /v0/deposit/submit). Mock: same interface, offline, so
the whole app runs and is testable without network or keys.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from decimal import ROUND_UP, Decimal
from typing import Any

import httpx


class OneClickError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class OneClick(ABC):
    mode: str

    @abstractmethod
    async def tokens(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def quote(self, request: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    async def status(self, deposit_address: str, deposit_memo: str | None = None) -> dict[str, Any]: ...

    @abstractmethod
    async def submit_deposit(self, tx_hash: str, deposit_address: str, memo: str | None = None) -> dict[str, Any]: ...


class LiveOneClick(OneClick):
    mode = "live"

    def __init__(self, base_url: str, api_key: str = "", transport: httpx.AsyncBaseTransport | None = None):
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key
        self._kw = dict(base_url=base_url, headers=headers, timeout=30, transport=transport)
        self._tokens_cache: tuple[float, list[dict[str, Any]]] | None = None

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        try:
            async with httpx.AsyncClient(**self._kw) as client:  # per call: event loops may change
                resp = await client.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise OneClickError(f"1Click unreachable: {exc}") from exc
        if resp.status_code >= 400:
            try:
                msg = resp.json().get("message", resp.text)
            except ValueError:
                msg = resp.text
            raise OneClickError(f"1Click {resp.status_code}: {msg}", resp.status_code)
        return resp.json()

    async def tokens(self) -> list[dict[str, Any]]:
        if self._tokens_cache and time.time() - self._tokens_cache[0] < 300:
            return self._tokens_cache[1]
        data = await self._request("GET", "/v0/tokens")
        self._tokens_cache = (time.time(), data)
        return data

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/v0/quote", json=request)

    async def status(self, deposit_address: str, deposit_memo: str | None = None) -> dict[str, Any]:
        params = {"depositAddress": deposit_address}
        if deposit_memo:
            params["depositMemo"] = deposit_memo
        return await self._request("GET", "/v0/status", params=params)

    async def submit_deposit(self, tx_hash: str, deposit_address: str, memo: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"txHash": tx_hash, "depositAddress": deposit_address}
        if memo:
            body["memo"] = memo
        return await self._request("POST", "/v0/deposit/submit", json=body)


# --- mock ------------------------------------------------------------------

_NOW = "2026-11-01T00:00:00Z"
MOCK_TOKENS: list[dict[str, Any]] = [
    {"assetId": "nep141:17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1", "decimals": 6, "blockchain": "near", "symbol": "USDC", "price": 1.0},
    {"assetId": "nep141:wrap.near", "decimals": 24, "blockchain": "near", "symbol": "wNEAR", "price": 2.8},
    {"assetId": "nep141:eth.omft.near", "decimals": 18, "blockchain": "eth", "symbol": "ETH", "price": 3600.0},
    {"assetId": "nep141:eth-0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48.omft.near", "decimals": 6, "blockchain": "eth", "symbol": "USDC", "price": 1.0},
    {"assetId": "nep141:base-0x833589fcd6edb6e08f4c7c32d4f71b54bda02913.omft.near", "decimals": 6, "blockchain": "base", "symbol": "USDC", "price": 1.0},
    {"assetId": "nep141:arb-0xaf88d065e77c8cc2239327c5edb3a432268e5831.omft.near", "decimals": 6, "blockchain": "arb", "symbol": "USDC", "price": 1.0},
    {"assetId": "nep141:sol.omft.near", "decimals": 9, "blockchain": "sol", "symbol": "SOL", "price": 180.0},
    {"assetId": "nep141:sol-5ce3bf3a31af18be40ba30f721101b4341690186.omft.near", "decimals": 6, "blockchain": "sol", "symbol": "USDC", "price": 1.0},
    {"assetId": "nep141:tron-d28a265909efecdcee7c5028585214ea0b96f015.omft.near", "decimals": 6, "blockchain": "tron", "symbol": "USDT", "price": 1.0},
    {"assetId": "nep141:btc.omft.near", "decimals": 8, "blockchain": "btc", "symbol": "BTC", "price": 95000.0},
]
for _t in MOCK_TOKENS:
    _t["priceUpdatedAt"] = _NOW


class MockOneClick(OneClick):
    """Deterministic stand-in. A deposit is 'made' by calling simulate_deposit()."""

    mode = "mock"
    SETTLE_SECONDS = 4.0

    def __init__(self, kv: Any = None) -> None:
        # kv: optional object with kv_get/kv_set (the Store). With it the simulator
        # state lives in the database, so it survives serverless instance changes.
        self._kv = kv
        self._quotes: dict[str, dict[str, Any]] = {}
        self._deposits: dict[str, float] = {}

    def _get(self, kind: str, addr: str) -> Any:
        if self._kv is not None:
            return self._kv.kv_get(f"mock1click:{kind}:{addr}")
        return (self._quotes if kind == "quote" else self._deposits).get(addr)

    def _put(self, kind: str, addr: str, value: Any) -> None:
        if self._kv is not None:
            self._kv.kv_set(f"mock1click:{kind}:{addr}", value)
        else:
            (self._quotes if kind == "quote" else self._deposits)[addr] = value

    async def tokens(self) -> list[dict[str, Any]]:
        return [dict(t) for t in MOCK_TOKENS]

    def _token(self, asset_id: str) -> dict[str, Any]:
        for t in MOCK_TOKENS:
            if t["assetId"] == asset_id:
                return t
        raise OneClickError(f"1Click 400: unsupported asset {asset_id}", 400)

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        origin = self._token(request["originAsset"])
        dest = self._token(request["destinationAsset"])
        if request["swapType"] != "EXACT_OUTPUT":
            raise OneClickError("mock supports EXACT_OUTPUT only", 400)
        out_units = int(request["amount"])
        out_usd = Decimal(out_units) / (Decimal(10) ** dest["decimals"]) * Decimal(str(dest["price"]))
        fee = Decimal("1.0025")  # unauthenticated 1Click fee
        min_in = out_usd * fee / Decimal(str(origin["price"]))
        slip = Decimal(1) + Decimal(request["slippageTolerance"]) / Decimal(10000)
        amount_in = min_in * slip
        scale = Decimal(10) ** origin["decimals"]
        amount_in_units = int((amount_in * scale).to_integral_value(rounding=ROUND_UP))
        min_in_units = int((min_in * scale).to_integral_value(rounding=ROUND_UP))
        seed = hashlib.sha256(f"{request}{time.time()}".encode()).hexdigest()
        deposit = self._fake_address(origin["blockchain"], seed)
        quote = {
            "depositAddress": None if request.get("dry") else deposit,
            "amountIn": str(amount_in_units),
            "amountInFormatted": _fmt(Decimal(amount_in_units) / scale),
            "amountInUsd": _fmt(amount_in * Decimal(str(origin["price"])), 2),
            "minAmountIn": str(min_in_units),
            "amountOut": str(out_units),
            "amountOutFormatted": _fmt(Decimal(out_units) / (Decimal(10) ** dest["decimals"])),
            "amountOutUsd": _fmt(out_usd, 2),
            "minAmountOut": str(out_units),
            "deadline": request["deadline"],
            "timeEstimate": 20,
        }
        resp = {
            "correlationId": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "signature": "mock:" + seed[:32],
            "quoteRequest": request,
            "quote": quote,
        }
        if not request.get("dry"):
            self._put("quote", deposit, resp)
        return resp

    @staticmethod
    def _fake_address(chain: str, seed: str) -> str:
        if chain in {"eth", "base", "arb"}:
            return "0x" + seed[:40]
        if chain == "near":
            return seed[:64]
        if chain == "btc":
            return "bc1q" + seed[:38]
        if chain == "tron":
            return "T" + seed[:33]
        return seed[:44]

    def simulate_deposit(self, deposit_address: str) -> None:
        if self._get("quote", deposit_address) is None:
            raise OneClickError("Deposit address not found", 404)
        if self._get("deposit", deposit_address) is None:
            self._put("deposit", deposit_address, time.time())

    async def status(self, deposit_address: str, deposit_memo: str | None = None) -> dict[str, Any]:
        q = self._get("quote", deposit_address)
        if q is None:
            raise OneClickError("1Click 404: Deposit address not found", 404)
        seen = self._get("deposit", deposit_address)
        if seen is None:
            status = "PENDING_DEPOSIT"
        elif time.time() - seen < self.SETTLE_SECONDS:
            status = "PROCESSING"
        else:
            status = "SUCCESS"
        details: dict[str, Any] = {
            "intentHashes": [],
            "nearTxHashes": [],
            "originChainTxHashes": [],
            "destinationChainTxHashes": [],
        }
        if status != "PENDING_DEPOSIT":
            h = hashlib.sha256(deposit_address.encode()).hexdigest()
            details["originChainTxHashes"] = [{"hash": "0x" + h, "explorerUrl": ""}]
            details["amountIn"] = q["quote"]["amountIn"]
            details["amountInFormatted"] = q["quote"]["amountInFormatted"]
        if status == "SUCCESS":
            details["nearTxHashes"] = ["mock" + h[:40]]
            details["amountOut"] = q["quote"]["amountOut"]
            details["amountOutFormatted"] = q["quote"]["amountOutFormatted"]
        return {
            "correlationId": q["correlationId"],
            "quoteResponse": q,
            "status": status,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "swapDetails": details,
        }

    async def submit_deposit(self, tx_hash: str, deposit_address: str, memo: str | None = None) -> dict[str, Any]:
        self.simulate_deposit(deposit_address)
        return await self.status(deposit_address)


def _fmt(d: Decimal, places: int = 8) -> str:
    s = f"{d:.{places}f}".rstrip("0").rstrip(".")
    return s or "0"
