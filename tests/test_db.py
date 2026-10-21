"""Database tests against a real PostgreSQL server.

Set TEST_DATABASE_URL to an empty, throwaway database, for example
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/bot_test.
Each test drops and creates the bot's tables, so never point it at real data.
"""
import asyncio
import datetime
import os

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

import dbmodels
from dbmodels import UploadRecord

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="set TEST_DATABASE_URL to an empty PostgreSQL database to run these tests",
)

KEY = "0123456789abcdef0123456789abcdef"
# Above the 32-bit range, as many Telegram user IDs are
USER = 7_000_000_001
OTHER_USER = 7_000_000_002

BOT_TABLES = {"apikey_tg", "pending_state", "user_settings", "upload_history"}


async def _execute(*statements):
    async with dbmodels.engine.begin() as conn:
        for statement in statements:
            await conn.execute(text(statement))


async def _rows(query, **params):
    async with dbmodels.engine.connect() as conn:
        return [tuple(row) for row in await conn.execute(text(query), params)]


async def _columns(table):
    rows = await _rows(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = :table",
        table=table,
    )
    return dict(rows)


@pytest.fixture(autouse=True)
async def empty_db():
    """Start each test without the bot's tables, and close the pool in the test's loop."""
    tables = {
        name
        for name, in await _rows(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema()"
        )
    }
    if tables - BOT_TABLES:
        pytest.exit(
            f"TEST_DATABASE_URL holds other tables {sorted(tables - BOT_TABLES)}; "
            "use an empty database"
        )
    await _execute(*(f"DROP TABLE IF EXISTS {table} CASCADE" for table in BOT_TABLES))
    yield
    await dbmodels.engine.dispose()


@pytest.fixture
async def schema():
    await dbmodels.init_db()


# ------------------------------
# Schema and migrations
# ------------------------------

async def test_init_db_creates_tables_and_runs_twice():
    await dbmodels.init_db()
    # Every start of the bot runs it again
    await dbmodels.init_db()

    api_keys = await _columns("apikey_tg")
    assert api_keys["user_id"] == "bigint"
    assert api_keys["creation_date"] == "timestamp with time zone"
    assert set(await _columns("user_settings")) == {"user_id", "expiration", "updated_at"}
    assert (await _columns("upload_history"))["expires_at"] == "timestamp with time zone"
    indexes = {name for name, in await _rows("SELECT indexname FROM pg_indexes")}
    assert {"ix_upload_history_user_id_id", "ix_upload_history_expires_at"} <= indexes


async def test_legacy_tables_are_migrated_and_keep_their_rows():
    # The shape that older versions of the bot left in production databases
    await _execute(
        "CREATE TABLE apikey_tg (id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL, "
        "api_key VARCHAR NOT NULL, creation_date TIMESTAMP WITHOUT TIME ZONE)",
        "CREATE UNIQUE INDEX ix_apikey_tg_user_id ON apikey_tg (user_id)",
        "INSERT INTO apikey_tg (user_id, api_key, creation_date) "
        f"VALUES (5, '{KEY}', '2024-01-02 03:04:05')",
        "CREATE TABLE pending_state (user_id BIGSERIAL PRIMARY KEY, "
        "state VARCHAR NOT NULL, created_at TIMESTAMP WITH TIME ZONE)",
    )
    await dbmodels.init_db()

    api_keys = await _columns("apikey_tg")
    assert api_keys["user_id"] == "bigint"
    assert api_keys["creation_date"] == "timestamp with time zone"
    # The key saved before encryption still works, and its date is read as UTC
    api_key, created = await dbmodels.get_user_key_info(5)
    assert api_key == KEY
    assert created == datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)

    await dbmodels.save_user_api_key(USER, KEY)
    assert await dbmodels.get_user_api_key(USER) == KEY
    await dbmodels.set_pending_state(USER, "waiting_for_api_key")
    assert (await dbmodels.get_pending_state(USER)).state == "waiting_for_api_key"


async def test_init_db_leaves_other_tables_alone():
    # A production database can also hold the tables of other applications
    await _execute(
        "CREATE TABLE users (user_id BIGINT PRIMARY KEY, name TEXT)",
        "INSERT INTO users VALUES (1, 'kept')",
    )
    try:
        await dbmodels.init_db()
        assert await _rows("SELECT name FROM users") == [("kept",)]
    finally:
        await _execute("DROP TABLE users")


# ------------------------------
# API keys and pending state
# ------------------------------

async def test_api_key_is_encrypted_updated_and_deleted(schema):
    await dbmodels.save_user_api_key(USER, f"  {KEY}  ")
    [(stored,)] = await _rows("SELECT api_key FROM apikey_tg")
    assert stored.startswith("gAAAA")  # a Fernet token, not the key

    api_key, created = await dbmodels.get_user_key_info(USER)
    assert api_key == KEY
    assert created.tzinfo is not None

    await dbmodels.save_user_api_key(USER, KEY.upper())
    assert await dbmodels.get_user_api_key(USER) == KEY.upper()
    assert len(await _rows("SELECT id FROM apikey_tg")) == 1

    assert await dbmodels.delete_user_api_key(USER) is True
    assert await dbmodels.delete_user_api_key(USER) is False
    assert await dbmodels.get_user_key_info(USER) is None


