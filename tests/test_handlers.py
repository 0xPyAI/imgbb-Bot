"""Unit tests for the menus, key commands, error paths and Kurigram call signatures."""
import asyncio
import datetime
import inspect
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import httpx
import pytest
from pyrogram import Client, enums, errors
from pyrogram.types import CallbackQuery, Message

import commands
from dbmodels import UploadRecord
from tests.conftest import VALID_KEY
from tests.test_commands import (
    DELETE_URL,
    IMAGE_URL,
    _command,
    _fake_client,
    _ok,
    _photo_message,
    _private_text,
)

# Every test here runs with the settings and history tables stubbed out
pytestmark = pytest.mark.usefixtures("user_db")


def _callback(data, user_id=42):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id),
        answer=AsyncMock(),
        message=SimpleNamespace(
            edit_text=AsyncMock(), edit_reply_markup=AsyncMock(), reply=AsyncMock()
        ),
    )


def _document_message(mime_type, file_name, chat_type=enums.ChatType.PRIVATE, file_size=1000):
    message, status = _photo_message(chat_type=chat_type)
    message.photo = None
    message.document = SimpleNamespace(
        mime_type=mime_type, file_name=file_name, file_size=file_size
    )
    return message, status


def _buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


# ------------------------------
# Menus and key commands
# ------------------------------

async def test_start_shows_the_key_menu():
    message = _command("start")
    await commands.start_handler(_fake_client(), message)

    reply = message.reply_text.await_args
    assert "Welcome" in reply.args[0]
    add_key, delete_key = _buttons(reply.kwargs["reply_markup"])
    assert add_key.url == "https://t.me/test_bot?start=addkeyforuser"
    assert delete_key.callback_data == "deletekeyforuser"


async def test_add_key_link_opens_the_key_prompt(monkeypatch):
    set_pending_state = AsyncMock()
    monkeypatch.setattr(commands, "set_pending_state", set_pending_state)
    message = _command("start", "addkeyforuser")
    await commands.start_handler(_fake_client(), message)

    set_pending_state.assert_awaited_once_with(42, commands.WAITING_FOR_API_KEY)
    reply = message.reply_text.await_args
    assert "send me your imgbb API key within 5 minutes" in reply.args[0]
    assert _buttons(reply.kwargs["reply_markup"])[0].callback_data == "cancel_action"


async def test_key_prompt_reports_database_errors(monkeypatch):
    monkeypatch.setattr(commands, "set_pending_state", AsyncMock(side_effect=RuntimeError))
    message = _command("start", "addkeyforuser")
    await commands.start_handler(_fake_client(), message)
    assert "Could not start key setup" in message.reply_text.await_args.args[0]


@pytest.mark.parametrize(
    ("pending", "expected"), [(True, "Operation cancelled."), (False, "Nothing to cancel.")]
)
async def test_cancel_command(fake_db, pending, expected):
    fake_db.pending = SimpleNamespace() if pending else None
    message = _command("cancel")
    await commands.cancel_command_handler(None, message)

    assert message.reply_text.await_args.args[0].endswith(expected)
    assert fake_db.pending is None


async def test_cancel_button_clears_the_prompt(fake_db):
    fake_db.pending = SimpleNamespace()
    callback = _callback("cancel_action")
    # An old prompt that is already changed must not raise
    callback.message.edit_text.side_effect = errors.MessageNotModified()
    await commands.cancel_callback_handler(None, callback)

    assert fake_db.pending is None
    callback.answer.assert_awaited_once_with("Cancelled.")


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        (None, "No API key set"),
        ((None, None), "could not be read"),
        (
            (VALID_KEY, datetime.datetime(2026, 1, 2, 8, 34, tzinfo=datetime.timezone(
                datetime.timedelta(hours=5, minutes=30)
            ))),
            "Key: ••••cdef\n📅 Added: 2026-01-02 03:04 UTC",
        ),
        ((VALID_KEY, None), "Added: unknown"),
    ],
)
async def test_mystatus(monkeypatch, info, expected):
    monkeypatch.setattr(commands, "get_user_key_info", AsyncMock(return_value=info))
    message = _command("mystatus")
    await commands.mystatus_handler(None, message)
    assert expected in message.reply_text.await_args.args[0]


