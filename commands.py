"""Telegram bot commands and handlers for imgbb uploads and key management."""
import asyncio
import datetime
import html
import logging
import re
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Dict, List, Optional, Set, Tuple, Union
from urllib.parse import urlsplit

import httpx
from pyrogram import Client, enums, errors, filters
from pyrogram.file_id import FileId
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)

from config import settings
from crypto import is_valid_imgbb_key
from dbmodels import (
    PendingState,
    UploadRecord,
    add_upload_history,
    clear_pending_state,
    clear_upload_history,
    delete_user_api_key,
    get_pending_state,
    get_upload_history,
    get_user_api_key,
    get_user_expiration,
    get_user_key_info,
    save_user_api_key,
    set_pending_state,
    set_user_expiration,
)
from http_client import get_http_client

logger = logging.getLogger(__name__)

IMGBB_UPLOAD_URL = "https://api.imgbb.com/1/upload"

# imgbb hard upload limit: 32 MB
MAX_FILE_SIZE = 32 * 1024 * 1024

# Bounds concurrent transfers. A slot is held from download through upload,
# so at most MAX_CONCURRENT_DOWNLOADS images are held in RAM at once.
_transfer_semaphore = asyncio.Semaphore(settings.MAX_CONCURRENT_DOWNLOADS)

# Strong references to running upload jobs so they are not garbage collected
_upload_tasks: Set[asyncio.Task] = set()

# Kurigram connects to a file's data center on the first download from it, with
# no lock: parallel first downloads each export the bot's authorization, which
# fails with AUTH_BYTES_INVALID or a FloodWait of minutes. So the first download
# from each data center runs alone; the ones after it run in parallel.
_ready_dcs: Set[int] = set()
_dc_locks: Dict[int, asyncio.Lock] = {}

# Retry configuration for imgbb API uploads
UPLOAD_RETRIES = 2
RETRY_BACKOFF = 1.5

# Formats supported by imgbb
ALLOWED_FORMATS = {
    "jpeg", "png", "gif", "bmp", "tiff", "tif",
    "raw", "heif", "heic", "webp", "jpg", "svg",
    "avif", "ico",
}

# MIME subtypes whose name differs from the file extension
_MIME_ALIASES = {
    "ms-bmp": "bmp",
    "icon": "ico",
    "vnd.microsoft.icon": "ico",
}

# How long a user has to send their key after tapping "Add Key"
KEY_PROMPT_TIMEOUT = 300
WAITING_FOR_API_KEY = "waiting_for_api_key"

# Expiry range that imgbb accepts, in seconds (1 minute to 180 days)
MIN_EXPIRATION = 60
MAX_EXPIRATION = 180 * 24 * 60 * 60

# Choices on the /expire keyboard; 0 means uploads never expire
EXPIRE_PRESETS = [
    ("Never", 0),
    ("1 hour", 60 * 60),
    ("1 day", 24 * 60 * 60),
    ("7 days", 7 * 24 * 60 * 60),
    ("30 days", 30 * 24 * 60 * 60),
    ("180 days", MAX_EXPIRATION),
]

# Durations such as "90", "30m", "12h", "7d" or "2w"
_DURATION_PATTERN = re.compile(r"^(\d+)\s*([smhdw]?)$")
_DURATION_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

# Telegram sends the items of an album as separate messages; wait this many
# seconds after the latest item, then upload all of them together
ALBUM_WAIT = 1.5

# Uploads listed by /history
HISTORY_SHOWN = 10

# Longest image link accepted in a text message
MAX_URL_LENGTH = 2048

# Telegram refuses messages longer than this, counted in UTF-16 code units
MAX_MESSAGE_LENGTH = 4096


@dataclass
class _Upload:
    """One image to upload: Telegram media to download, or a link imgbb fetches."""

    message: Message
    media: Any = None
    file_name: str = "image.jpg"
    source_url: Optional[str] = None
    # Set when the upload ends
    image_url: Optional[str] = None
    delete_url: Optional[str] = None
    error: Optional[str] = None


@dataclass
class _UploadJob:
    """Uploads from one message or one album that share a status message."""

    client: Client
    uploads: List[_Upload]
    # Kept out of repr(), which logs and error reports can show
    api_key: str = field(repr=False)
    expiration: Optional[int]
    status: Message
    queued: bool


@dataclass
class _Album:
    """Items of an album that are still arriving."""

    uploads: List[_Upload] = field(default_factory=list)
    last_added: float = 0.0


# Albums being collected, by (chat id, media group id)
_albums: Dict[Tuple[int, int], _Album] = {}


async def get_bot_username(client: Client) -> str:
    """Return the bot username (client.me is filled in by Client.start)."""
    me = client.me or await client.get_me()
    return me.username or ""


def _add_key_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    addkeyurl = f"https://t.me/{bot_username}?start=addkeyforuser"
    return InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add Key", url=addkeyurl)]])


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")]]
    )


def _delete_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Yes, Delete", callback_data="deletekey_confirm"),
                InlineKeyboardButton("❌ Cancel", callback_data="deletekey_cancel"),
            ]
        ]
    )


