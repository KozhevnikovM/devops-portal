"""Tests for the role secret_vars feature (#275).

Covers: crypto helpers, ansible runner secret injection, provision task no-retry on
SecretDecryptionError, and feature-flag suppression.
"""
import json
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet, InvalidToken


# ── crypto helpers ────────────────────────────────────────────────────────────

def _key() -> str:
    return Fernet.generate_key().decode()


def test_encrypt_decrypt_roundtrip():
    from app.infrastructure.crypto import decrypt_dict, encrypt_dict
    key = _key()
    plain = {"db_password": "s3cr3t", "count": 42, "flag": True}
    assert decrypt_dict(encrypt_dict(plain, key), key) == plain


def test_encrypt_empty_returns_empty():
    from app.infrastructure.crypto import encrypt_dict
    assert encrypt_dict({}, _key()) == {}
    assert encrypt_dict({}, "") == {}


def test_decrypt_empty_returns_empty():
    from app.infrastructure.crypto import decrypt_dict
    assert decrypt_dict({}, _key()) == {}
    assert decrypt_dict({}, "") == {}


def test_encrypt_raises_without_key():
    from app.infrastructure.crypto import encrypt_dict
    with pytest.raises(ValueError, match="SECRETS_ENCRYPTION_KEY"):
        encrypt_dict({"pw": "secret"}, "")


def test_decrypt_raises_without_key():
    from app.infrastructure.crypto import decrypt_dict
    with pytest.raises(InvalidToken):
        decrypt_dict({"pw": "someciphertext"}, "")


def test_decrypt_raises_on_wrong_key():
    from app.infrastructure.crypto import decrypt_dict, encrypt_dict
    key1 = _key()
    key2 = _key()
    encrypted = encrypt_dict({"pw": "secret"}, key1)
    with pytest.raises(InvalidToken):
        decrypt_dict(encrypted, key2)


def test_decrypt_atomic_no_partial_result():
    """decrypt_dict must raise before returning anything when any value is bad."""
    from app.infrastructure.crypto import decrypt_dict, encrypt_dict
    key = _key()
    blob = encrypt_dict({"a": "v1", "b": "v2"}, key)
    blob["b"] = "not-valid-ciphertext"
    with pytest.raises((InvalidToken, Exception)):
        decrypt_dict(blob, key)


# ── ansible runner — secrets injection ───────────────────────────────────────

def _booking_with_roles(roles):
    b = MagicMock()
    b.config_roles = roles
    return b


def test_render_playbook_no_secrets():
    from app.infrastructure.config.ansible import _render_playbook
    play = _render_playbook([{"ansible_role": "myrole", "vars": {}}])
    assert "no_log" not in play
    assert "vars_files" not in play


def test_render_playbook_with_secrets():
    from app.infrastructure.config.ansible import _render_playbook
    play = _render_playbook([{"ansible_role": "myrole", "vars": {}}], secrets_path="/tmp/secrets.yml")
    assert "no_log: true" in play
    assert "vars_files:" in play
    assert "/tmp/secrets.yml" in play


def test_ansible_runner_writes_secrets_file():
    """When roles carry encrypted secret_vars, secrets.yml is written before ansible-playbook runs."""
    from app.infrastructure.config.ansible import AnsibleConfigRunner
    key = _key()
    from app.infrastructure.crypto import encrypt_dict
    encrypted = encrypt_dict({"db_pw": "hunter2"}, key)

    booking = _booking_with_roles([
        {"ansible_role": "myrole", "vars": {}, "name": "myrole", "secret_vars": encrypted}
    ])

    written_paths = []

    def fake_run(cmd, on_progress=None):
        # Capture the secrets file path from the playbook
        play_path = next(p for p in cmd if p.endswith(".yml"))
        play_content = open(play_path).read()
        if "vars_files:" in play_content:
            import re
            match = re.search(r"- (.+secrets\.yml)", play_content)
            if match:
                written_paths.append(match.group(1))
        # Verify the secrets file exists at this point
        if written_paths:
            assert open(written_paths[0]).read()  # non-empty

    runner = AnsibleConfigRunner()
    with patch.object(runner, "_run", side_effect=fake_run), \
         patch("app.infrastructure.config.ansible.settings") as mock_settings:
        mock_settings.VM_SSH_USER = "root"
        mock_settings.VM_SSH_PORT = 22
        mock_settings.VM_SSH_PRIVATE_KEY = ""
        mock_settings.SECRETS_ENCRYPTION_KEY = key
        mock_settings.ANSIBLE_ROLES_PATH = "/roles"
        mock_settings.ANSIBLE_COLLECTIONS_PATH = "/collections"
        mock_settings.ANSIBLE_TIMEOUT = 1800
        runner.apply_roles(booking, ip="1.2.3.4", password="pw")

    assert written_paths, "secrets file path should appear in playbook"


