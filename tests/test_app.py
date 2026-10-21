"""Unit tests for update routing, startup and shutdown, settings and the helper scripts."""
import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pyrogram
import pytest
from cryptography.fernet import Fernet
from pyrogram import Client, enums, types

import commands
import config
import crypto
import http_client
import init_tables
import pyro
from config import BASE_DIR, settings

# ------------------------------
# Update routing
# ------------------------------


@pytest.fixture
async def app():
    """An offline Kurigram client with the bot's handlers registered."""
    client = Client("routing", api_id=1, api_hash="x", bot_token="1:x", in_memory=True)
    client.me = types.User(id=1, is_bot=True, username="test_bot")
    commands.register_handlers(client)
    # Kurigram 2.2.26 adds the handlers in a task
    for _ in range(5):
        await asyncio.sleep(0)
    return client


async def _route(client, update):
    """Return the handler that Kurigram's dispatcher would run for `update`."""
    for group in client.dispatcher.groups.values():
        for handler in group:
            update_type = (
                types.CallbackQuery
                if type(handler).__name__ == "CallbackQueryHandler"
                else types.Message
            )
            if isinstance(update, update_type) and await handler.check(client, update):
                return handler.callback
    return None


def _message(text=None, chat_type=enums.ChatType.PRIVATE, **media):
    return types.Message(
        id=1,
        chat=types.Chat(id=42, type=chat_type),
        from_user=types.User(id=42),
        text=text,
        **media,
    )


async def test_handlers_are_registered(app):
    callbacks = [handler.callback for group in app.dispatcher.groups.values() for handler in group]
    assert len(callbacks) == 17
    # The catch-all text handler must come after every command handler
    assert callbacks.index(commands.receive_api_key_handler) == 15


@pytest.mark.parametrize("command", [command.command for command in pyro.BOT_COMMANDS])
async def test_every_menu_command_has_its_own_handler(app, command):
    handler = await _route(app, _message(f"/{command}"))
    assert handler is not None
    assert handler is not commands.receive_api_key_handler
    # A command addressed to this bot in a group style also works in private
    assert await _route(app, _message(f"/{command}@test_bot")) is handler


@pytest.mark.parametrize(
    ("update", "expected"),
    [
        (_message("hello"), commands.receive_api_key_handler),
        (_message("https://example.com/cat.png"), commands.receive_api_key_handler),
        (_message("hello", chat_type=enums.ChatType.SUPERGROUP), None),
        (_message("/start", chat_type=enums.ChatType.SUPERGROUP), None),
        (_message(photo=object()), commands.upload_image_handler),
        (_message(document=object(), chat_type=enums.ChatType.SUPERGROUP), commands.upload_image_handler),
    ],
)
async def test_messages_reach_the_right_handler(app, update, expected):
    assert await _route(app, update) is expected


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("cancel_action", commands.cancel_callback_handler),
        ("deletekeyforuser", commands.deletekey_prompt_callback),
        ("deletekey_cancel", commands.deletekey_cancel_callback),
        ("deletekey_confirm", commands.deletekey_confirm_callback),
        ("expire:3600", commands.expire_callback),
        ("historyclear", commands.clear_history_prompt_callback),
        ("historyclear_cancel", commands.clear_history_cancel_callback),
        ("historyclear_confirm", commands.clear_history_confirm_callback),
        ("expire:soon", None),
        ("unknown", None),
    ],
)
async def test_buttons_reach_the_right_handler(app, data, expected):
    callback = types.CallbackQuery(
        id="1", from_user=types.User(id=42), chat_instance="1", data=data
    )
    assert await _route(app, callback) is expected


async def test_help_and_readme_list_every_menu_command():
    message = SimpleNamespace(reply_text=AsyncMock())
    await commands.help_handler(None, message)
    help_text = message.reply_text.await_args.args[0]
    readme = (BASE_DIR / "README.md").read_text(encoding="utf-8")

    for command in pyro.BOT_COMMANDS:
        assert f"/{command.command}" in help_text
        assert f"`/{command.command}`" in readme


# ------------------------------
# Startup and shutdown
# ------------------------------

def _recorder(calls, name, result=None, error=None):
    async def step(*args, **kwargs):
        calls.append(name)
        if error:
            raise error
        return result

    return step