def _expire_keyboard(current: Optional[int]) -> InlineKeyboardMarkup:
    """Preset expiry buttons, with the current choice marked."""
    buttons = [
        InlineKeyboardButton(
            f"✅ {label}" if seconds == (current or 0) else label,
            callback_data=f"expire:{seconds}",
        )
        for label, seconds in EXPIRE_PRESETS
    ]
    return InlineKeyboardMarkup([buttons[:3], buttons[3:]])


def _history_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🗑️ Clear History", callback_data="historyclear")]]
    )


def _clear_history_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Yes, Clear", callback_data="historyclear_confirm"),
                InlineKeyboardButton("❌ Cancel", callback_data="historyclear_cancel"),
            ]
        ]
    )


def _is_expired(pending: PendingState) -> bool:
    """Return True if a pending key prompt is older than KEY_PROMPT_TIMEOUT."""
    created_at = pending.created_at
    if created_at is None:
        return True
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=datetime.timezone.utc)
    age = datetime.datetime.now(datetime.timezone.utc) - created_at
    return age.total_seconds() > KEY_PROMPT_TIMEOUT


def _image_extension(document) -> Optional[str]:
    """Return the imgbb-compatible extension of a document, or None."""
    mime = (document.mime_type or "").lower()
    file_name = document.file_name or ""
    ext_from_name = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""

    if mime.startswith("image/"):
        ext = mime.split("/")[-1].split("+")[0].removeprefix("x-")
        ext = _MIME_ALIASES.get(ext, ext)
        if ext in ALLOWED_FORMATS:
            return ext

    if ext_from_name in ALLOWED_FORMATS:
        return ext_from_name
    return None


def _image_url(text: str) -> Optional[str]:
    """Return `text` if it is one http(s) link, which imgbb can fetch, else None."""
    if not text or len(text) > MAX_URL_LENGTH or any(char.isspace() for char in text):
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    return text


def _plural(count: int, word: str) -> str:
    """Return "1 image", "2 images" and so on."""
    return f"{count} {word}" + ("" if count == 1 else "s")


def _parse_expiration(text: str) -> Optional[int]:
    """Parse "off", "90", "30m", "12h", "7d" or "2w" into seconds (None means never).

    Raises ValueError when the text is not a time that imgbb accepts.
    """
    value = text.strip().lower()
    if value in ("off", "never", "0"):
        return None
    match = _DURATION_PATTERN.match(value)
    if not match:
        raise ValueError("Send a time such as 30m, 12h, 7d or 2w, or off.")
    seconds = int(match.group(1)) * _DURATION_UNITS[match.group(2)]
    if not MIN_EXPIRATION <= seconds <= MAX_EXPIRATION:
        raise ValueError("The time must be from 1 minute to 180 days.")
    return seconds