async def test_key_saved_with_another_fernet_key_is_not_used(schema):
    foreign = Fernet(Fernet.generate_key()).encrypt(KEY.encode()).decode()
    await _execute(
        "INSERT INTO apikey_tg (user_id, api_key, creation_date) "
        f"VALUES ({USER}, '{foreign}', now())"
    )

    api_key, created = await dbmodels.get_user_key_info(USER)
    assert api_key is None
    assert created is not None
    assert await dbmodels.get_user_api_key(USER) is None


async def test_concurrent_key_saves_keep_one_row(schema):
    keys = [f"{number:032x}" for number in range(20)]
    await asyncio.gather(*(dbmodels.save_user_api_key(USER, key) for key in keys))

    assert len(await _rows("SELECT id FROM apikey_tg")) == 1
    assert await dbmodels.get_user_api_key(USER) in keys


async def test_pending_state_lifecycle(schema):
    await dbmodels.set_pending_state(USER, "waiting_for_api_key")
    # A second tap on Add Key restarts the prompt
    await dbmodels.set_pending_state(USER, "waiting_for_api_key")
    pending = await dbmodels.get_pending_state(USER)
    assert pending.state == "waiting_for_api_key"
    assert pending.created_at.tzinfo is not None

    await dbmodels.set_pending_state(OTHER_USER, "waiting_for_api_key")
    await _execute(
        "UPDATE pending_state SET created_at = now() - interval '1 hour' "
        f"WHERE user_id = {OTHER_USER}"
    )
    assert await dbmodels.cleanup_expired_states(max_age_seconds=300) == 1
    assert await dbmodels.get_pending_state(OTHER_USER) is None

    assert await dbmodels.clear_pending_state(USER) is True
    assert await dbmodels.clear_pending_state(USER) is False


async def test_pool_serves_a_burst_of_queries(schema):
    # More callers than pool_size + max_overflow: the others wait for a connection
    await dbmodels.save_user_api_key(USER, KEY)
    results = await asyncio.gather(*(dbmodels.get_user_api_key(USER) for _ in range(60)))
    assert results == [KEY] * 60


# ------------------------------
# Expiry settings and upload history
# ------------------------------

async def test_expiration_setting(schema):
    assert await dbmodels.get_user_expiration(USER) is None
    await dbmodels.set_user_expiration(USER, 3600)
    await dbmodels.set_user_expiration(USER, 86400)
    assert await dbmodels.get_user_expiration(USER) == 86400

    await dbmodels.set_user_expiration(USER, None)
    assert await dbmodels.get_user_expiration(USER) is None


async def test_history_keeps_newest_and_hides_expired(schema, monkeypatch):
    monkeypatch.setattr(dbmodels, "HISTORY_KEEP", 4)
    now = datetime.datetime.now(datetime.timezone.utc)
    await dbmodels.add_upload_history(OTHER_USER, [UploadRecord("https://i.ibb.co/o/0.png")])
    await dbmodels.add_upload_history(
        USER,
        [UploadRecord(f"https://i.ibb.co/u/{n}.png", f"https://ibb.co/u/d{n}") for n in range(3)],
    )
    await dbmodels.add_upload_history(
        USER,
        [
            UploadRecord("https://i.ibb.co/u/3.png", None, now - datetime.timedelta(minutes=1)),
            UploadRecord(
                "https://i.ibb.co/u/4.png", "https://ibb.co/u/d4", now + datetime.timedelta(hours=1)
            ),
        ],
    )

    # The oldest upload is pruned, and the expired one is not listed
    history = await dbmodels.get_upload_history(USER, limit=10)
    assert [record.image_url[-5:] for record in history] == ["4.png", "2.png", "1.png"]
    assert history[0].delete_url == "https://ibb.co/u/d4"
    assert history[0].expires_at > now
    assert all(record.created_at.tzinfo for record in history)
    assert len(await dbmodels.get_upload_history(USER, limit=1)) == 1

    stored = await _rows("SELECT delete_url FROM upload_history WHERE delete_url IS NOT NULL")
    assert stored and all(value.startswith("gAAAA") for value, in stored)
    # Pruning one user's history never touches another user's uploads
    assert len(await dbmodels.get_upload_history(OTHER_USER)) == 1

    assert await dbmodels.cleanup_expired_uploads() == 1
    assert await dbmodels.clear_upload_history(USER) == 3
    assert await dbmodels.clear_upload_history(USER) == 0


async def test_history_survives_a_changed_fernet_key(schema):
    foreign = Fernet(Fernet.generate_key()).encrypt(b"https://ibb.co/x/del").decode()
    await _execute(
        "INSERT INTO upload_history (user_id, image_url, delete_url, created_at) "
        f"VALUES ({USER}, 'https://i.ibb.co/x/1.png', '{foreign}', now())"
    )

    [record] = await dbmodels.get_upload_history(USER)
    assert record.image_url == "https://i.ibb.co/x/1.png"
    assert record.delete_url is None


async def test_concurrent_history_writes_settle_at_the_limit(schema, monkeypatch):
    monkeypatch.setattr(dbmodels, "HISTORY_KEEP", 5)
    await asyncio.gather(
        *(
            dbmodels.add_upload_history(USER, [UploadRecord(f"https://i.ibb.co/c/{n}.png")])
            for n in range(12)
        )
    )
    await dbmodels.add_upload_history(USER, [UploadRecord("https://i.ibb.co/c/last.png")])

    [(count,)] = await _rows("SELECT count(*) FROM upload_history")
    assert count == 5
