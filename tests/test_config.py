"""Unit tests for configuration loading and validation."""
import pytest

from config import get, get_bool, get_int


def test_get_from_environ(monkeypatch):
    monkeypatch.setenv("TEST_CONFIG_KEY", "custom_value")
    assert get("TEST_CONFIG_KEY") == "custom_value"


def test_get_with_alias(monkeypatch):
    monkeypatch.delenv("PRIMARY_KEY", raising=False)
    monkeypatch.setenv("LEGACY_KEY", "legacy_value")
    assert get("PRIMARY_KEY", aliases=["LEGACY_KEY"]) == "legacy_value"


def test_get_required_raises_when_missing(monkeypatch):
    monkeypatch.delenv("TOTALLY_MISSING_KEY", raising=False)
    with pytest.raises(RuntimeError, match="Missing required configuration key"):
        get("TOTALLY_MISSING_KEY", required=True)


def test_get_int(monkeypatch):
    monkeypatch.setenv("INT_KEY", "42")
    assert get_int("INT_KEY") == 42

    monkeypatch.setenv("BAD_INT_KEY", "not_a_number")
    with pytest.raises(RuntimeError, match="must be an integer"):
        get_int("BAD_INT_KEY")


def test_get_bool(monkeypatch):
    monkeypatch.setenv("BOOL_TRUE_1", "true")
    monkeypatch.setenv("BOOL_TRUE_2", "1")
    monkeypatch.setenv("BOOL_FALSE", "0")
    monkeypatch.delenv("BOOL_MISSING", raising=False)

    assert get_bool("BOOL_TRUE_1") is True
    assert get_bool("BOOL_TRUE_2") is True
    assert get_bool("BOOL_FALSE") is False
    assert get_bool("BOOL_MISSING", default=False) is False
    assert get_bool("BOOL_MISSING", default=True) is True
