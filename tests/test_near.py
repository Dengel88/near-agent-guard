"""The signer must produce byte-identical transactions to the reference
implementation (py-near-primitives, Rust). Vector generated with it."""

import hashlib

import base58
from nacl.signing import SigningKey

from app.near import STORAGE_DEPOSIT_YOCTO, TGAS, FunctionCall, serialize_tx, sign_tx

REFERENCE_HEX = (
    "0a00000067756172642e6e6561720003a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b80700000000000000"
    "09000000757364632e6e656172000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f02000000020f00000073"
    "746f726167655f6465706f7369742d0000007b226163636f756e745f6964223a22616263222c22726567697374726174696f6e5f6f6e6c"
    "79223a747275657d00a0724e180900000000485637193cc34300000000000000020b00000066745f7472616e736665722f0000007b2272"
    "656365697665725f6964223a22616263222c22616d6f756e74223a22313030222c226d656d6f223a2278227d0040e59c301200000100"
    "000000000000000000000000000000ff1392c36affd22f039db43243bdb4a5127a2f305c94066c69e45de968f98806af4a05fd4f8bc6"
    "4283be6cece1bfc8b06a77c9947beb11b7294552dc91456f0a"
)


def test_signed_tx_matches_reference():
    key = SigningKey(bytes(range(32)))
    actions = [
        FunctionCall("storage_deposit", {"account_id": "abc", "registration_only": True}, 10 * TGAS, STORAGE_DEPOSIT_YOCTO),
        FunctionCall("ft_transfer", {"receiver_id": "abc", "amount": "100", "memo": "x"}, 20 * TGAS, 1),
    ]
    tx = serialize_tx("guard.near", bytes(key.verify_key), 7, "usdc.near", bytes(range(32)), actions)
    signed, tx_hash = sign_tx(key, tx)
    assert signed.hex() == REFERENCE_HEX
    assert tx_hash == base58.b58encode(hashlib.sha256(tx).digest()).decode()


def test_treasury_sends_signed_ft_transfer():
    import asyncio
    import base64
    import json

    import httpx

    from app.near import NearRpc, Treasury

    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "query":
            return httpx.Response(200, json={"result": {"nonce": 41, "block_hash": base58.b58encode(bytes(32)).decode()}})
        sent["tx"] = base64.b64decode(body["params"]["signed_tx_base64"])
        return httpx.Response(200, json={"result": {"status": {"SuccessValue": ""}}})

    key = SigningKey(bytes(range(32)))
    secret = "ed25519:" + base58.b58encode(bytes(key) + bytes(key.verify_key)).decode()
    t = Treasury("guard.near", secret, NearRpc("https://rpc.example", transport=httpx.MockTransport(handler)))
    out = asyncio.run(t.ft_transfer("usdc.near", "a" * 64, 1_000_000, "memo"))
    tx = sent["tx"]
    assert tx[-64:] and b"ft_transfer" in tx and b"storage_deposit" in tx and ("a" * 64).encode() in tx
    unsigned = tx[:-65]
    assert out["tx_hash"] == base58.b58encode(hashlib.sha256(unsigned).digest()).decode()
    key.verify_key.verify(hashlib.sha256(unsigned).digest(), tx[-64:])
    assert int.from_bytes(unsigned[4 + 10 + 1 + 32: 4 + 10 + 1 + 32 + 8], "little") == 42  # nonce + 1