@pytest.fixture
def lifecycle(monkeypatch, tmp_path):
    """Replace Telegram, the database and the HTTP client, and record the steps of main()."""
    calls = []
    heartbeat = tmp_path / "heartbeat"
    monkeypatch.setenv("HEARTBEAT_FILE", str(heartbeat))
    fake_app = SimpleNamespace(
        start=_recorder(calls, "start"),
        stop=_recorder(calls, "stop"),
        set_bot_commands=_recorder(calls, "set_bot_commands"),
        me=SimpleNamespace(username="test_bot"),
        is_connected=True,
    )

    async def idle():
        calls.append("idle")
        # Let the background tasks start
        await asyncio.sleep(0.05)
        calls.append(f"heartbeat while running: {heartbeat.exists()}")

    monkeypatch.setattr(pyro, "app", fake_app)
    monkeypatch.setattr(pyro, "idle", idle)
    monkeypatch.setattr(pyro, "init_db", _recorder(calls, "init_db"))
    monkeypatch.setattr(pyro, "register_handlers", lambda client: calls.append("register_handlers"))
    monkeypatch.setattr(pyro, "wait_for_uploads", _recorder(calls, "wait_for_uploads"))
    monkeypatch.setattr(pyro, "close_http_client", _recorder(calls, "close_http_client"))
    monkeypatch.setattr(pyro, "close_db", _recorder(calls, "close_db"))
    return SimpleNamespace(calls=calls, app=fake_app, heartbeat=heartbeat)


async def test_main_starts_and_stops_in_order(lifecycle):
    await pyro.main()

    assert lifecycle.calls == [
        "init_db",
        "register_handlers",
        "start",
        "set_bot_commands",
        "idle",
        "heartbeat while running: True",
        # Uploads finish before the Telegram connection closes
        "wait_for_uploads",
        "stop",
        "close_http_client",
        "close_db",
    ]
    assert not lifecycle.heartbeat.exists()


async def test_main_runs_when_the_command_menu_cannot_be_set(lifecycle):
    lifecycle.app.set_bot_commands = _recorder(
        lifecycle.calls, "set_bot_commands", error=RuntimeError("flood")
    )
    await pyro.main()
    assert "idle" in lifecycle.calls
    assert lifecycle.calls[-1] == "close_db"


async def test_main_cleans_up_after_a_crash(lifecycle, monkeypatch):
    monkeypatch.setattr(pyro, "idle", _recorder(lifecycle.calls, "idle", error=RuntimeError("crash")))
    with pytest.raises(RuntimeError, match="crash"):
        await pyro.main()
    assert lifecycle.calls[-4:] == ["wait_for_uploads", "stop", "close_http_client", "close_db"]


async def test_main_stops_at_once_on_a_bad_fernet_key(lifecycle, monkeypatch):
    monkeypatch.setenv("FERNET_KEY", "your_fernet_key_here")
    monkeypatch.setattr(crypto, "_fernet_instance", None)
    with pytest.raises(RuntimeError, match="Invalid FERNET_KEY"):
        await pyro.main()
    assert lifecycle.calls == []


@pytest.fixture
def restore_signal_handlers():
    """Kurigram's idle() replaces these handlers; give the test runner its own back."""
    numbers = (signal.SIGINT, signal.SIGTERM, signal.SIGABRT)
    saved = {number: signal.getsignal(number) for number in numbers}
    yield
    for number, handler in saved.items():
        signal.signal(number, handler)


@pytest.mark.parametrize("signal_number", [signal.SIGINT, signal.SIGTERM])
async def test_stop_signal_shuts_down_gracefully(
    lifecycle, monkeypatch, restore_signal_handlers, signal_number
):
    # Docker and systemd stop the bot with SIGTERM; Ctrl+C sends SIGINT
    monkeypatch.setattr(pyro, "idle", pyrogram.idle)
    asyncio.get_running_loop().call_later(0.2, signal.raise_signal, signal_number)

    await asyncio.wait_for(pyro.main(), timeout=10)
    assert lifecycle.calls[-4:] == ["wait_for_uploads", "stop", "close_http_client", "close_db"]


async def test_heartbeat_file_is_touched_and_removed(tmp_path):
    path = tmp_path / "heartbeat"
    task = asyncio.create_task(pyro._heartbeat(path, interval_seconds=0.01))
    await asyncio.sleep(0.05)
    assert path.exists()

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not path.exists()


