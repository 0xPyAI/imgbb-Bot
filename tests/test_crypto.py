"""Unit tests for cryptography and key validation."""
import pytest
from cryptography.fernet import Fernet, InvalidToken

from crypto import decrypt, encrypt, get_fernet, is_valid_imgbb_key


def test_fernet_initialization():
    fernet = get_fernet()
    assert fernet is not None
    assert isinstance(fernet, Fernet)


def test_encrypt_decrypt_roundtrip():
    test_key = "1234567890abcdef1234567890abcdef"
    encrypted = encrypt(test_key)
    assert encrypted != test_key
    assert len(encrypted) > len(test_key)

    decrypted = decrypt(encrypted)
    assert decrypted == test_key


def test_decrypt_legacy_plaintext():
    legacy_key = "abcdef1234567890abcdef1234567890"
    assert decrypt(legacy_key) == legacy_key


def test_decrypt_with_wrong_fernet_key_raises():
    # A token made with another FERNET_KEY must not come back as if it were a key
    foreign_token = Fernet(Fernet.generate_key()).encrypt(b"1234567890abcdef1234567890abcdef")
    with pytest.raises(InvalidToken):
        decrypt(foreign_token.decode())


def test_decrypt_garbage_raises():
    with pytest.raises(InvalidToken):
        decrypt("plain_text_api_key_not_encrypted")


def test_is_valid_imgbb_key():
    valid_key = "1234567890abcdef1234567890abcdef"
    assert is_valid_imgbb_key(valid_key) is True
    assert is_valid_imgbb_key(valid_key.upper()) is True

    # Invalid cases
    assert is_valid_imgbb_key("") is False
    assert is_valid_imgbb_key(None) is False
    assert is_valid_imgbb_key("short_key") is False
    assert is_valid_imgbb_key("1234567890abcdef1234567890abcdef_too_long") is False
    assert is_valid_imgbb_key("1234567890abcdef1234567890abcdez") is False  # 'z' is not hex
