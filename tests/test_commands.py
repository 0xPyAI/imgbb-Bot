"""Unit tests for the key capture flow, image checks and the imgbb upload path."""
import asyncio
import datetime
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pyrogram import enums, errors
from pyrogram.file_id import FileId, FileType
from pyrogram.parser.html import HTML

import commands
from dbmodels import UploadRecord
from tests.conftest import VALID_KEY

IMAGE_BYTES = b"\x89PNG\r\n\x1a\nfake-image-bytes"
IMAGE_URL = "https://i.ibb.co/abc/photo.jpg"
DELETE_URL = "https://ibb.co/abc/0123456789abcdef"

# Every test here runs with the settings and history tables stubbed out
pytestmark = pytest.mark.usefixtures("user_db")


def _command(*words, user_id=42):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        command=list(words),
        reply_text=AsyncMock(),
    )


# ------------------------------
# Image format detection
# ------------------------------

def test_allowed_formats():
    for ext in ("jpeg", "png", "webp", "gif", "avif"):
        assert ext in commands.ALLOWED_FORMATS
    for ext in ("exe", "mp4"):
        assert ext not in commands.ALLOWED_FORMATS


def test_max_file_size_limit():
    assert commands.MAX_FILE_SIZE == 32 * 1024 * 1024  # 32 MB


@pytest.mark.parametrize(
    ("mime_type", "file_name", "expected"),
    [
        ("image/png", "a.png", "png"),
        ("image/jpeg", None, "jpeg"),
        ("image/x-ms-bmp", "scan.bmp", "bmp"),
        ("image/x-icon", None, "ico"),
        ("image/vnd.microsoft.icon", None, "ico"),
        ("image/svg+xml", None, "svg"),
        ("application/octet-stream", "photo.WEBP", "webp"),
        ("image/x-unknown", "a.bin", None),
        ("video/mp4", "clip.mp4", None),
        (None, None, None),
    ],
)
def test_image_extension(mime_type, file_name, expected):
    document = SimpleNamespace(mime_type=mime_type, file_name=file_name)
    assert commands._image_extension(document) == expected


@pytest.mark.parametrize(
    ("text", "is_link"),
    [
        ("https://example.com/cat.png", True),
        ("http://example.com/a?b=c", True),
        ("HTTPS://EXAMPLE.COM/x", True),
        ("ftp://example.com/cat.png", False),
        ("example.com/cat.png", False),
        ("https://", False),
        ("look at https://example.com/cat.png", False),
        ("javascript:alert(1)", False),
        ("http://[::1", False),
        ("https://example.com/" + "a" * 3000, False),
        ("", False),
    ],
)
def test_image_url(text, is_link):
    assert (commands._image_url(text) == text) is is_link


# ------------------------------
# Upload expiry
# ------------------------------

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("90", 90),
        ("30m", 1800),
        ("12h", 43200),
        ("2 h", 7200),
        ("7D", 604800),
        ("2w", 1209600),
        ("180d", commands.MAX_EXPIRATION),
        ("off", None),
        ("Never", None),
        ("0", None),
    ],
)
def test_parse_expiration(text, expected):
    assert commands._parse_expiration(text) == expected


@pytest.mark.parametrize("text", ["59s", "181d", "soon", "1h30m", "-5m", "", "h"])
def test_parse_expiration_rejects_bad_times(text):
    with pytest.raises(ValueError):
        commands._parse_expiration(text)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(60, "1 minute"), (5400, "90 minutes"), (3600, "1 hour"), (172800, "2 days"), (61, "61 seconds")],
)
def test_format_duration(seconds, expected):
    assert commands._format_duration(seconds) == expected


async def test_expire_command_saves_time(user_db):
    message = _command("expire", "12h")
    await commands.expire_handler(None, message)

    assert user_db.expiration == 12 * 3600
    assert "12 hours" in message.reply_text.await_args.args[0]


async def test_expire_command_turns_expiry_off(user_db):
    user_db.expiration = 3600
    message = _command("expire", "off")
    await commands.expire_handler(None, message)

    assert user_db.expiration is None
    assert "never expire" in message.reply_text.await_args.args[0]


async def test_expire_command_rejects_bad_time(user_db):
    message = _command("expire", "5s")
    await commands.expire_handler(None, message)

    assert user_db.expiration is None
    assert "1 minute to 180 days" in message.reply_text.await_args.args[0]


