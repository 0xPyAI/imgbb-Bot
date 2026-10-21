"""Database models and async connection management using SQLAlchemy."""
import datetime
import logging
from typing import List, NamedTuple, Optional, Sequence, Tuple

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    delete,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from config import settings
from crypto import decrypt, encrypt

logger = logging.getLogger(__name__)

# Async engine with production-ready connection pooling and Neon/PgBouncer compatibility.
# Every query here reads or writes a few rows at most, so a small pool covers
# heavy traffic and stays well inside managed-Postgres connection limits.
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_size=5,
    max_overflow=10,
    pool_recycle=1800,  # Recycle connections every 30 minutes to prevent stale sockets
    pool_pre_ping=True,  # Test connection health before acquiring from pool
    connect_args={"statement_cache_size": 0},
)

# Async session factory
AsyncSessionLocal = async_sessionmaker(
    engine,
    expire_on_commit=False,
)

# Uploads kept per user; older ones are removed when new uploads are saved
HISTORY_KEEP = 50


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Base(DeclarativeBase):
    pass


class ApiKeydb(Base):
    """Stores encrypted user imgbb API keys."""

    __tablename__ = "apikey_tg"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Telegram user IDs exceed 32-bit integer limits; BigInteger is required
    user_id = Column(BigInteger, unique=True, index=True, nullable=False)
    api_key = Column(String, nullable=False)
    creation_date = Column(DateTime(timezone=True), default=_utcnow, nullable=False)


class PendingState(Base):
    """Stores temporary user conversation state so it survives bot restarts."""

    __tablename__ = "pending_state"

    user_id = Column(BigInteger, primary_key=True)
    state = Column(String, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        index=True,
        nullable=False,
    )


class UserSettings(Base):
    """Stores per-user upload preferences."""

    __tablename__ = "user_settings"

    user_id = Column(BigInteger, primary_key=True)
    # Seconds after which imgbb deletes new uploads; NULL keeps them forever
    expiration = Column(Integer, nullable=True)
    updated_at = Column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class UploadHistory(Base):
    """Stores each user's recent uploads; delete links are encrypted at rest."""

    __tablename__ = "upload_history"
    __table_args__ = (Index("ix_upload_history_user_id_id", "user_id", "id"),)

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False)
    image_url = Column(String, nullable=False)
    delete_url = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    # When imgbb deletes the image; NULL if it never expires
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)


class UploadRecord(NamedTuple):
    """One uploaded image, as saved for /history."""

    image_url: str
    delete_url: Optional[str] = None
    expires_at: Optional[datetime.datetime] = None
    created_at: Optional[datetime.datetime] = None


# ==============================
# DATABASE LIFECYCLE & HELPERS
# ==============================
async def init_db() -> None:
    """Initialize database tables and run lightweight migrations."""
    logger.info("Initializing database schema...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_legacy_columns(conn)
    logger.info("Database schema initialized successfully.")


async def _migrate_legacy_columns(conn: AsyncConnection) -> None:
    """Upgrade apikey_tg columns that older versions of the bot created."""
    result = await conn.execute(
        text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'apikey_tg'"
        )
    )
    column_types = dict(result.all())

    # Legacy tables stored Telegram user IDs as 32-bit integers
    if column_types.get("user_id") == "integer":
        logger.info("Migrating apikey_tg.user_id to BIGINT...")
        await conn.execute(
            text("ALTER TABLE apikey_tg ALTER COLUMN user_id TYPE BIGINT")
        )

    # Legacy tables stored creation dates without a time zone (values are UTC)
    if column_types.get("creation_date") == "timestamp without time zone":
        logger.info("Migrating apikey_tg.creation_date to TIMESTAMPTZ...")
        await conn.execute(
            text(
                "ALTER TABLE apikey_tg ALTER COLUMN creation_date TYPE TIMESTAMPTZ "
                "USING creation_date AT TIME ZONE 'UTC'"
            )
        )


async def close_db() -> None:
    """Dispose of the database connection pool on application shutdown."""
    logger.info("Disposing database connection pool...")
    await engine.dispose()
    logger.info("Database connection pool disposed.")


async def get_user_key_info(
    user_id: int,
) -> Optional[Tuple[Optional[str], Optional[datetime.datetime]]]:
    """Return (decrypted key, creation date) for a user, or None if no key is stored.

    The key is None when the stored value cannot be decrypted, which usually
    means FERNET_KEY changed after the key was saved.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ApiKeydb.api_key, ApiKeydb.creation_date).where(
                ApiKeydb.user_id == user_id
            )
        )
        row = result.first()

    if row is None:
        return None

    try:
        api_key = decrypt(row.api_key)
    except Exception:
        logger.error(
            "Could not decrypt stored key for user %s (was FERNET_KEY changed?)",
            user_id,
        )
        api_key = None
    return api_key or None, row.creation_date


async def get_user_api_key(user_id: int) -> Optional[str]:
    """Retrieve and decrypt the user's API key from the database."""
    info = await get_user_key_info(user_id)
    return info[0] if info else None