async def test_mystatus_reports_database_errors(monkeypatch):
    monkeypatch.setattr(commands, "get_user_key_info", AsyncMock(side_effect=RuntimeError))
    message = _command("mystatus")
    await commands.mystatus_handler(None, message)
    assert "Could not check status" in message.reply_text.await_args.args[0]


async def test_deletekey_command_and_button_ask_first():
    message = _command("deletekey")
    await commands.deletekey_command_handler(None, message)
    buttons = _buttons(message.reply_text.await_args.kwargs["reply_markup"])
    assert [button.callback_data for button in buttons] == ["deletekey_confirm", "deletekey_cancel"]

    callback = _callback("deletekeyforuser")
    await commands.deletekey_prompt_callback(None, callback)
    assert "Are you sure" in callback.message.reply.await_args.args[0]
    callback.answer.assert_awaited_once()


async def test_deletekey_cancel_keeps_the_key(monkeypatch):
    delete_user_api_key = AsyncMock()
    monkeypatch.setattr(commands, "delete_user_api_key", delete_user_api_key)
    callback = _callback("deletekey_cancel")
    await commands.deletekey_cancel_callback(None, callback)

    assert "Your key is safe" in callback.message.edit_text.await_args.args[0]
    delete_user_api_key.assert_not_awaited()


@pytest.mark.parametrize(
    ("deleted", "expected"), [(True, "deleted successfully"), (False, "No API key was found")]
)
async def test_deletekey_confirm(monkeypatch, deleted, expected):
    monkeypatch.setattr(commands, "delete_user_api_key", AsyncMock(return_value=deleted))
    callback = _callback("deletekey_confirm")
    await commands.deletekey_confirm_callback(None, callback)
    assert expected in callback.message.edit_text.await_args.args[0]


async def test_deletekey_confirm_reports_database_errors(monkeypatch):
    monkeypatch.setattr(commands, "delete_user_api_key", AsyncMock(side_effect=RuntimeError))
    callback = _callback("deletekey_confirm")
    await commands.deletekey_confirm_callback(None, callback)

    assert callback.answer.await_args.kwargs["show_alert"] is True
    callback.message.edit_text.assert_not_awaited()


# ------------------------------
# Expiry and history errors
# ------------------------------

async def test_expire_reports_database_errors(monkeypatch):
    monkeypatch.setattr(commands, "set_user_expiration", AsyncMock(side_effect=RuntimeError))
    monkeypatch.setattr(commands, "get_user_expiration", AsyncMock(side_effect=RuntimeError))

    saving = _command("expire", "1h")
    await commands.expire_handler(None, saving)
    assert "Could not save the setting" in saving.reply_text.await_args.args[0]

    reading = _command("expire")
    await commands.expire_handler(None, reading)
    assert "Could not read your settings" in reading.reply_text.await_args.args[0]


@pytest.mark.parametrize("data", ["expire:5", f"expire:{commands.MAX_EXPIRATION + 1}"])
async def test_expire_button_refuses_other_times(user_db, data):
    user_db.expiration = 3600
    callback = _callback(data)
    await commands.expire_callback(None, callback)

    assert callback.answer.await_args.kwargs["show_alert"] is True
    assert user_db.expiration == 3600


async def test_expire_button_reports_database_errors(monkeypatch):
    monkeypatch.setattr(commands, "set_user_expiration", AsyncMock(side_effect=RuntimeError))
    callback = _callback("expire:3600")
    await commands.expire_callback(None, callback)

    assert "Could not save" in callback.answer.await_args.args[0]
    callback.message.edit_text.assert_not_awaited()


async def test_history_reports_database_errors(monkeypatch):
    monkeypatch.setattr(commands, "get_upload_history", AsyncMock(side_effect=RuntimeError))
    message = _command("history")
    await commands.history_handler(None, message)
    assert "Could not read your history" in message.reply_text.await_args.args[0]