async def test_expire_menu_marks_current_choice(user_db):
    user_db.expiration = 86400
    message = _command("expire")
    await commands.expire_handler(None, message)

    reply = message.reply_text.await_args
    assert "deleted after 1 day" in reply.args[0]
    rows = reply.kwargs["reply_markup"].inline_keyboard
    labels = [button.text for row in rows for button in row]
    assert "✅ 1 day" in labels
    assert "Never" in labels


async def test_expire_button_saves_choice(user_db):
    user_db.expiration = 3600
    callback = SimpleNamespace(
        data="expire:0",
        from_user=SimpleNamespace(id=42),
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock()),
    )
    await commands.expire_callback(None, callback)

    assert user_db.expiration is None
    assert "never expire" in callback.message.edit_text.await_args.args[0]


# ------------------------------
# API key capture
# ------------------------------

def _pending(age_seconds=0, aware=True):
    now = datetime.datetime.now(datetime.timezone.utc)
    created_at = now - datetime.timedelta(seconds=age_seconds)
    if not aware:
        created_at = created_at.replace(tzinfo=None)
    return SimpleNamespace(state=commands.WAITING_FOR_API_KEY, created_at=created_at)


def _private_text(text, user_id=42):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        text=text,
        reply_text=AsyncMock(),
        delete=AsyncMock(),
    )


async def test_chatter_without_prompt_gets_hint_and_is_kept(fake_db):
    message = _private_text("hello there")
    await commands.receive_api_key_handler(None, message)

    message.delete.assert_not_awaited()
    assert "Send me an image" in message.reply_text.await_args.args[0]
    assert fake_db.saved == {}


async def test_commands_are_ignored(fake_db):
    message = _private_text("/unknown")
    await commands.receive_api_key_handler(None, message)

    message.delete.assert_not_awaited()
    message.reply_text.assert_not_awaited()


async def test_valid_key_after_prompt_is_saved_and_hidden(fake_db):
    fake_db.pending = _pending()
    message = _private_text(f"  {VALID_KEY}  ")
    await commands.receive_api_key_handler(None, message)

    message.delete.assert_awaited_once()
    assert fake_db.saved == {42: VALID_KEY}
    assert fake_db.pending is None
    assert "saved" in message.reply_text.await_args.args[0]


async def test_valid_key_without_prompt_is_saved(fake_db):
    message = _private_text(VALID_KEY)
    await commands.receive_api_key_handler(None, message)

    message.delete.assert_awaited_once()
    assert fake_db.saved == {42: VALID_KEY}


async def test_invalid_key_keeps_prompt_open(fake_db):
    fake_db.pending = _pending()
    message = _private_text("not-a-key")
    await commands.receive_api_key_handler(None, message)

    message.delete.assert_awaited_once()
    assert fake_db.saved == {}
    assert fake_db.pending is not None
    assert "not a valid" in message.reply_text.await_args.args[0]


async def test_expired_prompt_is_cleared(fake_db):
    fake_db.pending = _pending(age_seconds=commands.KEY_PROMPT_TIMEOUT + 5)
    message = _private_text("not-a-key")
    await commands.receive_api_key_handler(None, message)

    assert fake_db.pending is None
    assert "timed out" in message.reply_text.await_args.args[0]


def test_naive_timestamps_are_read_as_utc():
    assert commands._is_expired(_pending(age_seconds=10, aware=False)) is False
    assert commands._is_expired(_pending(age_seconds=10_000, aware=False)) is True


# ------------------------------
# imgbb upload client
# ------------------------------

def _ok(url=IMAGE_URL, delete_url=DELETE_URL):
    data = {"url": url, "delete_url": delete_url}
    return httpx.Response(200, json={"data": data, "success": True})


async def test_upload_retries_server_errors_and_resends_file(imgbb):
    imgbb.replies = [httpx.Response(502), _ok()]
    response = await commands._upload_to_imgbb(VALID_KEY, io.BytesIO(IMAGE_BYTES), "a.png")

    assert response.status_code == 200
    assert len(imgbb.requests) == 2
    for request in imgbb.requests:
        assert request.url.params["key"] == VALID_KEY
        assert IMAGE_BYTES in request.content  # the file object was rewound


