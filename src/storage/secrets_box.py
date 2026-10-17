"""
Encrypts the console's secrets (exchange API keys, Telegram token) before they are
written to disk.

Rule: a secret is never written to disk in plain text. If there is no encryption key,
or the `cryptography` package is missing, secrets simply stay in memory and have to be
re-entered after a restart.

The key is read from the STATE_ENCRYPTION_KEY environment variable and can be any
long passphrase; it is stretched with PBKDF2 into a Fernet key. Keep it out of the
repository and out of the state file's folder.
"""
import base64
import hashlib
import os
from typing import Optional

try:  # optional dependency: pip install cryptography
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - exercised by tests through monkeypatching
    Fernet = None  # type: ignore
    InvalidToken = Exception  # type: ignore

# Fixed, non-secret salt: it only makes the derived key specific to this app. The
# strength comes from the passphrase.
_SALT = b"trading-bot-state-secrets-v1"
_ITERATIONS = 200_000
MIN_PASSPHRASE_LENGTH = 12


def _derive_fernet_key(passphrase: str) -> bytes:
    raw = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), _SALT, _ITERATIONS)
    return base64.urlsafe_b64encode(raw)


class SecretsBox:
    def __init__(self, passphrase: Optional[str]):
        self._fernet = None
        self.unavailable_reason: Optional[str] = None
        if Fernet is None:
            self.unavailable_reason = "the 'cryptography' package is not installed"
        elif not passphrase:
            self.unavailable_reason = "STATE_ENCRYPTION_KEY is not set"
        elif len(passphrase) < MIN_PASSPHRASE_LENGTH:
            self.unavailable_reason = f"STATE_ENCRYPTION_KEY is shorter than {MIN_PASSPHRASE_LENGTH} characters"
        else:
            self._fernet = Fernet(_derive_fernet_key(passphrase))

    @classmethod
    def from_env(cls) -> "SecretsBox":
        return cls(os.environ.get("STATE_ENCRYPTION_KEY", ""))

    @property
    def available(self) -> bool:
        return self._fernet is not None

    def encrypt(self, text: str) -> str:
        if not self._fernet:
            raise RuntimeError(f"Secrets cannot be saved: {self.unavailable_reason}")
        return self._fernet.encrypt(text.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> Optional[str]:
        """Return the plain text, or None if the token cannot be decrypted (wrong key,
        damaged data, or encryption unavailable)."""
        if not self._fernet:
            return None
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError):
            return None
