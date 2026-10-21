"""Shared HTTP client with connection pooling and lifecycle management."""
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_http_client: Optional[httpx.AsyncClient] = None


def get_http_client() -> httpx.AsyncClient:
    """Return shared httpx.AsyncClient with connection pooling."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0, read=30.0),
            limits=httpx.Limits(
                max_keepalive_connections=50,
                max_connections=100,
                keepalive_expiry=30.0,
            ),
            follow_redirects=True,
        )
    return _http_client


async def close_http_client() -> None:
    """Close the shared HTTP client on shutdown to release open sockets."""
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        logger.info("Closing shared HTTP client...")
        await _http_client.aclose()
        _http_client = None
        logger.info("Shared HTTP client closed.")
