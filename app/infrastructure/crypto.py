"""Fernet-based encrypt/decrypt helpers for role secret_vars.

Keys remain plaintext in the stored dict; only values are encrypted.
Decryption is all-or-nothing: decrypt_dict raises before returning any
plaintext if any value fails (wrong key, corrupted ciphertext).
"""
import json

from cryptography.fernet import Fernet, InvalidToken


def encrypt_dict(d: dict, key: str) -> dict:
    """Encrypt every value in *d*; raise ValueError if *key* is empty and *d* is non-empty."""
    if not d:
        return {}
    if not key:
        raise ValueError(
            "SECRETS_ENCRYPTION_KEY must be set to store secret_vars. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    f = Fernet(key.encode())
    return {k: f.encrypt(json.dumps(v).encode()).decode() for k, v in d.items()}


def decrypt_dict(d: dict, key: str) -> dict:
    """Decrypt every value in *d* atomically; raise InvalidToken on any failure.

    The full dict is decrypted before anything is returned so callers never
    receive a partial result.
    """
    if not d:
        return {}
    if not key:
        raise InvalidToken("SECRETS_ENCRYPTION_KEY is not set; cannot decrypt secret_vars")
    f = Fernet(key.encode())
    result = {}
    for k, v in d.items():
        result[k] = json.loads(f.decrypt(v.encode()))
    return result
