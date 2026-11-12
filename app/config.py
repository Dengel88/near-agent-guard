"""Settings: secrets and modes from the environment, policy from config.yaml."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Payee:
    name: str
    chain: str
    address: str
    token: str = "USDC"
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class Policy:
    per_tx_limit_usd: float = 25
    daily_limit_usd: float = 50
    approval_above_usd: float = 5
    max_tx_per_hour: int = 10
    allowed_chains: tuple[str, ...] = ("near",)
    allowed_tokens: tuple[str, ...] = ("USDC",)
    unknown_recipient: str = "hold"
    ai_screening: bool = True
    ai_hold_score: int = 40
    ai_block_score: int = 80
    approval_ttl_minutes: int = 60


@dataclass(frozen=True)
class Agent:
    id: str
    name: str
    api_key: str
    policy: Policy
    allowlist: tuple[Payee, ...] = ()
    denylist: tuple[str, ...] = ()

    def payee_at(self, chain: str, address: str) -> Payee | None:
        return next((p for p in self.allowlist if p.chain == chain and same_address(p.address, address)), None)


def same_address(a: str, b: str) -> bool:
    a, b = a.strip(), b.strip()
    # EVM addresses are case-insensitive; everything else is compared exactly
    if a.startswith("0x") and b.startswith("0x"):
        return a.lower() == b.lower()
    return a == b


@dataclass(frozen=True)
class Settings:
    agents: tuple[Agent, ...]
    treasury_account: str = "wanderlust.near"
    treasury_private_key: str = ""
    usdc_contract: str = "17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1"

    execution_mode: str = "mock"          # mock | live (live = real NEAR transactions)
    oneclick_mode: str = "mock"
    oneclick_base_url: str = "https://1click.chaindefuser.com"
    oneclick_api_key: str = ""
    near_rpc_url: str = "https://rpc.mainnet.near.org"

    llm_provider: str = "mock"            # mock | gemini | openai
    llm_base_url: str = "https://cloud-api.near.ai/v1"
    llm_api_key: str = ""
    llm_model: str = "zai-org/GLM-5.1-FP8"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    public_url: str = ""

    db_path: str = "data/guard.db"
    turso_url: str = ""
    turso_token: str = ""
    owner_token: str = ""                 # protects /console approvals; empty = open demo
    game_rounds_per_hour: int = 40        # per IP, "Rob Bob" game (each round = 2 LLM calls)

    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def usdc_asset_id(self) -> str:
        return f"nep141:{self.usdc_contract}"

    def agent(self, agent_id: str) -> Agent | None:
        return next((a for a in self.agents if a.id == agent_id), None)

    def agent_by_key(self, key: str) -> Agent | None:
        return next((a for a in self.agents if key and a.api_key == key), None)


_VAR = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")


def _expand(value: str) -> str:
    """${NAME} or ${NAME:-default} in config.yaml values."""
    return _VAR.sub(lambda m: _env(m.group(1), m.group(2) or ""), value)


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip().strip("\"'") if value not in (None, "") else default


def _agent(raw: dict[str, Any]) -> Agent:
    pol = dict(raw.get("policy") or {})
    for k in ("allowed_chains", "allowed_tokens"):
        if k in pol:
            pol[k] = tuple(pol[k])
    env_key = "AGENT_KEY_" + re.sub(r"\W", "_", raw["id"]).upper()
    return Agent(
        id=raw["id"],
        name=raw.get("name", raw["id"]),
        api_key=_env(env_key, str(raw.get("api_key", ""))),
        policy=Policy(**pol),
        allowlist=tuple(
            Payee(
                name=p["name"], chain=p["chain"], address=_expand(str(p["address"])), token=p.get("token", "USDC"),
                aliases=tuple(a.lower() for a in p.get("aliases", [])) + (p["name"].lower(),),
            )
            for p in raw.get("allowlist") or []
        ),
        denylist=tuple(str(d) for d in raw.get("denylist") or []),
    )


def load_settings(config_path: Path | None = None, **overrides: Any) -> Settings:
    raw = yaml.safe_load((config_path or ROOT / "config.yaml").read_text("utf-8"))
    tre = raw.get("treasury") or {}
    values: dict[str, Any] = dict(
        agents=tuple(_agent(a) for a in raw["agents"]),
        treasury_account=_env("TREASURY_NEAR_ACCOUNT", tre.get("account", "wanderlust.near")),
        treasury_private_key=_env("TREASURY_PRIVATE_KEY", ""),
        usdc_contract=_env("USDC_CONTRACT", tre.get("token_contract", Settings.usdc_contract)),
        execution_mode=_env("EXECUTION_MODE", "mock"),
        oneclick_mode=_env("ONECLICK_MODE", "mock"),
        oneclick_base_url=_env("ONECLICK_BASE_URL", "https://1click.chaindefuser.com"),
        oneclick_api_key=_env("ONECLICK_API_KEY", ""),
        near_rpc_url=_env("NEAR_RPC_URL", "https://rpc.mainnet.near.org"),
        llm_provider=_env("LLM_PROVIDER", "mock"),
        llm_base_url=_env("LLM_BASE_URL", "https://cloud-api.near.ai/v1"),
        llm_api_key=_env("LLM_API_KEY", ""),
        llm_model=_env("LLM_MODEL", "zai-org/GLM-5.1-FP8"),
        gemini_api_key=_env("GEMINI_API_KEY", _env("GOOGLE_API_KEY", "")),
        gemini_model=_env("GEMINI_MODEL", "gemini-3.8-flash"),
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=_env("TELEGRAM_CHAT_ID", ""),
        public_url=_env("PUBLIC_URL", ""),
        db_path=_env("DB_PATH", "/tmp/guard.db" if os.getenv("VERCEL") else "data/guard.db"),
        turso_url=_env("TURSO_DATABASE_URL", ""),
        turso_token=_env("TURSO_AUTH_TOKEN", "").removeprefix("Bearer ").strip(),
        owner_token=_env("OWNER_TOKEN", ""),
        game_rounds_per_hour=int(_env("GAME_ROUNDS_PER_HOUR", "40")),
    )
    values.update(overrides)
    return Settings(**values)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
