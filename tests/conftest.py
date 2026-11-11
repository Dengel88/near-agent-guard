import os

os.environ["DB_PATH"] = ":memory:"
os.environ["ONECLICK_MODE"] = "mock"
os.environ["LLM_PROVIDER"] = "mock"
os.environ["EXECUTION_MODE"] = "mock"
os.environ.pop("TURSO_DATABASE_URL", None)
os.environ.pop("OWNER_TOKEN", None)

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app
from app.oneclick import MockOneClick
from app.store import Store


@pytest.fixture
def settings():
    return load_settings(db_path=":memory:", oneclick_mode="mock", llm_provider="mock", execution_mode="mock",
                         turso_url="", owner_token="")


@pytest.fixture
def store():
    return Store(":memory:")


@pytest.fixture
def oneclick(store):
    oc = MockOneClick(kv=store)
    oc.SETTLE_SECONDS = 0
    return oc


@pytest.fixture
def client(settings, store, oneclick):
    return TestClient(create_app(settings=settings, store=store, oneclick=oneclick, llm=None))


AGENT = {"Authorization": "Bearer demo-agent-key"}
