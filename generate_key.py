#!/usr/bin/env python3
"""Generate a Fernet secret key for encrypting API keys at rest.

Run once, then copy the printed value into your .env or config.env file:
    python generate_key.py
"""
from cryptography.fernet import Fernet

if __name__ == "__main__":
    key = Fernet.generate_key().decode("utf-8")
    print("=" * 60)
    print("Generated FERNET_KEY (Keep this secret and securely backed up):")
    print("=" * 60)
    print(f"FERNET_KEY={key}")
    print("=" * 60)
