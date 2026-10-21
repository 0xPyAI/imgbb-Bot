#!/usr/bin/env python3
"""One-off utility script to initialize database tables and run migrations.

Usage:
    python init_tables.py
"""
import asyncio
import logging
import sys

from dbmodels import close_db, init_db

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("init_tables")


async def main() -> None:
    """Initialize database tables."""
    try:
        logger.info("Connecting to PostgreSQL and creating tables...")
        await init_db()
        logger.info("✅ Database tables and schema verified successfully!")
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