def _format_duration(seconds: int) -> str:
    """Return a duration such as "1 hour" or "90 minutes"."""
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds % size == 0:
            return _plural(seconds // size, unit)
    return _plural(seconds, "second")


def _expiration_text(seconds: Optional[int]) -> str:
    """Describe what imgbb does with new uploads."""
    if not seconds:
        return "New uploads never expire."
    return f"New uploads are deleted after {_format_duration(seconds)}."


def _expire_menu_text(seconds: Optional[int]) -> str:
    return (
        f"⏱ {_expiration_text(seconds)}\n\n"
        "Tap a time below, or send one such as /expire 12h "
        "(m, h, d or w, from 1 minute to 180 days). "
        "Images that you uploaded before do not change."
    )


def _format_time(moment: datetime.datetime) -> str:
    """Return a database time as UTC text."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


async def _set_status(status: Message, text: str, final: bool = False, **kwargs) -> bool:
    """Edit the status message without raising. Returns True if the text is shown.

    A final result waits out one FloodWait so the link still reaches the user;
    progress text is skipped instead, because it must never stall an upload.
    """
    try:
        await status.edit_text(text, **kwargs)
        return True
    except errors.FloodWait as exc:
        if not final:
            return False
        await asyncio.sleep(exc.value)
        try:
            await status.edit_text(text, **kwargs)
            return True
        except errors.RPCError as retry_exc:
            logger.warning("Could not post upload result: %s", retry_exc)
    except errors.MessageNotModified:
        return True
    except errors.RPCError as exc:
        logger.warning("Could not update status message: %s", exc)
    return False


async def _upload_to_imgbb(
    api_key: str,
    image: Union[bytes, BinaryIO, str],
    file_name: str = "image.jpg",
    expiration: Optional[int] = None,
) -> Optional[httpx.Response]:
    """Upload an image to imgbb via multipart form data with retries.

    `image` may be bytes, a file object, or an image link (str) that imgbb
    downloads itself. httpx rewinds file objects before each attempt, so
    retries send the full image again. With `expiration` (seconds), imgbb
    deletes the image after that time.
    """
    params = {"key": api_key.strip()}
    if expiration:
        params["expiration"] = str(expiration)
    # A link is a plain form field: without a file name it is not a file upload
    image_part = (None, image) if isinstance(image, str) else (file_name, image)

    backoff = RETRY_BACKOFF
    http_client = get_http_client()

    for attempt in range(UPLOAD_RETRIES + 1):
        try:
            response = await http_client.post(
                IMGBB_UPLOAD_URL,
                params=params,
                files={"image": image_part},
            )
            if response.status_code < 500:
                return response
            logger.warning(
                "imgbb returned HTTP %s (attempt %s/%s)",
                response.status_code,
                attempt + 1,
                UPLOAD_RETRIES + 1,
            )
        except httpx.RequestError as exc:
            logger.warning(
                "imgbb request error (attempt %s/%s): %s",
                attempt + 1,
                UPLOAD_RETRIES + 1,
                exc,
            )

        if attempt < UPLOAD_RETRIES:
            await asyncio.sleep(backoff)
            backoff *= 2

    logger.error("imgbb upload failed after %s attempts", UPLOAD_RETRIES + 1)
    return None


def _imgbb_error(response: httpx.Response) -> str:
    """Return a readable error message from a failed imgbb response."""
    try:
        err_msg = response.json().get("error", {}).get("message")
    except (ValueError, AttributeError):
        err_msg = None
    err_msg = err_msg or f"HTTP {response.status_code}"
    if response.status_code in (400, 401, 403) and "key" in err_msg.lower():
        err_msg += "\nUpdate your key with /start → Add Key."
    return err_msg


def _sender(message: Message) -> str:
    """Return HTML that names the sender of a message."""
    user = message.from_user
    if user.username:
        return f"@{user.username}"
    safe_name = html.escape(user.first_name or "User")
    return f'<a href="tg://user?id={user.id}">{safe_name}</a>'


def _delete_link(delete_url: str) -> str:
    return f'🗑 <a href="{html.escape(delete_url)}">Delete from imgbb</a>'


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _shorten(text: str, units: int) -> str:
    """Return the longest start of `text` that is at most `units` UTF-16 code units."""
    total = 0
    for index, char in enumerate(text):
        total += 2 if ord(char) > 0xFFFF else 1
        if total > units:
            return text[:index]
    return text


def _with_captions(text: str, captions: List[str]) -> str:
    """Add captions to an HTML message, shortened so that Telegram still accepts it.

    Without this, a long caption (up to 4096 characters for Premium users) makes
    the result too long to post, and the links are never shown.
    """
    for caption in captions:
        # The text that Telegram counts: no tags, and entities read as characters
        shown = html.unescape(re.sub(r"<[^>]*>", "", text))
        # Keep one unit for the line break before the caption
        room = MAX_MESSAGE_LENGTH - _utf16_length(shown) - 1
        if room < 2:
            break
        if _utf16_length(caption) > room:
            caption = _shorten(caption, room - 1) + "…"
        text += f"\n{html.escape(caption)}"
    return text


def _result_text(
    message: Message,
    image_url: str,
    delete_url: Optional[str] = None,
    expiration: Optional[int] = None,
) -> str:
    """Build the HTML reply that credits the sender and shows the link."""
    text = f"✅ {_sender(message)}\n{html.escape(image_url)}"
    if delete_url:
        text += f"\n{_delete_link(delete_url)}"
    if expiration:
        text += f"\n⏱ Expires in {_format_duration(expiration)}"
    return _with_captions(text, [message.caption] if message.caption else [])


def _album_text(
    uploads: List[_Upload],
    show_delete_links: bool,
    expiration: Optional[int] = None,
) -> str:
    """Build one HTML reply with the link or the error of each album item."""
    done = sum(1 for upload in uploads if upload.image_url)
    mark = "✅" if done == len(uploads) else ("⚠️" if done else "❌")
    lines = [
        f"{mark} {_sender(uploads[0].message)} · "
        f"{done} of {_plural(len(uploads), 'image')} uploaded"
    ]
    for number, upload in enumerate(uploads, 1):
        if upload.image_url:
            line = f"{number}. {html.escape(upload.image_url)}"
            if show_delete_links and upload.delete_url:
                line += f' · <a href="{html.escape(upload.delete_url)}">🗑 delete</a>'
        else:
            line = f"{number}. {html.escape(upload.error or '❌ Upload failed.')}"
        lines.append(line)

    if expiration and done:
        lines.append(f"⏱ Expires in {_format_duration(expiration)}")

    captions = []
    for upload in uploads:
        caption = upload.message.caption
        if caption and caption not in captions:
            captions.append(caption)
    return _with_captions("\n".join(lines), captions)


def _history_text(records: List[UploadRecord]) -> str:
    """Build the HTML /history list, newest upload first."""
    lines = ["🗂 <b>Your recent uploads</b> (newest first)"]
    for number, record in enumerate(records, 1):
        details = [_format_time(record.created_at)] if record.created_at else []
        if record.delete_url:
            details.append(f'<a href="{html.escape(record.delete_url)}">🗑 delete</a>')
        if record.expires_at:
            details.append(f"⏱ until {_format_time(record.expires_at)}")
        lines.append(f"\n{number}. {html.escape(record.image_url)}")
        if details:
            lines.append(" · ".join(details))
    return "\n".join(lines)


# ==============================
# HANDLER IMPLEMENTATIONS
# ==============================

async def start_handler(client: Client, message: Message) -> None:
    """Handle /start command in private chats."""
    payload = message.command[1] if len(message.command) > 1 else None

    if payload == "addkeyforuser":
        await add_key_conversation(client, message)
        return

    bot_username = await get_bot_username(client)
    addkeyurl = f"https://t.me/{bot_username}?start=addkeyforuser"

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Add Key", url=addkeyurl)],
            [InlineKeyboardButton("🗑️ Delete Key", callback_data="deletekeyforuser")],
        ]
    )

    await message.reply_text(
        "👋 Welcome! Click below to manage your imgbb API key.\n\n"
        "Get a free key from: https://api.imgbb.com",
        reply_markup=keyboard,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def add_key_conversation(client: Client, message: Message) -> None:
    """Ask the user for their API key.

    This only records the pending state and returns. receive_api_key_handler
    picks up the next text message, so no handler worker is held while the
    user types (Pyrogram runs a small fixed pool of workers).
    """
    user_id = message.from_user.id

    try:
        await set_pending_state(user_id, WAITING_FOR_API_KEY)
    except Exception:
        logger.exception("Failed to save pending state in DB for user %s", user_id)
        await message.reply_text(
            "⚠️ Could not start key setup. Please try again later."
        )
        return

    await message.reply_text(
        f"🔑 Please send me your imgbb API key within {KEY_PROMPT_TIMEOUT // 60} minutes.\n\n"
        "(You can find it at https://api.imgbb.com)",
        reply_markup=_cancel_keyboard(),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def cancel_command_handler(client: Client, message: Message) -> None:
    """Handle /cancel command."""
    user_id = message.from_user.id
    try:
        cancelled = await clear_pending_state(user_id)
    except Exception:
        logger.exception("Failed to clear DB pending state for user %s", user_id)
        cancelled = True

    await message.reply_text(
        "❌ Operation cancelled." if cancelled else "Nothing to cancel."
    )


async def cancel_callback_handler(client: Client, callback_query: CallbackQuery) -> None:
    """Handle cancel button callback."""
    user_id = callback_query.from_user.id
    try:
        await clear_pending_state(user_id)
    except Exception:
        logger.exception("Failed to clear pending state on callback for user %s", user_id)

    await callback_query.answer("Cancelled.")
    try:
        await callback_query.message.edit_text("❌ Operation cancelled.")
    except errors.RPCError:
        pass


async def help_handler(client: Client, message: Message) -> None:
    """Handle /help command."""
    await message.reply_text(
        "🖼 **imgbb Upload Bot**\n\n"
        "1. Get a free API key from https://api.imgbb.com\n"
        "2. Register it with /start → **Add Key**\n"
        "3. Send any image (photo or document up to 32 MB) to upload it. "
        "An album gets one reply with all its links, and in this chat you can "
        "also send an image link.\n\n"
        "**Delete Links and Expiry**\n"
        "Each upload comes with a delete link; for group uploads, the bot sends "
        "it to you here. Use /expire to make imgbb delete new uploads after a "
        "set time, and /history to find your recent links again.\n\n"
        "**Group Support**\n"
        "Add this bot to any group as an admin. Any member who registered their "
        "key in private can share images in the group and get instant upload links.\n\n"
        "**Available Commands**\n"
        "/start — Manage your API key\n"
        "/mystatus — Check your saved key status\n"
        "/expire — Set when new uploads are deleted\n"
        "/history — Show your recent uploads and delete links\n"
        "/deletekey — Delete your saved API key\n"
        "/cancel — Cancel the active operation\n"
        "/help — Show this help message",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def mystatus_handler(client: Client, message: Message) -> None:
    """Handle /mystatus command."""
    user_id = message.from_user.id
    try:
        info = await get_user_key_info(user_id)
    except Exception:
        logger.exception("mystatus error for user %s", user_id)
        await message.reply_text("⚠️ Could not check status. Please try again later.")
        return

    if info is None:
        await message.reply_text(
            "❌ No API key set. Use /start → **Add Key** to add one."
        )
        return

    api_key, creation_date = info
    if not api_key:
        await message.reply_text(
            "⚠️ Your saved key could not be read. Please add it again with "
            "/start → **Add Key**."
        )
        return

    added = _format_time(creation_date) if creation_date else "unknown"
    await message.reply_text(
        f"✅ Your API key is saved.\n🔑 Key: ••••{api_key[-4:]}\n📅 Added: {added}"
    )


async def deletekey_command_handler(client: Client, message: Message) -> None:
    """Handle /deletekey command."""
    await message.reply_text(
        "Are you sure you want to delete your saved API key?",
        reply_markup=_delete_confirm_keyboard(),
    )


async def deletekey_prompt_callback(client: Client, callback_query: CallbackQuery) -> None:
    """Prompt confirmation on delete key button tap."""
    await callback_query.message.reply(
        "Are you sure you want to delete your saved API key?",
        reply_markup=_delete_confirm_keyboard(),
    )
    await callback_query.answer()


async def deletekey_cancel_callback(client: Client, callback_query: CallbackQuery) -> None:
    """Handle cancellation of key deletion."""
    await callback_query.answer("Cancelled.")
    try:
        await callback_query.message.edit_text("Deletion cancelled. Your key is safe.")
    except errors.RPCError:
        pass


async def deletekey_confirm_callback(client: Client, callback_query: CallbackQuery) -> None:
    """Handle confirmation of key deletion."""
    user_id = callback_query.from_user.id
    try:
        deleted = await delete_user_api_key(user_id)
    except Exception:
        logger.exception("Error deleting key for user %s", user_id)
        await callback_query.answer("Failed to delete key. Please try again.", show_alert=True)
        return

    await callback_query.answer("Deleted." if deleted else "Nothing to delete.")
    try:
        if deleted:
            await callback_query.message.edit_text("✅ API key deleted successfully.")
        else:
            await callback_query.message.edit_text("❌ No API key was found to delete.")
    except errors.RPCError:
        pass


async def expire_handler(client: Client, message: Message) -> None:
    """Handle /expire: show or change when imgbb deletes new uploads."""
    user_id = message.from_user.id
    value = " ".join(message.command[1:])

    if value:
        try:
            seconds = _parse_expiration(value)
        except ValueError as exc:
            await message.reply_text(f"⚠️ {exc}")
            return
        try:
            await set_user_expiration(user_id, seconds)
        except Exception:
            logger.exception("Failed to save expiration for user %s", user_id)
            await message.reply_text("⚠️ Could not save the setting. Please try again later.")
            return
        await message.reply_text(f"✅ {_expiration_text(seconds)}")
        return

    try:
        current = await get_user_expiration(user_id)
    except Exception:
        logger.exception("Failed to read expiration for user %s", user_id)
        await message.reply_text("⚠️ Could not read your settings. Please try again later.")
        return

    await message.reply_text(_expire_menu_text(current), reply_markup=_expire_keyboard(current))


async def expire_callback(client: Client, callback_query: CallbackQuery) -> None:
    """Save the expiry chosen with an /expire button."""
    user_id = callback_query.from_user.id
    seconds = int(callback_query.data.split(":", 1)[1]) or None
    if seconds is not None and not MIN_EXPIRATION <= seconds <= MAX_EXPIRATION:
        await callback_query.answer("This choice is not available.", show_alert=True)
        return

    try:
        await set_user_expiration(user_id, seconds)
    except Exception:
        logger.exception("Failed to save expiration for user %s", user_id)
        await callback_query.answer("Could not save. Please try again.", show_alert=True)
        return

    await callback_query.answer("Saved.")
    try:
        await callback_query.message.edit_text(
            _expire_menu_text(seconds), reply_markup=_expire_keyboard(seconds)
        )
    except errors.RPCError:
        pass


async def history_handler(client: Client, message: Message) -> None:
    """Handle /history: list recent uploads with their delete links."""
    user_id = message.from_user.id
    try:
        records = await get_upload_history(user_id, limit=HISTORY_SHOWN)
    except Exception:
        logger.exception("history error for user %s", user_id)
        await message.reply_text("⚠️ Could not read your history. Please try again later.")
        return

    if not records:
        await message.reply_text(
            "🗂 You have no saved uploads yet. Send me an image to upload it."
        )
        return

    await message.reply_text(
        _history_text(records),
        parse_mode=enums.ParseMode.HTML,
        reply_markup=_history_keyboard(),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def clear_history_prompt_callback(client: Client, callback_query: CallbackQuery) -> None:
    """Ask for confirmation on clear history button tap."""
    await callback_query.answer(
        "Clear your history? The saved delete links are removed too; "
        "your images stay on imgbb.",
        show_alert=True,
    )
    try:
        await callback_query.message.edit_reply_markup(_clear_history_confirm_keyboard())
    except errors.RPCError:
        pass


async def clear_history_cancel_callback(client: Client, callback_query: CallbackQuery) -> None:
    """Handle cancellation of clearing the history."""
    await callback_query.answer("Cancelled.")
    try:
        await callback_query.message.edit_reply_markup(_history_keyboard())
    except errors.RPCError:
        pass


async def clear_history_confirm_callback(client: Client, callback_query: CallbackQuery) -> None:
    """Handle confirmation of clearing the history."""
    user_id = callback_query.from_user.id
    try:
        removed = await clear_upload_history(user_id)
    except Exception:
        logger.exception("Error clearing history for user %s", user_id)
        await callback_query.answer("Failed to clear history. Please try again.", show_alert=True)
        return

    await callback_query.answer("Cleared.")
    try:
        # Replacing the list also removes the delete links from the chat
        await callback_query.message.edit_text(
            f"✅ Removed {_plural(removed, 'upload')} from your history. "
            "Your images stay on imgbb."
        )
    except errors.RPCError:
        pass


async def receive_api_key_handler(client: Client, message: Message) -> None:
    """Handle private text: capture API keys, upload image links, or show a hint."""
    user_id = message.from_user.id
    text = (message.text or "").strip()

    if text.startswith("/"):
        return

    try:
        pending = await get_pending_state(user_id)
    except Exception:
        # Go on without it: a key must still leave the chat, and the user gets an answer
        logger.exception("Could not read pending state for user %s", user_id)
        pending = None

    looks_like_key = is_valid_imgbb_key(text)
    if pending is None and not looks_like_key:
        image_url = _image_url(text)
        if image_url:
            await _start_uploads(client, [_Upload(message, source_url=image_url)])
            return
        await message.reply_text(
            "📸 Send me an image, an album or an image link to upload it to imgbb.\n"
            "Use /start to manage your API key or /help for more."
        )
        return

    # The message may hold an API key: remove it from the chat
    try:
        await message.delete()
    except errors.RPCError:
        pass

    try:
        if not looks_like_key:
            if _is_expired(pending):
                await clear_pending_state(user_id)
                await message.reply_text("⏰ Key setup timed out. Use /start when you are ready.")
            else:
                await message.reply_text(
                    "❌ That is not a valid imgbb API key (a 32-character code).\n"
                    "Send it again, or use /cancel to stop.",
                    reply_markup=_cancel_keyboard(),
                )
            return

        await save_user_api_key(user_id, text)
        if pending is not None:
            await clear_pending_state(user_id)
        await message.reply_text("✅ Your imgbb API key has been saved securely!")

    except Exception:
        logger.exception("Error saving API key for user %s", user_id)
        await message.reply_text(
            "⚠️ An error occurred while saving your key. Please try again later."
        )


async def upload_image_handler(client: Client, message: Message) -> None:
    """Check an incoming photo or document and start a background upload."""
    if not message.from_user:
        return

    upload = _media_upload(message)
    if upload is None:
        return

    # Album items arrive one by one; they are uploaded together a moment later
    if message.media_group_id:
        _add_to_album(client, upload)
        return

    if upload.error:
        # Only private chats get an answer about files that are not images
        if message.chat.type == enums.ChatType.PRIVATE:
            try:
                await message.reply(upload.error)
            except errors.RPCError as exc:
                logger.warning("Telegram RPC error before upload: %s", exc)
        return

    await _start_uploads(client, [upload])


# ==============================
# UPLOAD PIPELINE
# ==============================

def _spawn(coro) -> None:
    """Run `coro` as a tracked background task, so shutdown can wait for it."""
    task = asyncio.create_task(coro)
    _upload_tasks.add(task)
    task.add_done_callback(_upload_tasks.discard)


async def _download(client: Client, media: Any) -> Any:
    """Download Telegram media into memory, alone if it is the first from its data center."""
    try:
        dc_id = FileId.decode(media.file_id).dc_id
    except Exception:
        dc_id = None

    if dc_id is not None and dc_id not in _ready_dcs:
        async with _dc_locks.setdefault(dc_id, asyncio.Lock()):
            if dc_id not in _ready_dcs:
                buffer = await asyncio.wait_for(
                    client.download_media(media, in_memory=True),
                    timeout=settings.DOWNLOAD_TIMEOUT,
                )
                if buffer:
                    _ready_dcs.add(dc_id)
                return buffer

    return await asyncio.wait_for(
        client.download_media(media, in_memory=True), timeout=settings.DOWNLOAD_TIMEOUT
    )


def _media_upload(message: Message) -> Optional[_Upload]:
    """Return the upload for a photo or an image document, or None for other messages.

    A document that imgbb cannot take gets an upload with `error` set.
    """
    if message.photo:
        return _Upload(message, media=message.photo, file_name="photo.jpg")

    document = message.document
    if not document:
        return None

    ext = _image_extension(document)
    if ext is None:
        is_image = (document.mime_type or "").lower().startswith("image/")
        return _Upload(
            message,
            error=(
                "⚠️ This image format is not supported by imgbb."
                if is_image
                else "⚠️ Only image files are supported."
            ),
        )
    return _Upload(message, media=document, file_name=document.file_name or f"image.{ext}")


def _add_to_album(client: Client, upload: _Upload) -> None:
    """Collect the items of an album, so that one job uploads all of them."""
    message = upload.message
    key = (message.chat.id, message.media_group_id)
    album = _albums.get(key)
    is_new = album is None
    if is_new:
        album = _albums[key] = _Album()
    album.uploads.append(upload)
    album.last_added = asyncio.get_running_loop().time()
    if is_new:
        _spawn(_collect_album(client, key, album))


async def _collect_album(client: Client, key: Tuple[int, int], album: _Album) -> None:
    """Wait until an album stops growing, then upload all its items as one job."""
    loop = asyncio.get_running_loop()
    try:
        while True:
            remaining = album.last_added + ALBUM_WAIT - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(remaining)
    finally:
        _albums.pop(key, None)

    uploads = sorted(album.uploads, key=lambda upload: upload.message.id)
    job = await _prepare_job(client, uploads)
    if job is not None:
        await _run_job(job)


async def _start_uploads(client: Client, uploads: List[_Upload]) -> None:
    """Prepare uploads, then run them in the background so the worker is free."""
    job = await _prepare_job(client, uploads)
    if job is not None:
        _spawn(_run_job(job))


def _first_status(uploads: List[_Upload], queued: bool) -> str:
    if len(uploads) > 1:
        count = sum(1 for upload in uploads if upload.error is None)
        return f"⏳ Uploading {_plural(count, 'image')}…"
    if uploads[0].source_url:
        return "⬆️ Uploading to imgbb…"
    return "⏳ Waiting in queue…" if queued else "⬇️ Downloading image…"


async def _prepare_job(client: Client, uploads: List[_Upload]) -> Optional[_UploadJob]:
    """Check the sender's key and the image sizes, then post a status message.

    All uploads come from one sender in one chat. Returns None when there is
    nothing to upload; the user is told why when that helps.
    """
    message = uploads[0].message
    is_private = message.chat.type == enums.ChatType.PRIVATE
    if not is_private:
        # Groups stay quiet about files that are not images
        uploads = [upload for upload in uploads if upload.error is None]
        if not uploads:
            return None
        message = uploads[0].message

    try:
        # Retrieve user's API key; in groups, stay silent for unregistered users
        api_key = await get_user_api_key(message.from_user.id)
        if not api_key:
            if is_private:
                bot_username = await get_bot_username(client)
                await message.reply(
                    "⚠️ API key not found. Please set your key first.",
                    reply_markup=_add_key_keyboard(bot_username),
                )
            return None

        for upload in uploads:
            size = getattr(upload.media, "file_size", None)
            if upload.error is None and size and size > MAX_FILE_SIZE:
                upload.error = "⚠️ Image is too large (maximum size is 32 MB)."
        if all(upload.error for upload in uploads):
            await message.reply(uploads[0].error)
            return None

        # Read before the upload: an image must never outlive the user's expiry
        expiration = await get_user_expiration(message.from_user.id)

        queued = _transfer_semaphore.locked()
        status = await message.reply(_first_status(uploads, queued))

    except errors.RPCError as exc:
        logger.warning("Telegram RPC error before upload: %s", exc)
        return None
    except Exception:
        logger.exception("Unexpected error while preparing image upload")
        if is_private:
            try:
                await message.reply("⚠️ Something went wrong. Please try again later.")
            except errors.RPCError:
                pass
        return None

    return _UploadJob(client, uploads, api_key, expiration, status, queued)


async def _run_job(job: _UploadJob) -> None:
    """Upload every image of a job, then post the result in its status message."""
    try:
        pending = [upload for upload in job.uploads if upload.error is None]
        if len(job.uploads) == 1:
            await _transfer(job, pending[0], show_progress=True)
        else:
            finished = 0

            async def transfer_and_count(upload: _Upload) -> None:
                nonlocal finished
                await _transfer(job, upload)
                finished += 1
                if finished < len(pending):
                    await _set_status(
                        job.status, f"⬆️ Uploaded {finished} of {len(pending)} images…"
                    )

            await asyncio.gather(*(transfer_and_count(upload) for upload in pending))

        await _report(job)

    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Unexpected error during image upload")
        await _set_status(job.status, "⚠️ Something went wrong during upload.", final=True)


async def _transfer(job: _UploadJob, upload: _Upload, show_progress: bool = False) -> None:
    """Upload one image to imgbb, and record its links or its error on `upload`."""
    try:
        if upload.source_url:
            # imgbb downloads the image itself, so no transfer slot is needed
            response = await _upload_to_imgbb(
                job.api_key, upload.source_url, expiration=job.expiration
            )
        else:
            async with _transfer_semaphore:
                if show_progress and job.queued:
                    await _set_status(job.status, "⬇️ Downloading image…")

                buffer = await _download(job.client, upload.media)
                if not buffer or not hasattr(buffer, "read"):
                    upload.error = "❌ Failed to download image from Telegram. Please retry."
                    return

                try:
                    if show_progress:
                        await _set_status(job.status, "⬆️ Uploading to imgbb…")
                    response = await _upload_to_imgbb(
                        job.api_key, buffer, upload.file_name, job.expiration
                    )
                finally:
                    buffer.close()

    except asyncio.TimeoutError:
        upload.error = "⏰ Download timed out. Please try sending a smaller image."
        return
    except Exception:
        logger.exception("Unexpected error during image upload")
        upload.error = "⚠️ Something went wrong during upload."
        return

    if response is None:
        upload.error = "❌ Upload failed after retries. Please try again later."
    elif response.status_code != 200:
        upload.error = f"❌ Upload failed: {_imgbb_error(response)}"
    else:
        try:
            data = response.json()["data"]
            upload.image_url = data["url"]
            upload.delete_url = data.get("delete_url")
        except (ValueError, KeyError, TypeError, AttributeError):
            logger.exception("Unexpected imgbb response payload: %s", response.text[:500])
            upload.error = "❌ Upload failed: unexpected response format from imgbb."


async def _report(job: _UploadJob) -> None:
    """Post the links, remove the uploaded originals, and save the uploads."""
    message = job.uploads[0].message
    is_private = message.chat.type == enums.ChatType.PRIVATE
    uploaded = [upload for upload in job.uploads if upload.image_url]
    failures = {upload.error for upload in job.uploads if upload.error}

    if not uploaded and len(failures) == 1:
        # One reason for every failure: show it once, as plain text
        await _set_status(
            job.status, failures.pop(), final=True, parse_mode=enums.ParseMode.DISABLED
        )
        return

    # Delete links stay private: in groups they go to the uploader instead
    if len(job.uploads) == 1:
        text = _result_text(
            message,
            uploaded[0].image_url,
            uploaded[0].delete_url if is_private else None,
            job.expiration,
        )
    else:
        text = _album_text(job.uploads, show_delete_links=is_private, expiration=job.expiration)
    posted = await _set_status(
        job.status,
        text,
        final=True,
        parse_mode=enums.ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    if not uploaded:
        return

    # Remove the original media only when the links are shown; in groups this
    # needs delete rights
    if posted:
        try:
            await job.client.delete_messages(
                message.chat.id, [upload.message.id for upload in uploaded]
            )
        except errors.RPCError as exc:
            logger.debug("Could not delete original message: %s", exc)

    await _save_history(message.from_user.id, uploaded, job.expiration)
    if not is_private:
        await _send_delete_links(job.client, message, uploaded)


async def _save_history(user_id: int, uploads: List[_Upload], expiration: Optional[int]) -> None:
    """Save uploads for /history; a database error must not hide the posted links."""
    expires_at = None
    if expiration:
        expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            seconds=expiration
        )
    records = [UploadRecord(upload.image_url, upload.delete_url, expires_at) for upload in uploads]
    try:
        await add_upload_history(user_id, records)
    except Exception:
        logger.exception("Could not save upload history for user %s", user_id)


async def _send_delete_links(client: Client, message: Message, uploads: List[_Upload]) -> None:
    """Send the delete links of a group upload to the uploader in private."""
    uploads = [upload for upload in uploads if upload.delete_url]
    if not uploads:
        return

    chat_title = html.escape(message.chat.title or "a group")
    parts = [f"🔒 Delete links for your upload in <b>{chat_title}</b>:"]
    parts.extend(
        f"{html.escape(upload.image_url)}\n{_delete_link(upload.delete_url)}"
        for upload in uploads
    )
    try:
        await client.send_message(
            message.from_user.id,
            "\n\n".join(parts),
            parse_mode=enums.ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            disable_notification=True,
        )
    except errors.RPCError as exc:
        # The user may have blocked the bot; /history still has the links
        logger.info("Could not send delete links to user %s: %s", message.from_user.id, exc)


async def wait_for_uploads(timeout: float) -> None:
    """Give running uploads up to `timeout` seconds to finish, then cancel them."""
    if not _upload_tasks:
        return
    logger.info("Waiting for %d running upload(s) to finish...", len(_upload_tasks))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    # Updates arrive until the client stops, so uploads can start while we
    # wait: check again until none are left or the time is up
    while _upload_tasks:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        await asyncio.wait(set(_upload_tasks), timeout=remaining)
    pending = set(_upload_tasks)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


# ==============================
# ROUTER REGISTRATION
# ==============================

def register_handlers(app: Client) -> None:
    """Register all message and callback query handlers on the client."""
    # Command handlers (private chats)
    app.on_message(filters.command(["start"]) & filters.private)(start_handler)
    app.on_message(filters.command(["cancel"]) & filters.private)(cancel_command_handler)
    app.on_message(filters.command(["help"]) & filters.private)(help_handler)
    app.on_message(filters.command(["mystatus"]) & filters.private)(mystatus_handler)
    app.on_message(filters.command(["deletekey"]) & filters.private)(deletekey_command_handler)
    app.on_message(filters.command(["expire"]) & filters.private)(expire_handler)
    app.on_message(filters.command(["history"]) & filters.private)(history_handler)

    # Callback queries
    app.on_callback_query(filters.regex("^cancel_action$"))(cancel_callback_handler)
    app.on_callback_query(filters.regex("^deletekeyforuser$"))(deletekey_prompt_callback)
    app.on_callback_query(filters.regex("^deletekey_cancel$"))(deletekey_cancel_callback)
    app.on_callback_query(filters.regex("^deletekey_confirm$"))(deletekey_confirm_callback)
    app.on_callback_query(filters.regex(r"^expire:\d+$"))(expire_callback)
    app.on_callback_query(filters.regex("^historyclear$"))(clear_history_prompt_callback)
    app.on_callback_query(filters.regex("^historyclear_cancel$"))(clear_history_cancel_callback)
    app.on_callback_query(filters.regex("^historyclear_confirm$"))(clear_history_confirm_callback)

    # Key capture and image link handler
    app.on_message(filters.private & filters.text)(receive_api_key_handler)

    # Media uploader handler
    app.on_message(filters.photo | filters.document)(upload_image_handler)