async def test_upload_retries_network_errors(imgbb):
    imgbb.replies = [httpx.ConnectError("boom"), _ok()]
    response = await commands._upload_to_imgbb(VALID_KEY, IMAGE_BYTES)
    assert response.status_code == 200


async def test_upload_gives_up_after_all_attempts(imgbb):
    imgbb.replies = [httpx.Response(503)] * (commands.UPLOAD_RETRIES + 1)
    assert await commands._upload_to_imgbb(VALID_KEY, IMAGE_BYTES) is None
    assert len(imgbb.requests) == commands.UPLOAD_RETRIES + 1


async def test_upload_does_not_retry_client_errors(imgbb):
    imgbb.replies = [httpx.Response(400, json={"error": {"message": "Bad"}})]
    response = await commands._upload_to_imgbb(VALID_KEY, IMAGE_BYTES)
    assert response.status_code == 400
    assert len(imgbb.requests) == 1


async def test_upload_sends_expiration_only_when_set(imgbb):
    imgbb.replies = [_ok(), _ok()]
    await commands._upload_to_imgbb(VALID_KEY, IMAGE_BYTES, expiration=3600)
    await commands._upload_to_imgbb(VALID_KEY, IMAGE_BYTES)

    assert imgbb.requests[0].url.params["expiration"] == "3600"
    assert "expiration" not in imgbb.requests[1].url.params


def _sent_link(request):
    """Return the link sent as the plain "image" form field, or None."""
    field = b'name="image"\r\n\r\n'
    if field not in request.content or b"filename=" in request.content:
        return None
    return request.content.split(field, 1)[1].split(b"\r\n", 1)[0].decode()


async def test_upload_from_link_sends_the_link(imgbb):
    imgbb.replies = [_ok()]
    await commands._upload_to_imgbb(VALID_KEY, "https://example.com/cat.png")

    request = imgbb.requests[0]
    assert request.headers["content-type"].startswith("multipart/form-data")
    assert _sent_link(request) == "https://example.com/cat.png"


def test_imgbb_error_messages():
    bad_key = httpx.Response(400, json={"error": {"message": "Invalid API v1 key."}})
    assert "Update your key" in commands._imgbb_error(bad_key)
    assert commands._imgbb_error(httpx.Response(500, text="oops")) == "HTTP 500"


def test_result_text_escapes_user_content():
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=7, username=None, first_name="<b>Al</b>"),
        caption="cats & dogs",
    )
    text = commands._result_text(message, "https://i.ibb.co/x/y.png")
    assert "&lt;b&gt;Al&lt;/b&gt;" in text
    assert "cats &amp; dogs" in text
    assert "tg://user?id=7" in text
    assert "Delete" not in text


def test_album_text_lists_links_and_errors():
    sender = SimpleNamespace(id=7, username="bob", first_name="Bob")
    uploads = [
        commands._Upload(
            SimpleNamespace(from_user=sender, caption="a <trip>"),
            image_url="https://i.ibb.co/x/1.png",
            delete_url="https://ibb.co/x/del",
        ),
        commands._Upload(
            SimpleNamespace(from_user=sender, caption=None),
            error="⚠️ Image is too large (maximum size is 32 MB).",
        ),
    ]
    text = commands._album_text(uploads, show_delete_links=True, expiration=86400)

    assert text.startswith("⚠️ @bob · 1 of 2 images uploaded")
    assert '1. https://i.ibb.co/x/1.png · <a href="https://ibb.co/x/del">' in text
    assert "2. ⚠️ Image is too large" in text
    assert "Expires in 1 day" in text
    assert "a &lt;trip&gt;" in text
    assert "ibb.co/x/del" not in commands._album_text(uploads, show_delete_links=False)


async def _shown(text):
    """Return the text that Telegram shows, as Kurigram's HTML parser makes it."""
    return (await HTML(None).parse(text))["message"]


def _utf16(text):
    return len(text.encode("utf-16-le")) // 2


