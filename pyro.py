"""Application entry point and lifecycle manager for the Telegram imgbb bot."""
import asyncio
import logging
import sys
from pathlib import Path

from pyrogram import Client, idle
from pyrogram.types import BotCommand, BotCommandScopeAllPrivateChats

from commands import KEY_PROMPT_TIMEOUT, register_handlers, wait_for_uploads
from config import settings
from crypto import get_fernet
from dbmodels import cleanup_expired_states, cleanup_expired_uploads, close_db, init_db
from http_client import close_http_client

# Configure structured application logging
_log_level = getattr(logging, settings.LOG_LEVEL, None)
if not isinstance(_log_level, int):
    _log_level = logging.INFO
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s (%(filename)s:%(lineno)d): %(message)s",
    level=_log_level,
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("pyrogrambot")

# httpx logs every request URL at INFO, and imgbb upload URLs carry the user's
# API key, so keep these loggers quiet unless something goes wrong
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
# Kurigram's DEBUG logs dump every raw Telegram request and update, which hold
# the bot token at login and the API keys that users send, so never go below INFO
logging.getLogger("pyrogram").setLevel(max(_log_level, logging.INFO))

# Optional Sentry observability integration
if settings.SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.httpx import HttpxIntegration

        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            traces_sample_rate=0.1,
            profiles_sample_rate=0.1,
            # imgbb upload URLs and the upload code's variables hold the user's
            # API key, so Sentry gets neither request URLs nor local variables
            disabled_integrations=[HttpxIntegration()],
            include_local_variables=False,
        )
        logger.info("Sentry monitoring initialized.")
    except ImportError:
        logger.warning("SENTRY_DSN is configured but sentry-sdk is not installed.")
    except Exception as e:
        logger.error("Failed to initialize Sentry: %s", e)

# Kurigram downloads one file at a time unless told otherwise. Allow as many
# parallel downloads as transfer slots, but cap it: Kurigram warns that high
# values cause network errors.
MAX_TELEGRAM_TRANSMISSIONS = 8

# Construct Kurigram client instance (Kurigram still imports as `pyrogram`)
app = Client(
    "bot",
    api_id=settings.API_ID,
    api_hash=settings.API_HASH,
    bot_token=settings.BOT_TOKEN,
    workdir=settings.SESSION_DIR,
    max_concurrent_transmissions=min(
        settings.MAX_CONCURRENT_DOWNLOADS, MAX_TELEGRAM_TRANSMISSIONS
    ),
)

# Register command menu for slash auto-completion in private chats
BOT_COMMANDS = [
    BotCommand("start", "Manage your imgbb API key"),
    BotCommand("mystatus", "Check your saved key"),
    BotCommand("expire", "Set when new uploads are deleted"),
    BotCommand("history", "Show your recent uploads"),
    BotCommand("deletekey", "Delete your saved API key"),
    BotCommand("cancel", "Cancel current operation"),
    BotCommand("help", "Usage guide and help"),
]

# Seconds that running uploads get to finish when the bot shuts down.
# Keep docker-compose stop_grace_period and systemd TimeoutStopSec above this.
SHUTDOWN_UPLOAD_GRACE = 30

# Seconds between heartbeat file updates (the Docker health check allows 90)
HEARTBEAT_INTERVAL = 30


async def _heartbeat(path: Path, interval_seconds: int = HEARTBEAT_INTERVAL) -> None:
    """Touch `path` while the event loop runs, so a health check can see a hung bot."""
    try:
        while True:
            try:
                path.touch()
            except OSError as e:
                logger.warning("Could not update heartbeat file %s: %s", path, e)
            await asyncio.sleep(interval_seconds)
    finally:
        path.unlink(missing_ok=True)


async def _periodic_cleanup(interval_seconds: int = 900) -> None:
    """Periodically purge expired pending states and expired uploads from the database."""
    logger.info("Started background DB state cleanup worker.")
    try:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                deleted_count = await cleanup_expired_states(
                    max_age_seconds=KEY_PROMPT_TIMEOUT
                )
                if deleted_count > 0:
                    logger.info("Cleaned up %d expired pending states.", deleted_count)

                # imgbb has already deleted these images
                deleted_uploads = await cleanup_expired_uploads()
                if deleted_uploads > 0:
                    logger.info("Removed %d expired uploads from history.", deleted_uploads)
            except Exception as e:
                logger.warning("Error during periodic database cleanup: %s", e)
    except asyncio.CancelledError:
        logger.info("Periodic cleanup worker stopped.")


async def main() -> None:
    """Start application services and maintain event loop."""
    # Stop now on a bad FERNET_KEY: otherwise the bot runs, but no saved key
    # can be read and every user is told that they have no key
    get_fernet()

    logger.info("Starting database initialization...")
    await init_db()

    # Register handlers inside the running loop: Kurigram 2.2.26 on PyPI
    # schedules handler registration as a task on the client's loop
    register_handlers(app)

    logger.info("Connecting to Telegram MTProto...")
    await app.start()

    background_tasks = []
    try:
        # Set private chat bot commands
        try:
            await app.set_bot_commands(
                BOT_COMMANDS, scope=BotCommandScopeAllPrivateChats()
            )
            logger.info("Bot command menu registered successfully.")
        except Exception as e:
            logger.warning("Could not register bot command menu: %s", e)

        # Launch background state cleanup and health heartbeat tasks
        background_tasks.append(asyncio.create_task(_periodic_cleanup()))
        if settings.HEARTBEAT_FILE:
            background_tasks.append(
                asyncio.create_task(_heartbeat(Path(settings.HEARTBEAT_FILE)))
            )

        logger.info("🤖 Bot @%s is running and ready for updates!", app.me.username)
        await idle()
    finally:
        logger.info("Shutting down bot services gracefully...")
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)

        # Uploads need the Telegram connection, so let them finish before stopping
        await wait_for_uploads(timeout=SHUTDOWN_UPLOAD_GRACE)
        if app.is_connected:
            await app.stop()

        await close_http_client()
        await close_db()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    try:
        # asyncio.run works with both Kurigram on PyPI and the GitHub main
        # branch; main no longer has the Client.loop attribute
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Process terminated by user signal.")
    except Exception as exc:
        logger.critical("Fatal error encountered during runtime: %s", exc, exc_info=True)
        sys.exit(1)
