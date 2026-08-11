"""Regression tests for per-key secret_vars merge on role update
(openspec change ansible-vars-secrets-form-ui, Decision 3).

The key-value editor masks existing secrets as ●●● and re-submits that sentinel for any key the
admin left untouched. `_merge_secret_vars` must then reuse the stored ciphertext verbatim for those
keys (never needing their plaintext), encrypt only changed/new keys, and drop keys the admin
removed — while `{}` still clears everything.
"""

from unittest.mock import patch

from cryptography.fernet import Fernet

from app.infrastructure import crypto
from app.infrastructure.repositories import role_repo
from app.infrastructure.repositories.role_repo import (
    SECRET_UNCHANGED_SENTINEL,
    _merge_secret_vars,
)


def _key() -> str:
    return Fernet.generate_key().decode()


def _merge(existing_plain: dict, submitted: dict, key: str):
    existing_encrypted = crypto.encrypt_dict(existing_plain, key)
    with patch.object(role_repo.settings, "SECRETS_ENCRYPTION_KEY", key):
        merged, changed = _merge_secret_vars(existing_encrypted, submitted)
    return existing_encrypted, merged, changed


def test_untouched_masked_key_reuses_stored_ciphertext():
    key = _key()
    existing_encrypted, merged, changed = _merge(
        {"db_password": "s3cr3t", "api_key": "abc"},
        {
            "db_password": SECRET_UNCHANGED_SENTINEL,
            "api_key": SECRET_UNCHANGED_SENTINEL,
        },
        key,
    )
    # Ciphertext copied verbatim — no re-encryption of untouched secrets.
    assert merged == existing_encrypted
    assert changed == []
    assert crypto.decrypt_dict(merged, key) == {
        "db_password": "s3cr3t",
        "api_key": "abc",
    }


def test_changed_value_is_reencrypted_and_others_preserved():
    key = _key()
    existing_encrypted, merged, changed = _merge(
        {"db_password": "old", "api_key": "keep"},
        {"db_password": "new-value", "api_key": SECRET_UNCHANGED_SENTINEL},
        key,
    )
    assert changed == ["db_password"]
    assert merged["api_key"] == existing_encrypted["api_key"]  # untouched: verbatim
    assert (
        merged["db_password"] != existing_encrypted["db_password"]
    )  # changed: re-encrypted
    assert crypto.decrypt_dict(merged, key) == {
        "db_password": "new-value",
        "api_key": "keep",
    }


def test_new_key_is_encrypted():
    key = _key()
    _, merged, changed = _merge(
        {"existing": "x"},
        {"existing": SECRET_UNCHANGED_SENTINEL, "added": "brand-new"},
        key,
    )
    assert changed == ["added"]
    assert crypto.decrypt_dict(merged, key) == {"existing": "x", "added": "brand-new"}


def test_removed_key_is_dropped():
    key = _key()
    _, merged, _ = _merge(
        {"keep": "a", "drop": "b"},
        {"keep": SECRET_UNCHANGED_SENTINEL},
        key,
    )
    assert set(merged.keys()) == {"keep"}


def test_empty_mapping_clears_all():
    key = _key()
    _, merged, changed = _merge({"a": "1", "b": "2"}, {}, key)
    assert merged == {}
    assert changed == []


def test_sentinel_for_unknown_key_is_stored_literally():
    """A sentinel value for a key that doesn't already exist can't be "kept" — it's a real
    (if odd) new secret and must be encrypted, not silently dropped."""
    key = _key()
    _, merged, changed = _merge({}, {"weird": SECRET_UNCHANGED_SENTINEL}, key)
    assert changed == ["weird"]
    assert crypto.decrypt_dict(merged, key) == {"weird": SECRET_UNCHANGED_SENTINEL}


def test_sentinel_matches_the_js_mask_character():
    """Guard against the Python/JS sentinel drifting apart: it must be three U+25CF bullets."""
    assert SECRET_UNCHANGED_SENTINEL == "●●●"