async def test_clear_history_asks_then_cancels(user_db):
    user_db.history = [UploadRecord(IMAGE_URL, DELETE_URL)]
    callback = _callback("historyclear")
    await commands.clear_history_prompt_callback(None, callback)

    assert callback.answer.await_args.kwargs["show_alert"] is True
    confirm = _buttons(callback.message.edit_reply_markup.await_args.args[0])
    assert [button.callback_data for button in confirm] == [
        "historyclear_confirm",
        "historyclear_cancel",
    ]

    await commands.clear_history_cancel_callback(None, callback)
    restored = _buttons(callback.message.edit_reply_markup.await_args.args[0])
    assert [button.callback_data for button in restored] == ["historyclear"]
    assert len(user_db.history) == 1


async def test_clear_history_reports_database_errors(monkeypatch):
    monkeypatch.setattr(commands, "clear_upload_history", AsyncMock(side_effect=RuntimeError))
    callback = _callback("historyclear_confirm")
    await commands.clear_history_confirm_callback(None, callback)

    assert callback.answer.await_args.kwargs["show_alert"] is True
    callback.message.edit_text.assert_not_awaited()


# ------------------------------
# Key capture errors
# ------------------------------

async def test_key_is_saved_when_its_message_cannot_be_deleted(fake_db):
    message = _private_text(VALID_KEY)
    message.delete.side_effect = errors.MessageDeleteForbidden()
    await commands.receive_api_key_handler(None, message)
    assert fake_db.saved == {42: VALID_KEY}


async def test_key_save_errors_are_reported(fake_db, monkeypatch):
    monkeypatch.setattr(commands, "save_user_api_key", AsyncMock(side_effect=RuntimeError))
    message = _private_text(VALID_KEY)
    await commands.receive_api_key_handler(None, message)
    assert "error occurred while saving your key" in message.reply_text.await_args.args[0]


async def test_key_leaves_the_chat_when_the_database_is_down(fake_db, monkeypatch):
    monkeypatch.setattr(commands, "get_pending_state", AsyncMock(side_effect=RuntimeError))
    key_message = _private_text(VALID_KEY)
    await commands.receive_api_key_handler(None, key_message)

    key_message.delete.assert_awaited_once()
    assert fake_db.saved == {42: VALID_KEY}

    chatter = _private_text("hello")
    await commands.receive_api_key_handler(None, chatter)
    assert "Send me an image" in chatter.reply_text.await_args.args[0]


# ------------------------------
# Upload checks and failures
# ------------------------------

@pytest.mark.parametrize(
    ("mime_type", "file_name", "expected"),
    [
        ("text/plain", "notes.txt", "Only image files are supported"),
        ("image/x-portable-pixmap", "scan.ppm", "not supported by imgbb"),
    ],
)
async def test_non_images_get_an_answer_in_private_only(mime_type, file_name, expected):
    private, _ = _document_message(mime_type, file_name)
    await commands.upload_image_handler(_fake_client(), private)
    assert expected in private.reply.await_args.args[0]

    group, _ = _document_message(mime_type, file_name, chat_type=enums.ChatType.SUPERGROUP)
    await commands.upload_image_handler(_fake_client(), group)
    group.reply.assert_not_awaited()


async def test_messages_without_a_sender_or_media_are_ignored():
    client = _fake_client()
    anonymous_admin, _ = _photo_message(chat_type=enums.ChatType.SUPERGROUP)
    anonymous_admin.from_user = None
    await commands.upload_image_handler(client, anonymous_admin)

    text_only, _ = _photo_message()
    text_only.photo = None
    await commands.upload_image_handler(client, text_only)

    anonymous_admin.reply.assert_not_awaited()
    text_only.reply.assert_not_awaited()


async def test_image_document_keeps_its_file_name(imgbb, with_key):
    imgbb.replies = [_ok()]
    message, status = _document_message("image/png", "scan.png")
    await commands.upload_image_handler(_fake_client(), message)
    await commands.wait_for_uploads(timeout=5)

    assert b'filename="scan.png"' in imgbb.requests[0].content
    assert IMAGE_URL in status.edit_text.await_args.args[0]


async def test_too_large_image_is_refused_before_download(with_key):
    client = _fake_client()
    message, _ = _document_message("image/png", "big.png", file_size=commands.MAX_FILE_SIZE + 1)
    await commands.upload_image_handler(client, message)

    assert "too large" in message.reply.await_args.args[0]
    client.download_media.assert_not_awaited()


