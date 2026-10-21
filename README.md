# 🖼 Production-Ready Telegram imgbb Upload Bot

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

A hardened, containerized, production-ready Telegram bot that instantly uploads images to [imgbb](https://imgbb.com) and replies with direct shareable links. Each user brings their **own** imgbb API key, which is **encrypted at rest** using Fernet (AES-128-CBC + HMAC) — guaranteeing user privacy and ensuring uploads land directly in each user's personal imgbb account.

Works seamlessly in **private chats** and **group chats**.

---

## ✨ Features & Architecture

- 🔐 **Per-User Encrypted Storage** — API keys are encrypted at rest with Fernet (AES-128-CBC + HMAC) before storing in PostgreSQL. Keys are only decrypted in-memory during an active upload, and never written to logs.
- 🔗 **Private Delete Links & Expiry** — Every upload comes with its imgbb delete link: in a private chat it is part of the reply, and for a group upload the bot sends it to the uploader in private. `/expire` makes imgbb delete new uploads after a set time (1 minute to 180 days).
- 🖼 **Albums & Image Links** — An album gets one reply with all of its links, and its originals are removed together. In a private chat you can also send an image link: imgbb downloads it, so the bot never holds the file.
- 🗂 **Upload History** — `/history` lists your last 10 uploads with their delete links, which are also encrypted at rest. Expired uploads leave the list automatically, and a button clears it.
- 🛡️ **Zero-Exposure MTProto Streaming** — Downloads media directly to in-memory buffers via MTProto and streams raw multipart bytes to imgbb. Your `BOT_TOKEN` is never sent to third-party endpoints.
- 🚀 **High-Performance Async Architecture** — Built on [Kurigram](https://github.com/kurigram-org/kurigram) (actively-maintained Pyrogram fork) and async SQLAlchemy with PostgreSQL connection pooling (`asyncpg`). Handlers never wait on a user or an upload, so Kurigram's small fixed pool of update workers stays free.
- ⚡ **Connection Pooling & Keep-Alive** — Reusable `httpx.AsyncClient` connection pool eliminates TCP/TLS handshake latency for upstream imgbb API calls.
- 🚦 **Concurrency & Memory Throttling** — Uploads run as background jobs behind an `asyncio.Semaphore`. A slot is held from download until the imgbb upload ends, so at most `MAX_CONCURRENT_DOWNLOADS` images sit in RAM at once, and Telegram downloads run in parallel (up to 8).
- 🔄 **Resilient Error Recovery** — Automatic exponential backoff retries for transient 5xx or network errors, plus handling for Telegram `FloodWait` and RPC exceptions.
- ⏳ **Background Garbage Collection** — Asynchronous periodic worker purges expired conversation states from PostgreSQL.
- 🐳 **Deployment Ready** — Non-root, read-only Docker container with a heartbeat health check and log rotation, `docker-compose.yml`, a sandboxed `systemd` unit, graceful shutdown that lets running uploads finish, and fully pinned dependencies.

---

## 🤖 Bot Commands

| Command      | Scope         | Description                                        |
| ------------ | ------------- | -------------------------------------------------- |
| `/start`     | Private Chats | Open interactive menu to add or delete your API key |
| `/mystatus`  | Private Chats | Show your saved key (masked) and when it was added |
| `/expire`    | Private Chats | Set when imgbb deletes new uploads (`/expire 12h`, `/expire off`) |
| `/history`   | Private Chats | Show your recent uploads with their delete links   |
| `/deletekey` | Private Chats | Securely delete your API key (with confirmation)   |
| `/cancel`    | Private Chats | Cancel any pending operation or key setup          |
| `/help`      | Private Chats | Show usage guide and instructions                  |

---

## 🛠 Prerequisites

- Python 3.10 or higher (or Docker)
- PostgreSQL 14+ database (or the optional bundled one in Docker Compose)
- Telegram API ID & API Hash from [my.telegram.org](https://my.telegram.org)
- Telegram Bot Token from [@BotFather](https://t.me/BotFather)

---

## 🚀 Quick Start (Docker Compose — Recommended)

1. **Clone the repository:**
   ```bash
   git clone https://github.com/<your-username>/<your-repo>.git
   cd <your-repo>
   ```

2. **Generate encryption key & create configuration:**
   ```bash
   python generate_key.py
   cp config.env.example config.env
   chmod 600 config.env
   ```
   *Edit `config.env` and paste the generated `FERNET_KEY`, your Telegram `API_ID`, `API_HASH`, `BOT_TOKEN`, and your `DATABASE_URL`.*

3. **Start the bot:**
   ```bash
   docker compose up -d --build
   ```
   No database yet? Use the bundled PostgreSQL: put `POSTGRES_PASSWORD=<a strong password>` in a `.env` file next to `docker-compose.yml`, set `DATABASE_URL=postgresql://postgres:<that password>@db:5432/imgbb_bot` in `config.env`, and run `docker compose --profile local-db up -d --build`.

4. **View live logs and health:**
   ```bash
   docker compose logs -f bot
   docker compose ps   # STATUS shows (healthy) once the bot is running
   ```

---

## 💻 Manual Setup (Local / VM)

1. **Create and activate a virtual environment** (on Debian or Ubuntu, first run `sudo apt install python3-venv`):
   ```bash
   python -m venv venv
   # Linux / macOS:
   source venv/bin/activate
   # Windows PowerShell:
   venv\Scripts\Activate.ps1
   ```

2. **Install the pinned dependencies:**
   ```bash
   pip install -r requirements.txt
   # Optional: Kurigram from its GitHub main branch instead of the PyPI release
   pip install https://github.com/kurigram-org/kurigram/archive/main.zip --force-reinstall
   ```
   On x86-64 Linux with Python 3.10 or 3.11 this also installs TgCrypto, which makes Telegram downloads much faster.

3. **Configure environment:**
   ```bash
   cp config.env.example config.env
   python generate_key.py
   # Edit config.env with your credentials and FERNET_KEY
   ```

4. **Start the application:**
   ```bash
   python pyro.py
   ```
   The bot creates and migrates its tables at startup. To do that step on its own (for example before a deploy), run `python init_tables.py`.

### Running as a systemd service

```bash
sudo apt install python3-venv   # Debian / Ubuntu
sudo useradd --system --home /opt/pyrogrambot --shell /usr/sbin/nologin appuser
sudo cp -r . /opt/pyrogrambot && cd /opt/pyrogrambot
sudo python3 -m venv venv && sudo venv/bin/pip install -r requirements.txt
sudo chown -R appuser:appuser /opt/pyrogrambot && sudo chmod 600 config.env
sudo cp pyrogrambot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now pyrogrambot
journalctl -u pyrogrambot -f
```

---

## ⚙️ Configuration Reference

All settings can be configured via `config.env`, `.env`, or real OS environment variables. Environment variables take precedence. Legacy aliases are still read, so older config files keep working.

| Variable | Legacy Alias | Required | Default | Description |
| :--- | :--- | :--- | :--- | :--- |
| `API_ID` | `YOUR_API_ID` | **Yes** | — | Telegram API ID from [my.telegram.org](https://my.telegram.org) |
| `API_HASH` | `YOUR_API_HASH` | **Yes** | — | Telegram API Hash from [my.telegram.org](https://my.telegram.org) |
| `BOT_TOKEN` | `token` | **Yes** | — | Bot token from [@BotFather](https://t.me/BotFather) |
| `DATABASE_URL` | `grespost1` | **Yes** | — | PostgreSQL URI (`postgresql://user:pass@host:5432/db`) |
| `FERNET_KEY` | — | **Yes** | — | 32-byte URL-safe base64 key generated via `generate_key.py` |
| `LOG_LEVEL` | — | No | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`). Kurigram's own logs never go below `INFO`, because its debug output holds the bot token and users' keys |
| `MAX_CONCURRENT_DOWNLOADS` | — | No | `4` | Maximum images transferred at once; each is held in RAM (up to 32 MB) until its upload ends |
| `DOWNLOAD_TIMEOUT` | — | No | `60` | Timeout in seconds for MTProto media downloads |
| `SESSION_DIR` | — | No | project directory | Directory for the Telegram `bot.session` file (`/app/data` in Docker) |
| `HEARTBEAT_FILE` | — | No | off | File the bot touches every 30 s for health checks (set in the Docker image) |
| `SENTRY_DSN` | — | No | `None` | Sentry DSN for error reporting (needs `pip install "sentry-sdk>=2"`). Request URLs and local variables are not sent, because they can hold users' API keys |

---

## ✅ Production Checklist

- **Back up `FERNET_KEY`.** Without it, every saved user key and delete link is unreadable, and users must add their keys again.
- **Keep `config.env` private** (`chmod 600`); it holds your bot token and database password. Put a value that contains `$` in single quotes: Docker Compose expands `$` in other values.
- **Run one instance per bot token.** Two running copies both answer every message.
- **Size memory for uploads:** `MAX_CONCURRENT_DOWNLOADS × 32 MB` must stay well under the container or service memory limit (1.5 GB by default).
- **Groups:** make the bot an admin, so that it receives the images and can remove the originals after upload.

---

## 🧪 Testing & Code Quality

Install the pinned development tools, then run the test suite (no Telegram account or `config.env` needed; `tests/conftest.py` supplies dummy settings):
```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
ruff check .
```
The database tests in `tests/test_db.py` run only when `TEST_DATABASE_URL` names an **empty, throwaway** PostgreSQL database, because they drop and create the bot's tables (they refuse to run on a database that holds other tables):
```bash
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/bot_test python -m pytest tests/ -v
```

To update dependencies, change the ranges in `pyproject.toml` and compile new pins with [uv](https://docs.astral.sh/uv/):
```bash
uv pip compile pyproject.toml --universal --python-version 3.10 -o requirements.txt
uv pip compile pyproject.toml --universal --python-version 3.10 --extra dev -o requirements-dev.txt
```

---

## 📂 Project Structure

```
.
├── .github/workflows/
│   └── ci.yml               # CI: lint, tests on Python 3.10-3.14 with PostgreSQL, audit, Docker build
├── tests/
│   ├── conftest.py          # Dummy settings and shared fakes, so tests run without secrets
│   ├── test_app.py          # Update routing, startup, shutdown signals, settings, scripts
│   ├── test_commands.py     # Key capture, uploads, albums, links, expiry, history
│   ├── test_config.py       # Configuration and fallback tests
│   ├── test_crypto.py       # Encryption roundtrip & validation tests
│   ├── test_db.py           # Real PostgreSQL: migrations, upserts, history (TEST_DATABASE_URL)
│   └── test_handlers.py     # Menus, error paths, Kurigram call signatures
├── commands.py              # Telegram handlers and upload pipeline
├── config.py                # Typed settings loader with env fallbacks
├── crypto.py                # Fernet encryption service & key syntax validator
├── dbmodels.py              # SQLAlchemy async models, pooling & migrations
├── http_client.py           # Shared httpx AsyncClient with connection pooling
├── pyro.py                  # Application entry point & lifecycle manager
├── generate_key.py          # Fernet key generator utility
├── init_tables.py           # Optional stand-alone table creation & migration
├── Dockerfile               # Non-root, read-only production image
├── docker-compose.yml       # Bot service plus optional bundled PostgreSQL
├── pyrogrambot.service      # Sandboxed Linux systemd service unit
├── pyproject.toml           # Dependency ranges and tool configuration
├── requirements.txt         # Pinned production dependencies (compiled)
├── requirements-dev.txt     # Pinned production + test dependencies (compiled)
└── README.md
```

---

## 📄 License

Released under the [MIT License](LICENSE).
