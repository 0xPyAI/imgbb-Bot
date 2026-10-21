# ==========================================
# Production Dockerfile for Telegram imgbb Bot
# ==========================================
FROM python:3.11-slim

# Set environment variables for performance and security
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    SESSION_DIR=/app/data \
    HEARTBEAT_FILE=/app/data/heartbeat

# Create a non-privileged application user
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid 1000 --no-create-home --shell /sbin/nologin appuser

WORKDIR /app

# Install pinned dependencies first for optimal Docker layer caching.
# On x86-64 with Python 3.11 this includes TgCrypto for fast MTProto downloads
# (ARM64 images run without it, because TgCrypto has no ARM64 Linux wheels).
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy only the application code. It stays owned by root, so the bot cannot
# change its own code; /app/data (the volume) is the only writable path.
COPY *.py ./
RUN mkdir -p /app/data && chown appuser:appuser /app/data

# Switch to non-root user
USER appuser

# Kurigram writes unknown_errors.txt to the working directory, so run from the
# writable data directory
WORKDIR /app/data

# Healthy while the bot keeps touching its heartbeat file (every 30 s)
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os, sys, time; sys.exit(time.time() - os.path.getmtime(os.environ['HEARTBEAT_FILE']) > 90)"

# Start the bot
CMD ["python", "/app/pyro.py"]