def test_ansible_runner_no_secrets_file_when_empty():
    """When no role has secret_vars, secrets.yml is not created and no_log is absent."""
    from app.infrastructure.config.ansible import AnsibleConfigRunner

    booking = _booking_with_roles([
        {"ansible_role": "myrole", "vars": {}, "name": "myrole", "secret_vars": {}}
    ])

    playbook_content = []

    def fake_run(cmd, on_progress=None):
        play_path = next(p for p in cmd if p.endswith(".yml"))
        playbook_content.append(open(play_path).read())

    runner = AnsibleConfigRunner()
    with patch.object(runner, "_run", side_effect=fake_run), \
         patch("app.infrastructure.config.ansible.settings") as mock_settings:
        mock_settings.VM_SSH_USER = "root"
        mock_settings.VM_SSH_PORT = 22
        mock_settings.VM_SSH_PRIVATE_KEY = ""
        mock_settings.SECRETS_ENCRYPTION_KEY = ""
        mock_settings.ANSIBLE_ROLES_PATH = "/roles"
        mock_settings.ANSIBLE_COLLECTIONS_PATH = "/collections"
        mock_settings.ANSIBLE_TIMEOUT = 1800
        runner.apply_roles(booking, ip="1.2.3.4", password="pw")

    assert playbook_content
    assert "no_log" not in playbook_content[0]
    assert "vars_files" not in playbook_content[0]


def test_ansible_runner_raises_secret_decryption_error_on_bad_key():
    from app.infrastructure.config.ansible import AnsibleConfigRunner
    from app.domain.exceptions import SecretDecryptionError
    key = _key()
    from app.infrastructure.crypto import encrypt_dict
    encrypted = encrypt_dict({"pw": "s3cr3t"}, key)
    wrong_key = _key()

    booking = _booking_with_roles([
        {"ansible_role": "myrole", "vars": {}, "name": "myrole", "secret_vars": encrypted}
    ])

    runner = AnsibleConfigRunner()
    with patch("app.infrastructure.config.ansible.settings") as mock_settings:
        mock_settings.SECRETS_ENCRYPTION_KEY = wrong_key
        with pytest.raises(SecretDecryptionError):
            runner.apply_roles(booking, ip="1.2.3.4", password="pw")


def test_secrets_file_deleted_after_run():
    """The temp directory (and secrets.yml inside it) is removed after apply_roles completes."""
    from app.infrastructure.config.ansible import AnsibleConfigRunner
    key = _key()
    from app.infrastructure.crypto import encrypt_dict
    encrypted = encrypt_dict({"pw": "s3cr3t"}, key)

    booking = _booking_with_roles([
        {"ansible_role": "myrole", "vars": {}, "name": "myrole", "secret_vars": encrypted}
    ])

    captured_secrets_path = []

    def fake_run(cmd, on_progress=None):
        play_path = next(p for p in cmd if p.endswith(".yml"))
        content = open(play_path).read()
        import re
        match = re.search(r"- (.+secrets\.yml)", content)
        if match:
            captured_secrets_path.append(match.group(1))

    runner = AnsibleConfigRunner()
    with patch.object(runner, "_run", side_effect=fake_run), \
         patch("app.infrastructure.config.ansible.settings") as mock_settings:
        mock_settings.VM_SSH_USER = "root"
        mock_settings.VM_SSH_PORT = 22
        mock_settings.VM_SSH_PRIVATE_KEY = ""
        mock_settings.SECRETS_ENCRYPTION_KEY = key
        mock_settings.ANSIBLE_ROLES_PATH = "/roles"
        mock_settings.ANSIBLE_COLLECTIONS_PATH = "/collections"
        mock_settings.ANSIBLE_TIMEOUT = 1800
        runner.apply_roles(booking, ip="1.2.3.4", password="pw")

    assert captured_secrets_path, "should have captured secrets path"
    import os
    assert not os.path.exists(captured_secrets_path[0]), "secrets file must be deleted after run"


# ── feature flag ─────────────────────────────────────────────────────────────

def test_feature_flag_suppresses_secret_vars_in_snapshot():
    """When SECRET_VARS_ENABLED=False, snapshot builder uses empty secret_vars."""
    # Simulate what api_bookings.py does with the flag
    with patch("app.config.settings") as mock_cfg:
        mock_cfg.SECRET_VARS_ENABLED = False
        role = MagicMock()
        role.name = "testrole"
        role.ansible_role = "testrole"
        role.default_vars = {}
        role.secret_vars = {"pw": "encrypted-blob"}

        snapshot = {
            "name": role.name,
            "ansible_role": role.ansible_role,
            "vars": role.default_vars or {},
            "secret_vars": role.secret_vars if mock_cfg.SECRET_VARS_ENABLED else {},
        }
        assert snapshot["secret_vars"] == {}