async def test_long_captions_are_shortened_to_fit_telegram():
    sender = SimpleNamespace(id=7, username="bob", first_name="Bob")
    # Premium users can write captions of 4096 characters
    single = commands._result_text(
        SimpleNamespace(from_user=sender, caption="a" * 4096), IMAGE_URL, DELETE_URL, 3600
    )
    shown = await _shown(single)
    assert _utf16(shown) == commands.MAX_MESSAGE_LENGTH
    assert IMAGE_URL in shown
    assert shown.endswith("a…")

    # Each album item can have its own caption; emoji count twice
    uploads = [
        commands._Upload(
            SimpleNamespace(from_user=sender, caption=f"{n} " + "😀" * 600),
            image_url=f"https://i.ibb.co/x/{n}.png",
            delete_url=DELETE_URL,
        )
        for n in range(10)
    ]
    shown = await _shown(commands._album_text(uploads, show_delete_links=True))
    assert _utf16(shown) <= commands.MAX_MESSAGE_LENGTH
    assert all(f"https://i.ibb.co/x/{n}.png" in shown for n in range(10))
    assert "😀…" in shown


# ------------------------------
# Upload handler end to end
# ------------------------------

def _photo_message(
    chat_type=enums.ChatType.PRIVATE, message_id=10, media_group_id=None, caption=None
):
    status = SimpleNamespace(edit_text=AsyncMock())
    return SimpleNamespace(
        id=message_id,
        from_user=SimpleNamespace(id=42, username="alice", first_name="Alice"),
        chat=SimpleNamespace(id=1, type=chat_type, title="Cat Lovers"),
        photo=SimpleNamespace(file_id=f"photo-{message_id}", file_size=len(IMAGE_BYTES)),
        document=None,
        caption=caption,
        media_group_id=media_group_id,
        reply=AsyncMock(return_value=status),
        delete=AsyncMock(),
    ), status


def _link_message(text="https://example.com/cat.png"):
    status = SimpleNamespace(edit_text=AsyncMock())
    return SimpleNamespace(
        id=20,
        from_user=SimpleNamespace(id=42, username="alice", first_name="Alice"),
        chat=SimpleNamespace(id=1, type=enums.ChatType.PRIVATE, title=None),
        text=text,
        caption=None,
        media_group_id=None,
        reply=AsyncMock(return_value=status),
        reply_text=AsyncMock(),
        delete=AsyncMock(),
    ), status


def _fake_client():
    return SimpleNamespace(
        me=SimpleNamespace(username="test_bot"),
        # A new buffer for each download, because each upload closes its buffer
        download_media=AsyncMock(side_effect=lambda *args, **kwargs: io.BytesIO(IMAGE_BYTES)),
        delete_messages=AsyncMock(),
        send_message=AsyncMock(),
    )


async def test_upload_runs_in_background_and_posts_link(imgbb, with_key, user_db):
    imgbb.replies = [_ok()]
    client = _fake_client()
    message, status = _photo_message()

    await commands.upload_image_handler(client, message)
    # The handler hands off the transfer, so its worker is free at once
    client.download_media.assert_not_awaited()

    await commands.wait_for_uploads(timeout=5)

    client.download_media.assert_awaited_once()
    final_text = status.edit_text.await_args.args[0]
    assert IMAGE_URL in final_text
    assert "@alice" in final_text
    # In a private chat the delete link is part of the reply
    assert f'href="{DELETE_URL}"' in final_text
    client.delete_messages.assert_awaited_once_with(1, [10])
    client.send_message.assert_not_awaited()
    assert user_db.history == [UploadRecord(IMAGE_URL, DELETE_URL, None)]


async def test_expiry_setting_reaches_imgbb_and_history(imgbb, with_key, user_db):
    user_db.expiration = 3600
    imgbb.replies = [_ok()]
    message, status = _photo_message()

    await commands.upload_image_handler(_fake_client(), message)
    await commands.wait_for_uploads(timeout=5)

    assert imgbb.requests[0].url.params["expiration"] == "3600"
    assert "Expires in 1 hour" in status.edit_text.await_args.args[0]
    remaining = user_db.history[0].expires_at - datetime.datetime.now(datetime.timezone.utc)
    assert 3500 < remaining.total_seconds() <= 3600


async def test_group_upload_sends_delete_link_in_private(imgbb, with_key):
    imgbb.replies = [_ok()]
    client = _fake_client()
    message, status = _photo_message(chat_type=enums.ChatType.SUPERGROUP)

    await commands.upload_image_handler(client, message)
    await commands.wait_for_uploads(timeout=5)

    final_text = status.edit_text.await_args.args[0]
    assert IMAGE_URL in final_text
    assert DELETE_URL not in final_text
    private = client.send_message.await_args
    assert private.args[0] == 42
    assert DELETE_URL in private.args[1]
    assert "Cat Lovers" in private.args[1]
    assert private.kwargs["disable_notification"] is True