async def test_album_reports_a_too_large_item_and_uploads_the_rest(monkeypatch, imgbb, with_key):
    monkeypatch.setattr(commands, "ALBUM_WAIT", 0.01)
    imgbb.replies = [_ok()]
    client = _fake_client()
    small, status = _photo_message(message_id=1, media_group_id=9)
    big, _ = _photo_message(message_id=2, media_group_id=9)
    big.photo.file_size = commands.MAX_FILE_SIZE + 1
    for message in (small, big):
        await commands.upload_image_handler(client, message)
    await commands.wait_for_uploads(timeout=5)

    text = status.edit_text.await_args.args[0]
    assert "1 of 2 images uploaded" in text
    assert "2. ⚠️ Image is too large" in text
    client.download_media.assert_awaited_once()
    assert client.delete_messages.await_args.args == (1, [1])


async def test_status_reply_failure_stops_quietly(with_key):
    client = _fake_client()
    message, _ = _photo_message()
    message.reply.side_effect = errors.ChatWriteForbidden()
    await commands.upload_image_handler(client, message)
    await commands.wait_for_uploads(timeout=5)
    client.download_media.assert_not_awaited()


async def test_unexpected_errors_before_upload_are_reported(monkeypatch, with_key):
    monkeypatch.setattr(commands, "get_user_expiration", AsyncMock(side_effect=RuntimeError))
    message, _ = _photo_message()
    await commands.upload_image_handler(_fake_client(), message)
    assert "Something went wrong" in message.reply.await_args.args[0]


@pytest.mark.parametrize(
    ("download", "expected"),
    [
        (AsyncMock(return_value=None), "Failed to download image"),
        (AsyncMock(side_effect=RuntimeError("boom")), "Something went wrong during upload"),
    ],
)
async def test_download_failures_are_reported(with_key, download, expected):
    client = _fake_client()
    client.download_media = download
    message, status = _photo_message()
    await commands.upload_image_handler(client, message)
    await commands.wait_for_uploads(timeout=5)

    assert expected in status.edit_text.await_args.args[0]
    client.delete_messages.assert_not_awaited()


async def test_slow_download_times_out(monkeypatch, with_key):
    monkeypatch.setattr(type(commands.settings), "DOWNLOAD_TIMEOUT", 0.01)
    client = _fake_client()

    async def slow_download(*args, **kwargs):
        await asyncio.sleep(5)

    client.download_media = slow_download
    message, status = _photo_message()
    await commands.upload_image_handler(client, message)
    await commands.wait_for_uploads(timeout=5)
    assert "Download timed out" in status.edit_text.await_args.args[0]


@pytest.mark.parametrize(
    ("replies", "expected"),
    [
        ([httpx.Response(503)] * (commands.UPLOAD_RETRIES + 1), "Upload failed after retries"),
        ([httpx.Response(200, json={"success": True})], "unexpected response format"),
    ],
)
async def test_imgbb_failures_are_reported(imgbb, with_key, user_db, replies, expected):
    imgbb.replies = list(replies)
    message, status = _photo_message()
    await commands.upload_image_handler(_fake_client(), message)
    await commands.wait_for_uploads(timeout=5)

    assert expected in status.edit_text.await_args.args[0]
    assert user_db.history == []


async def test_group_links_stay_when_telegram_refuses_cleanup(imgbb, with_key, user_db):
    imgbb.replies = [_ok()]
    client = _fake_client()
    # No delete rights in the group, and the uploader blocked the bot
    client.delete_messages.side_effect = errors.MessageDeleteForbidden()
    client.send_message.side_effect = errors.UserIsBlocked()
    message, status = _photo_message(chat_type=enums.ChatType.SUPERGROUP)
    await commands.upload_image_handler(client, message)
    await commands.wait_for_uploads(timeout=5)

    assert IMAGE_URL in status.edit_text.await_args.args[0]
    client.send_message.assert_awaited_once()
    assert len(user_db.history) == 1