async def test_periodic_cleanup_keeps_running_after_errors(monkeypatch):
    state_calls = []

    async def cleanup_states(max_age_seconds):
        state_calls.append(max_age_seconds)
        if len(state_calls) == 1:
            raise RuntimeError("database restarting")
        return 1

    cleanup_uploads = AsyncMock(return_value=2)
    monkeypatch.setattr(pyro, "cleanup_expired_states", cleanup_states)
    monkeypatch.setattr(pyro, "cleanup_expired_uploads", cleanup_uploads)

    task = asyncio.create_task(pyro._periodic_cleanup(interval_seconds=0.01))
    await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert len(state_calls) >= 2
    assert set(state_calls) == {commands.KEY_PROMPT_TIMEOUT}
    assert cleanup_uploads.await_count >= 1


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("DEBUG", [logging.DEBUG, logging.INFO]),
        ("warning", [logging.WARNING, logging.WARNING]),
        ("NOT_A_LEVEL", [logging.INFO, logging.INFO]),
        ("BASIC_FORMAT", [logging.INFO, logging.INFO]),
    ],
)
def test_log_level_and_kurigram_floor(level, expected):
    # Kurigram's DEBUG output holds the bot token and users' API keys
    code = (
        "import logging, pyro; "
        "print(logging.getLogger().level, logging.getLogger('pyrogram').getEffectiveLevel())"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=BASE_DIR,
        env={**os.environ, "LOG_LEVEL": level, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert [int(value) for value in result.stdout.split()[-2:]] == expected


# ------------------------------
# HTTP client and helper scripts
# ------------------------------

async def test_http_client_is_shared_and_made_again_after_close():
    first = http_client.get_http_client()
    assert http_client.get_http_client() is first

    await http_client.close_http_client()
    assert first.is_closed
    second = http_client.get_http_client()
    assert second is not first

    await http_client.close_http_client()
    # A second close is harmless
    await http_client.close_http_client()


async def test_init_tables_closes_the_pool_when_setup_fails(monkeypatch):
    close_db = AsyncMock()
    monkeypatch.setattr(init_tables, "init_db", AsyncMock(side_effect=RuntimeError("no database")))
    monkeypatch.setattr(init_tables, "close_db", close_db)

    with pytest.raises(RuntimeError):
        await init_tables.main()
    close_db.assert_awaited_once()


def test_generate_key_prints_a_valid_fernet_key():
    result = subprocess.run(
        [sys.executable, str(BASE_DIR / "generate_key.py")],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    line = next(line for line in result.stdout.splitlines() if line.startswith("FERNET_KEY="))
    Fernet(line.split("=", 1)[1].encode())


# ------------------------------
# Settings
# ------------------------------

@pytest.fixture
def no_config_files(monkeypatch):
    """Ignore any .env or config.env on the machine that runs the tests."""
    monkeypatch.setattr(config, "_file_values", {})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgres://u:p@db:5432/bot", "postgresql+asyncpg://u:p@db:5432/bot"),
        ("postgresql://u:p@db/bot", "postgresql+asyncpg://u:p@db/bot"),
        ("postgresql+asyncpg://u:p@db/bot", "postgresql+asyncpg://u:p@db/bot"),
    ],
)
def test_database_url_uses_asyncpg(monkeypatch, raw, expected):
    monkeypatch.setenv("DATABASE_URL", raw)
    assert settings.DATABASE_URL == expected


def test_numbers_have_a_floor(monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENT_DOWNLOADS", "0")
    monkeypatch.setenv("DOWNLOAD_TIMEOUT", "-5")
    assert settings.MAX_CONCURRENT_DOWNLOADS == 1
    assert settings.DOWNLOAD_TIMEOUT == 1


def test_optional_settings_have_defaults(monkeypatch, no_config_files):
    for name in ("LOG_LEVEL", "MAX_CONCURRENT_DOWNLOADS", "DOWNLOAD_TIMEOUT", "SESSION_DIR",
                 "HEARTBEAT_FILE", "SENTRY_DSN"):
        monkeypatch.delenv(name, raising=False)

    assert settings.LOG_LEVEL == "INFO"
    assert settings.MAX_CONCURRENT_DOWNLOADS == 4
    assert settings.DOWNLOAD_TIMEOUT == 60
    assert settings.SESSION_DIR == str(BASE_DIR)
    assert settings.HEARTBEAT_FILE is None
    assert settings.SENTRY_DSN is None


def test_legacy_names_still_work(monkeypatch, no_config_files):
    for name in ("API_ID", "BOT_TOKEN", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("YOUR_API_ID", "777")
    monkeypatch.setenv("token", "1:legacy")
    monkeypatch.setenv("grespost1", "postgres://u:p@db/bot")

    assert settings.API_ID == 777
    assert settings.BOT_TOKEN == "1:legacy"
    assert settings.DATABASE_URL == "postgresql+asyncpg://u:p@db/bot"


def test_config_file_order(tmp_path):
    """Environment variables beat .env, which beats config.env; single quotes keep $."""
    shutil.copy(BASE_DIR / "config.py", tmp_path)
    (tmp_path / ".env").write_text("FIRST=from-dotenv\n")
    (tmp_path / "config.env").write_text(
        "FIRST=from-config\nSECOND=from-config\nTHIRD=from-config\n"
        "PASSWORD='p$ss${word}'\n"
    )
    env = {key: value for key, value in os.environ.items() if key not in ("FIRST", "SECOND")}
    env.update(THIRD="from-environment", PYTHONDONTWRITEBYTECODE="1")
    code = (
        "import config; "
        "print('|'.join(config.get(k) for k in ('FIRST', 'SECOND', 'THIRD', 'PASSWORD')))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "from-dotenv|from-config|from-environment|p$ss${word}"