async def test_upload_without_key_in_group_is_silent(monkeypatch):
    monkeypatch.setattr(commands, "get_user_api_key", AsyncMock(return_value=None))
    message, _ = _photo_message(chat_type=enums.ChatType.SUPERGROUP)

    await commands.upload_image_handler(_fake_client(), message)
    message.reply.assert_not_awaited()


async def test_upload_without_key_in_private_offers_add_key(monkeypatch):
    monkeypatch.setattr(commands, "get_user_api_key", AsyncMock(return_value=None))
    message, _ = _photo_message()

    await commands.upload_image_handler(_fake_client(), message)
    assert "API key not found" in message.reply.await_args.args[0]
    assert "reply_markup" in message.reply.await_args.kwargs


async def test_imgbb_rejection_is_reported(imgbb, with_key, user_db):
    imgbb.replies = [httpx.Response(400, json={"error": {"message": "Invalid API v1 key."}})]
    client = _fake_client()
    message, status = _photo_message()

    await commands.upload_image_handler(client, message)
    await commands.wait_for_uploads(timeout=5)

    final_text = status.edit_text.await_args.args[0]
    assert "Invalid API v1 key." in final_text
    client.delete_messages.assert_not_awaited()
    assert user_db.history == []


async def test_original_is_kept_when_the_link_cannot_be_posted(imgbb, with_key):
    imgbb.replies = [_ok()]
    client = _fake_client()
    message, status = _photo_message()
    status.edit_text.side_effect = errors.MessageIdInvalid()

    await commands.upload_image_handler(client, message)
    await commands.wait_for_uploads(timeout=5)

    client.delete_messages.assert_not_awaited()


async def test_album_gets_one_reply(monkeypatch, imgbb, with_key, user_db):
    monkeypatch.setattr(commands, "ALBUM_WAIT", 0.01)
    imgbb.replies = [
        _ok("https://i.ibb.co/a/1.jpg", "https://ibb.co/a/d1"),
        _ok("https://i.ibb.co/b/2.jpg", "https://ibb.co/b/d2"),
    ]
    client = _fake_client()
    first, status = _photo_message(message_id=10, media_group_id=777, caption="holiday")
    second, _ = _photo_message(message_id=11, media_group_id=777)

    # Album items can arrive in any order
    await commands.upload_image_handler(client, second)
    await commands.upload_image_handler(client, first)
    await commands.wait_for_uploads(timeout=5)

    first.reply.assert_awaited_once()
    second.reply.assert_not_awaited()
    final_text = status.edit_text.await_args.args[0]
    assert "2 of 2 images uploaded" in final_text
    assert "https://i.ibb.co/a/1.jpg" in final_text
    assert "https://i.ibb.co/b/2.jpg" in final_text
    assert "holiday" in final_text
    chat_id, message_ids = client.delete_messages.await_args.args
    assert chat_id == 1
    assert sorted(message_ids) == [10, 11]
    assert len(user_db.history) == 2
    assert commands._albums == {}


async def test_album_waits_for_late_items(monkeypatch, imgbb, with_key):
    monkeypatch.setattr(commands, "ALBUM_WAIT", 0.2)
    imgbb.replies = [_ok() for _ in range(3)]
    client = _fake_client()
    items = [_photo_message(message_id=n, media_group_id=4) for n in (1, 2, 3)]

    # Each item comes before the wait ends, so the album keeps growing
    for message, _ in items:
        await commands.upload_image_handler(client, message)
        await asyncio.sleep(0.1)
    await commands.wait_for_uploads(timeout=5)

    first, status = items[0]
    first.reply.assert_awaited_once()
    for message, _ in items[1:]:
        message.reply.assert_not_awaited()
    assert "3 of 3 images uploaded" in status.edit_text.await_args.args[0]


def _file_id(dc_id, media_id):
    """Return a real Telegram file id for a document stored in data center `dc_id`."""
    return FileId(
        file_type=FileType.DOCUMENT, dc_id=dc_id, media_id=media_id, access_hash=1, file_reference=b""
    ).encode()