async def test_history_errors_do_not_hide_the_links(monkeypatch, imgbb, with_key):
    monkeypatch.setattr(commands, "add_upload_history", AsyncMock(side_effect=RuntimeError))
    imgbb.replies = [_ok()]
    client = _fake_client()
    message, status = _photo_message()
    await commands.upload_image_handler(client, message)
    await commands.wait_for_uploads(timeout=5)

    assert IMAGE_URL in status.edit_text.await_args.args[0]
    client.delete_messages.assert_awaited_once()


async def test_unexpected_report_errors_end_with_a_message(monkeypatch, imgbb, with_key):
    monkeypatch.setattr(commands, "_report", AsyncMock(side_effect=RuntimeError))
    imgbb.replies = [_ok()]
    message, status = _photo_message()
    await commands.upload_image_handler(_fake_client(), message)
    await commands.wait_for_uploads(timeout=5)
    assert "Something went wrong during upload" in status.edit_text.await_args.args[0]


async def test_queued_upload_says_that_it_waits(monkeypatch, imgbb, with_key):
    semaphore = asyncio.Semaphore(1)
    monkeypatch.setattr(commands, "_transfer_semaphore", semaphore)
    imgbb.replies = [_ok()]
    # Another upload holds the only transfer slot
    await semaphore.acquire()
    message, status = _photo_message()
    await commands.upload_image_handler(_fake_client(), message)
    assert message.reply.await_args.args[0] == "⏳ Waiting in queue…"

    semaphore.release()
    await commands.wait_for_uploads(timeout=5)
    texts = [call.args[0] for call in status.edit_text.await_args_list]
    assert texts[0] == "⬇️ Downloading image…"
    assert IMAGE_URL in texts[-1]


async def test_shutdown_cancels_uploads_that_take_too_long():
    started = asyncio.Event()

    async def stuck_upload():
        started.set()
        await asyncio.sleep(60)

    commands._spawn(stuck_upload())
    await started.wait()
    await commands.wait_for_uploads(timeout=0.05)
    assert not commands._upload_tasks


def test_job_repr_hides_the_api_key():
    job = commands._UploadJob(None, [], VALID_KEY, None, None, False)
    assert VALID_KEY not in repr(job)


# ------------------------------
# Status message edits
# ------------------------------

async def test_final_status_waits_out_one_flood_wait():
    status = SimpleNamespace(edit_text=AsyncMock(side_effect=[errors.FloodWait(value=0), None]))
    assert await commands._set_status(status, "done", final=True) is True
    assert status.edit_text.await_count == 2


async def test_final_status_gives_up_after_a_second_failure():
    status = SimpleNamespace(
        edit_text=AsyncMock(side_effect=[errors.FloodWait(value=0), errors.MessageIdInvalid()])
    )
    assert await commands._set_status(status, "done", final=True) is False


async def test_progress_status_skips_flood_wait():
    status = SimpleNamespace(edit_text=AsyncMock(side_effect=errors.FloodWait(value=0)))
    assert await commands._set_status(status, "50%") is False
    assert status.edit_text.await_count == 1


@pytest.mark.parametrize(
    ("error", "shown"), [(errors.MessageNotModified(), True), (errors.MessageIdInvalid(), False)]
)
async def test_status_edit_errors(error, shown):
    status = SimpleNamespace(edit_text=AsyncMock(side_effect=error))
    assert await commands._set_status(status, "text", final=True) is shown


# ------------------------------
# Kurigram call signatures
# ------------------------------
# Mocks made with create_autospec take only the arguments that Kurigram's real
# methods take, so a wrong or renamed argument fails these tests.

def _spec(cls):
    """Return an autospec instance of a Kurigram class whose methods can be awaited.

    Kurigram wraps each method so that it also runs without an event loop, and
    that wrapper hides from create_autospec that the method is a coroutine.
    """
    plain = {
        name: inspect.unwrap(member)
        for name, member in inspect.getmembers(cls, inspect.isfunction)
        if hasattr(member, "__wrapped__")
    }
    return create_autospec(type(cls.__name__, (cls,), plain), instance=True)


def _spec_client():
    client = _spec(Client)
    client.me = SimpleNamespace(username="test_bot")
    client.download_media.side_effect = lambda *args, **kwargs: io.BytesIO(b"image")
    return client


