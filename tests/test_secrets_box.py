"""
Unit tests for encrypting secrets at rest.
"""
import pytest

import src.storage.secrets_box as sb
from src.storage.secrets_box import SecretsBox

# These tests need the real encryption library; they are skipped (not failed) without it.
requires_crypto = pytest.mark.skipif(sb.Fernet is None, reason="the 'cryptography' package is not installed")

PASSPHRASE = "a-long-passphrase-123"


@requires_crypto
def test_round_trip_and_ciphertext_hides_the_secret():
    box = SecretsBox(PASSPHRASE)
    assert box.available
    token = box.encrypt("my-bybit-secret")
    assert "my-bybit-secret" not in token
    assert box.decrypt(token) == "my-bybit-secret"


@requires_crypto
def test_wrong_passphrase_cannot_decrypt():
    token = SecretsBox(PASSPHRASE).encrypt("secret")
    assert SecretsBox("a-different-long-passphrase").decrypt(token) is None


def test_damaged_token_returns_none_instead_of_raising():
    box = SecretsBox(PASSPHRASE)
    assert box.decrypt("not-a-real-token") is None


@requires_crypto
def test_no_key_means_unavailable_and_refuses_to_encrypt():
    box = SecretsBox("")
    assert not box.available
    assert "STATE_ENCRYPTION_KEY" in box.unavailable_reason
    assert box.decrypt("anything") is None
    try:
        box.encrypt("secret")
        assert False, "encrypt must refuse when unavailable"
    except RuntimeError:
        pass


@requires_crypto
def test_short_passphrase_is_rejected():
    box = SecretsBox("short")
    assert not box.available
    assert "shorter" in box.unavailable_reason


def test_missing_cryptography_package_means_unavailable(monkeypatch):
    monkeypatch.setattr(sb, "Fernet", None)
    box = SecretsBox(PASSPHRASE)
    assert not box.available
    assert "cryptography" in box.unavailable_reason


@requires_crypto
def test_from_env_reads_the_variable(monkeypatch):
    monkeypatch.setenv("STATE_ENCRYPTION_KEY", PASSPHRASE)
    assert SecretsBox.from_env().available
    monkeypatch.delenv("STATE_ENCRYPTION_KEY")
    assert not SecretsBox.from_env().available