async def test_first_download_from_a_data_center_runs_alone(monkeypatch, imgbb, with_key):
    monkeypatch.setattr(commands, "ALBUM_WAIT", 0.01)
    monkeypatch.setattr(commands, "_ready_dcs", set())
    monkeypatch.setattr(commands, "_dc_locks", {})
    imgbb.replies = [_ok() for _ in range(6)]
    connected, connecting = set(), set()
    active = SimpleNamespace(now=0, peak=0)

    async def download_media(media, in_memory=False):
        dc_id = FileId.decode(media.file_id).dc_id
        if dc_id not in connected:
            # As in Kurigram: a download while the first one connects fails
            if dc_id in connecting:
                raise errors.AuthBytesInvalid()
            connecting.add(dc_id)
            await asyncio.sleep(0.05)
            connecting.discard(dc_id)
            connected.add(dc_id)
        else:
            active.now += 1
            active.peak = max(active.peak, active.now)
            await asyncio.sleep(0.05)
            active.now -= 1
        return io.BytesIO(IMAGE_BYTES)

    client = _fake_client()
    client.download_media = download_media
    items = [_photo_message(message_id=n, media_group_id=8) for n in range(1, 7)]
    for (message, _), dc_id in zip(items, (4, 4, 4, 5, 5, 5), strict=True):
        message.photo.file_id = _file_id(dc_id, message.id)
    for message, _ in items:
        await commands.upload_image_handler(client, message)
    await commands.wait_for_uploads(timeout=5)

    assert "6 of 6 images uploaded" in items[0][1].edit_text.await_args.args[0]
    assert commands._ready_dcs == {4, 5}
    # After the first download, the others from that data center run in parallel
    assert active.peak >= 2


async def test_album_in_group_without_key_is_silent(monkeypatch):
    monkeypatch.setattr(commands, "ALBUM_WAIT", 0.01)
    monkeypatch.setattr(commands, "get_user_api_key", AsyncMock(return_value=None))
    client = _fake_client()
    messages = [
        _photo_message(chat_type=enums.ChatType.SUPERGROUP, message_id=i, media_group_id=5)[0]
        for i in (1, 2)
    ]

    for message in messages:
        await commands.upload_image_handler(client, message)
    await commands.wait_for_uploads(timeout=5)

    for message in messages:
        message.reply.assert_not_awaited()
    client.download_media.assert_not_awaited()


async def test_image_link_in_private_is_uploaded(fake_db, imgbb, with_key, user_db):
    imgbb.replies = [_ok()]
    client = _fake_client()
    message, status = _link_message()

    await commands.receive_api_key_handler(client, message)
    await commands.wait_for_uploads(timeout=5)

    assert _sent_link(imgbb.requests[0]) == "https://example.com/cat.png"
    client.download_media.assert_not_awaited()
    assert IMAGE_URL in status.edit_text.await_args.args[0]
    client.delete_messages.assert_awaited_once_with(1, [20])
    assert user_db.history[0].image_url == IMAGE_URL


# ------------------------------
# Upload history
# ------------------------------

async def test_history_lists_uploads_newest_first(user_db):
    created = datetime.datetime(2026, 10, 20, 9, 30, tzinfo=datetime.timezone.utc)
    user_db.history = [
        UploadRecord("https://i.ibb.co/old/1.png", None, None, created),
        UploadRecord(
            "https://i.ibb.co/new/2.png", DELETE_URL, created + datetime.timedelta(days=1), created
        ),
    ]
    message = _command("history")
    await commands.history_handler(None, message)

    reply = message.reply_text.await_args
    text = reply.args[0]
    assert text.index("new/2.png") < text.index("old/1.png")
    assert f'href="{DELETE_URL}"' in text
    assert "until 2026-10-21 09:30 UTC" in text
    assert reply.kwargs["parse_mode"] == enums.ParseMode.HTML


async def test_history_when_empty(user_db):
    message = _command("history")
    await commands.history_handler(None, message)
    assert "no saved uploads" in message.reply_text.await_args.args[0]


async def test_clear_history_confirm(user_db):
    user_db.history = [UploadRecord("https://i.ibb.co/a/1.png")] * 3
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock()),
    )
    await commands.clear_history_confirm_callback(None, callback)

    assert user_db.history == []
    assert "Removed 3 uploads" in callback.message.edit_text.await_args.args[0]
