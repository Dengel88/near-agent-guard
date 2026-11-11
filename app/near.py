"""Minimal NEAR signer for the Guard treasury: borsh-serialize, ed25519-sign
and send a transaction over JSON-RPC. No SDK dependency (pynacl + base58).

The treasury is the only key holder. Agents never see it: they can only ask
the Guard, and the Guard signs only payments that passed policy.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
from dataclasses import dataclass
from typing import Any

import base58
import httpx
from nacl.signing import SigningKey

TGAS = 10**12
ONE_YOCTO = 1
# NEP-145 storage for one FT account; 1Click deposit addresses may be fresh
STORAGE_DEPOSIT_YOCTO = 1_250_000_000_000_000_000_000  # 0.00125 NEAR


class NearError(RuntimeError):
    pass


# --- borsh -----------------------------------------------------------------

def _u32(n: int) -> bytes:
    return struct.pack("<I", n)


def _u64(n: int) -> bytes:
    return struct.pack("<Q", n)


def _u128(n: int) -> bytes:
    return n.to_bytes(16, "little")


def _str(s: str) -> bytes:
    b = s.encode()
    return _u32(len(b)) + b


@dataclass(frozen=True)
class FunctionCall:
    method: str
    args: dict[str, Any]
    gas: int = 30 * TGAS
    deposit: int = 0

    def borsh(self) -> bytes:
        args = json.dumps(self.args, separators=(",", ":")).encode()
        return (
            b"\x02"  # Action::FunctionCall
            + _str(self.method)
            + _u32(len(args)) + args
            + _u64(self.gas)
            + _u128(self.deposit)
        )


def serialize_tx(
    signer_id: str, public_key: bytes, nonce: int, receiver_id: str, block_hash: bytes, actions: list[FunctionCall]
) -> bytes:
    out = _str(signer_id) + b"\x00" + public_key + _u64(nonce) + _str(receiver_id) + block_hash
    out += _u32(len(actions)) + b"".join(a.borsh() for a in actions)
    return out


def parse_private_key(secret: str) -> SigningKey:
    raw = base58.b58decode(secret.strip().removeprefix("ed25519:"))
    if len(raw) not in (32, 64):
        raise NearError("TREASURY_PRIVATE_KEY must be an ed25519 key (ed25519:...)")
    return SigningKey(raw[:32])


def sign_tx(key: SigningKey, tx: bytes) -> tuple[bytes, str]:
    """Return (borsh SignedTransaction, tx hash in base58)."""
    digest = hashlib.sha256(tx).digest()
    sig = key.sign(digest).signature
    return tx + b"\x00" + sig, base58.b58encode(digest).decode()


# --- RPC -------------------------------------------------------------------

class NearRpc:
    def __init__(self, url: str, transport: httpx.AsyncBaseTransport | None = None):
        self._transport = transport
        self._url = url

    async def call(self, method: str, params: Any) -> Any:
        try:
            async with httpx.AsyncClient(timeout=60, transport=self._transport) as client:
                r = await client.post(self._url, json={"jsonrpc": "2.0", "id": "guard", "method": method, "params": params})
        except httpx.HTTPError as exc:
            raise NearError(f"NEAR RPC unreachable: {exc}") from exc
        body = r.json()
        if "error" in body:
            err = body["error"]
            raise NearError(f"NEAR RPC: {err.get('cause', {}).get('name') or err.get('message')}: {json.dumps(err)[:300]}")
        return body["result"]


class Treasury:
    """A NEAR account that holds USDC and pays only what the Guard approved."""

    def __init__(self, account_id: str, private_key: str, rpc: NearRpc):
        self.account_id = account_id
        self._key = parse_private_key(private_key)
        self.public_key = bytes(self._key.verify_key)
        self._rpc = rpc

    @property
    def public_key_str(self) -> str:
        return "ed25519:" + base58.b58encode(self.public_key).decode()

    async def _send(self, receiver_id: str, actions: list[FunctionCall]) -> dict[str, Any]:
        ak = await self._rpc.call(
            "query",
            {"request_type": "view_access_key", "finality": "final",
             "account_id": self.account_id, "public_key": self.public_key_str},
        )
        tx = serialize_tx(
            self.account_id, self.public_key, ak["nonce"] + 1, receiver_id,
            base58.b58decode(ak["block_hash"]), actions,
        )
        signed, tx_hash = sign_tx(self._key, tx)
        res = await self._rpc.call(
            "send_tx", {"signed_tx_base64": base64.b64encode(signed).decode(), "wait_until": "FINAL"}
        )
        status = (res.get("status") or {})
        if "Failure" in status:
            raise NearError(f"transaction {tx_hash} failed: {json.dumps(status['Failure'])[:300]}")
        return {"tx_hash": tx_hash, "explorer": f"https://nearblocks.io/txns/{tx_hash}"}

    async def ft_transfer(self, token_contract: str, receiver_id: str, amount: int, memo: str = "") -> dict[str, Any]:
        """storage_deposit (idempotent) + ft_transfer in one transaction."""
        return await self._send(
            token_contract,
            [
                FunctionCall("storage_deposit", {"account_id": receiver_id, "registration_only": True},
                             gas=10 * TGAS, deposit=STORAGE_DEPOSIT_YOCTO),
                FunctionCall("ft_transfer", {"receiver_id": receiver_id, "amount": str(amount), "memo": memo[:120]},
                             gas=20 * TGAS, deposit=ONE_YOCTO),
            ],
        )

    async def ft_balance(self, token_contract: str) -> int:
        res = await self._rpc.call(
            "query",
            {"request_type": "call_function", "finality": "final", "account_id": token_contract,
             "method_name": "ft_balance_of",
             "args_base64": base64.b64encode(json.dumps({"account_id": self.account_id}).encode()).decode()},
        )
        return int(json.loads(bytes(res["result"]).decode()))