def _spec_message(chat_type=enums.ChatType.PRIVATE, message_id=10, media_group_id=None, text=None):
    message = _spec(Message)
    message.id = message_id
    message.from_user = SimpleNamespace(id=42, username=None, first_name="Alice")
    message.chat = SimpleNamespace(id=1, type=chat_type, title="Cat Lovers")
    message.photo = None if text else SimpleNamespace(file_id="x", file_size=10)
    message.document = None
    message.caption = None
    message.text = text
    message.command = None
    message.media_group_id = media_group_id
    message.reply.return_value = _spec(Message)
    message.reply_text.return_value = _spec(Message)
    return message


def _spec_callback(data):
    callback = _spec(CallbackQuery)
    callback.data = data
    callback.from_user = SimpleNamespace(id=42)
    callback.message = _spec(Message)
    return callback


def _final_text(message):
    return message.reply.return_value.edit_text.await_args.args[0]


async def test_upload_calls_match_kurigram(monkeypatch, imgbb, with_key, fake_db):
    monkeypatch.setattr(commands, "ALBUM_WAIT", 0.01)
    imgbb.replies = [_ok() for _ in range(5)]
    client = _spec_client()

    private = _spec_message()
    group = _spec_message(chat_type=enums.ChatType.SUPERGROUP)
    album = [_spec_message(message_id=n, media_group_id=3) for n in (20, 21)]
    for message in [private, group, *album]:
        await commands.upload_image_handler(client, message)
    link = _spec_message(text="https://example.com/cat.png")
    await commands.receive_api_key_handler(client, link)
    await commands.wait_for_uploads(timeout=5)

    for message in (private, group, album[0], link):
        assert IMAGE_URL in _final_text(message)
    assert "tg://user?id=42" in _final_text(private)
    assert client.delete_messages.await_count == 4
    client.send_message.assert_awaited_once()


async def test_command_calls_match_kurigram(monkeypatch, user_db, fake_db):
    monkeypatch.setattr(commands, "get_user_key_info", AsyncMock(return_value=(VALID_KEY, None)))
    monkeypatch.setattr(commands, "set_pending_state", AsyncMock())
    user_db.history = [UploadRecord(IMAGE_URL, DELETE_URL)]
    client = _spec_client()
    handlers = [
        (commands.start_handler, ["start"]),
        (commands.start_handler, ["start", "addkeyforuser"]),
        (commands.help_handler, ["help"]),
        (commands.mystatus_handler, ["mystatus"]),
        (commands.deletekey_command_handler, ["deletekey"]),
        (commands.cancel_command_handler, ["cancel"]),
        (commands.expire_handler, ["expire"]),
        (commands.expire_handler, ["expire", "2h"]),
        (commands.history_handler, ["history"]),
    ]
    for handler, words in handlers:
        message = _spec_message(text="/" + " ".join(words))
        message.command = words
        await handler(client, message)
        message.reply_text.assert_awaited_once()

    for text in (VALID_KEY, "hello"):
        message = _spec_message(text=text)
        await commands.receive_api_key_handler(client, message)
        message.reply_text.assert_awaited_once()


async def test_callback_calls_match_kurigram(monkeypatch, user_db, fake_db):
    monkeypatch.setattr(commands, "delete_user_api_key", AsyncMock(return_value=True))
    callbacks = [
        (commands.cancel_callback_handler, "cancel_action"),
        (commands.deletekey_prompt_callback, "deletekeyforuser"),
        (commands.deletekey_cancel_callback, "deletekey_cancel"),
        (commands.deletekey_confirm_callback, "deletekey_confirm"),
        (commands.expire_callback, "expire:3600"),
        (commands.expire_callback, "expire:5"),
        (commands.clear_history_prompt_callback, "historyclear"),
        (commands.clear_history_cancel_callback, "historyclear_cancel"),
        (commands.clear_history_confirm_callback, "historyclear_confirm"),
    ]
    for handler, data in callbacks:
        callback = _spec_callback(data)
        await handler(None, callback)
        callback.answer.assert_awaited_once()
