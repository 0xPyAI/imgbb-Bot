"""Shared test setup.

Modules read settings at import time (dbmodels builds its engine then), so
dummy values go into the environment before any test module imports them.
Environment variables beat config.env, so tests never touch a real database.
tests/test_db.py runs only when TEST_DATABASE_URL names an empty PostgreSQL
database that the tests may fill and clear.
"""
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet

os.environ["API_ID"] = "12345"
os.environ["API_HASH"] = "0123456789abcdef0123456789abcdef"
os.environ["BOT_TOKEN"] = "123456:TEST-TOKEN"
os.environ["DATABASE_URL"] = (
    os.environ.get("TEST_DATABASE_URL") or "postgresql+asyncpg://test:test@127.0.0.1:1/test"
)
os.environ["FERNET_KEY"] = Fernet.generate_key().decode()

import commands  # noqa: E402

VALID_KEY = "1234567890abcdef1234567890abcdef"


@pytest.fixture
def user_db(monkeypatch):
    """Stand in for the settings and history tables."""
    db = SimpleNamespace(expiration=None, history=[])

    async def get_user_expiration(user_id):
        return db.expiration

    async def set_user_expiration(user_id, seconds):
        db.expiration = seconds

    async def add_upload_history(user_id, records):
        db.history.extend(records)

    async def get_upload_history(user_id, limit=10):
        return list(reversed(db.history))[:limit]

    async def clear_upload_history(user_id):
        removed = len(db.history)
        db.history = []
        return removed

    monkeypatch.setattr(commands, "get_user_expiration", get_user_expiration)
    monkeypatch.setattr(commands, "set_user_expiration", set_user_expiration)
    monkeypatch.setattr(commands, "add_upload_history", add_upload_history)
    monkeypatch.setattr(commands, "get_upload_history", get_upload_history)
    monkeypatch.setattr(commands, "clear_upload_history", clear_upload_history)
    return db


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setattr(commands, "get_user_api_key", AsyncMock(return_value=VALID_KEY))


@pytest.fixture
def fake_db(monkeypatch):
    """Stand in for the pending state and API key tables."""
    db = SimpleNamespace(pending=None, saved={})

    async def get_pending_state(user_id):
        return db.pending

    async def save_user_api_key(user_id, key):
        db.saved[user_id] = key

    async def clear_pending_state(user_id):
        existed = db.pending is not None
        db.pending = None
        return existed

    monkeypatch.setattr(commands, "get_pending_state", get_pending_state)
    monkeypatch.setattr(commands, "save_user_api_key", save_user_api_key)
    monkeypatch.setattr(commands, "clear_pending_state", clear_pending_state)
    return db


@pytest.fixture
def imgbb(monkeypatch):
    """Route imgbb requests to a queue of canned responses or exceptions."""
    state = SimpleNamespace(replies=[], requests=[])

    def handler(request):
        state.requests.append(request)
        reply = state.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(commands, "get_http_client", lambda: client)
    monkeypatch.setattr(commands, "RETRY_BACKOFF", 0)
    return state