async def save_user_api_key(user_id: int, plaintext_key: str) -> None:
    """Encrypt and save or update a user's API key in one atomic upsert."""
    stmt = insert(ApiKeydb).values(
        user_id=user_id,
        api_key=encrypt(plaintext_key.strip()),
        creation_date=_utcnow(),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[ApiKeydb.user_id],
        set_={
            "api_key": stmt.excluded.api_key,
            "creation_date": stmt.excluded.creation_date,
        },
    )
    async with AsyncSessionLocal() as session:
        await session.execute(stmt)
        await session.commit()


async def delete_user_api_key(user_id: int) -> bool:
    """Delete a user's stored API key. Returns True if a key was deleted."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            delete(ApiKeydb).where(ApiKeydb.user_id == user_id)
        )
        await session.commit()
    return result.rowcount > 0


async def get_pending_state(user_id: int) -> Optional[PendingState]:
    """Return a user's pending conversation state, if any."""
    async with AsyncSessionLocal() as session:
        return await session.get(PendingState, user_id)


async def set_pending_state(user_id: int, state: str) -> None:
    """Create or restart a user's pending conversation state."""
    stmt = insert(PendingState).values(
        user_id=user_id, state=state, created_at=_utcnow()
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[PendingState.user_id],
        set_={"state": stmt.excluded.state, "created_at": stmt.excluded.created_at},
    )
    async with AsyncSessionLocal() as session:
        await session.execute(stmt)
        await session.commit()


async def clear_pending_state(user_id: int) -> bool:
    """Delete a user's pending state. Returns True if one existed."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            delete(PendingState).where(PendingState.user_id == user_id)
        )
        await session.commit()
    return result.rowcount > 0


async def cleanup_expired_states(max_age_seconds: int = 600) -> int:
    """Delete PendingState records older than max_age_seconds."""
    cutoff = _utcnow() - datetime.timedelta(seconds=max_age_seconds)
    async with AsyncSessionLocal() as session:
        stmt = delete(PendingState).where(PendingState.created_at < cutoff)
        result = await session.execute(stmt)
        await session.commit()
        return result.rowcount


async def get_user_expiration(user_id: int) -> Optional[int]:
    """Return seconds after which imgbb deletes the user's uploads, or None."""
    async with AsyncSessionLocal() as session:
        return await session.scalar(
            select(UserSettings.expiration).where(UserSettings.user_id == user_id)
        )


async def set_user_expiration(user_id: int, seconds: Optional[int]) -> None:
    """Save when imgbb deletes the user's new uploads; None keeps them forever."""
    stmt = insert(UserSettings).values(
        user_id=user_id, expiration=seconds, updated_at=_utcnow()
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[UserSettings.user_id],
        set_={
            "expiration": stmt.excluded.expiration,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    async with AsyncSessionLocal() as session:
        await session.execute(stmt)
        await session.commit()


async def add_upload_history(user_id: int, records: Sequence[UploadRecord]) -> None:
    """Save new uploads and keep only the user's newest HISTORY_KEEP."""
    if not records:
        return
    now = _utcnow()
    rows = [
        {
            "user_id": user_id,
            "image_url": record.image_url,
            "delete_url": encrypt(record.delete_url) if record.delete_url else None,
            "created_at": now,
            "expires_at": record.expires_at,
        }
        for record in records
    ]
    newest = (
        select(UploadHistory.id)
        .where(UploadHistory.user_id == user_id)
        .order_by(UploadHistory.id.desc())
        .limit(HISTORY_KEEP)
    )
    async with AsyncSessionLocal() as session:
        await session.execute(insert(UploadHistory), rows)
        await session.execute(
            delete(UploadHistory).where(
                UploadHistory.user_id == user_id, UploadHistory.id.not_in(newest)
            )
        )
        await session.commit()


async def get_upload_history(user_id: int, limit: int = 10) -> List[UploadRecord]:
    """Return the user's newest uploads that have not expired, newest first.

    A delete link is None when it cannot be decrypted, which usually means
    FERNET_KEY changed after the upload.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(
                UploadHistory.image_url,
                UploadHistory.delete_url,
                UploadHistory.expires_at,
                UploadHistory.created_at,
            )
            .where(
                UploadHistory.user_id == user_id,
                or_(
                    UploadHistory.expires_at.is_(None),
                    UploadHistory.expires_at > _utcnow(),
                ),
            )
            .order_by(UploadHistory.id.desc())
            .limit(limit)
        )
        rows = result.all()

    records = []
    unreadable = 0
    for row in rows:
        delete_url = None
        if row.delete_url:
            try:
                delete_url = decrypt(row.delete_url)
            except Exception:
                unreadable += 1
        records.append(
            UploadRecord(row.image_url, delete_url, row.expires_at, row.created_at)
        )
    if unreadable:
        logger.error(
            "Could not decrypt %d saved delete link(s) for user %s (was FERNET_KEY changed?)",
            unreadable,
            user_id,
        )
    return records


async def clear_upload_history(user_id: int) -> int:
    """Delete all saved uploads of a user. Returns the number removed."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            delete(UploadHistory).where(UploadHistory.user_id == user_id)
        )
        await session.commit()
    return result.rowcount


async def cleanup_expired_uploads() -> int:
    """Delete saved uploads that imgbb has already deleted."""
    async with AsyncSessionLocal() as session:
        stmt = delete(UploadHistory).where(UploadHistory.expires_at < _utcnow())
        result = await session.execute(stmt)
        await session.commit()
        return result.rowcount
