"""Regression tests for S6/#302: provider credentials must not appear in generated HCL.

Before the fix, VCD_PASSWORD and api_token were interpolated directly into main.tf.
After the fix they are passed as environment variables (VCD_TOKEN / VCD_USER+VCD_PASSWORD)
to the terraform subprocess so the on-disk file never contains a secret.
"""
import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.infrastructure.terraform.vcd_adapter import TerraformVcdAdapter


_DUMMY_CONFIG = {
    "name": "portal-test",
    "vapp_template_id": "urn:vcloud:vapptemplate:abc",
    "cpus": 2,
    "memory": 4096,
    "disk_size": 26624,
    "vm_password": "vm-secret-pw",
}


# ── _provider_block: no secrets in the generated HCL ─────────────────────────

def test_provider_block_token_auth_omits_token_value(tmp_path):
    """api_token value must not appear in the generated provider block."""
    adapter = TerraformVcdAdapter()
    secret_token = "vcd-api-token-super-secret"
    block = adapter._provider_block(api_token=secret_token)
    assert secret_token not in block
    assert 'auth_type            = "api_token"' in block


def test_provider_block_token_auth_omits_placeholder_password():
    """The provider block for token auth keeps user/password as literal 'none' (not a real secret)."""
    adapter = TerraformVcdAdapter()
    block = adapter._provider_block(api_token="some-token")
    # 'none' placeholder must be present (required by provider), real password must not
    assert 'user                 = "none"' in block
    assert 'password             = "none"' in block


def test_provider_block_integrated_auth_omits_password():
    """Real VCD_USER and VCD_PASSWORD must not appear in the integrated-auth provider block."""
    adapter = TerraformVcdAdapter()
    real_user = "vcd-admin-user"
    real_password = "vcd-secret-password"
    with patch("app.infrastructure.terraform.vcd_adapter.settings") as s:
        s.VCD_URL = "https://vcd.example.com/api"
        s.VCD_ORG = "my-org"
        s.VCD_VDC = "my-vdc"
        s.VCD_USER = real_user
        s.VCD_PASSWORD = real_password
        s.VCD_API_TOKEN = ""
        s.VCD_ALLOW_UNVERIFIED_SSL = False
        block = adapter._provider_block(api_token=None)

    assert real_user not in block
    assert real_password not in block
    assert 'auth_type            = "integrated"' in block


# ── _write_workspace: no secrets in main.tf ──────────────────────────────────

def test_write_workspace_token_auth_secret_not_in_main_tf(tmp_path):
    """After _write_workspace, main.tf must not contain the api_token value."""
    secret_token = "vcd-api-token-should-not-appear"
    adapter = TerraformVcdAdapter()
    with patch("app.infrastructure.terraform.vcd_adapter.settings") as s:
        s.VCD_URL = "https://vcd.example.com/api"
        s.VCD_ORG = "org"
        s.VCD_VDC = "vdc"
        s.VCD_API_TOKEN = ""
        s.VCD_ALLOW_UNVERIFIED_SSL = False
        s.TF_PG_CONN_STR = "postgresql://portal:portal@postgres/portal"
        s.TF_MODULE_SOURCE = "/app/terraform/modules/vapp_vm"
        s.VCD_NETWORK_NAME = "default-network"
        adapter._write_workspace(tmp_path, _DUMMY_CONFIG, api_token=secret_token)

    main_tf = (tmp_path / "main.tf").read_text()
    assert secret_token not in main_tf


def test_write_workspace_integrated_auth_secrets_not_in_main_tf(tmp_path):
    """After _write_workspace, main.tf must not contain VCD_USER or VCD_PASSWORD."""
    real_user = "vcd-admin"
    real_password = "vcd-password-secret"
    adapter = TerraformVcdAdapter()
    with patch("app.infrastructure.terraform.vcd_adapter.settings") as s:
        s.VCD_URL = "https://vcd.example.com/api"
        s.VCD_ORG = "org"
        s.VCD_VDC = "vdc"
        s.VCD_API_TOKEN = ""
        s.VCD_USER = real_user
        s.VCD_PASSWORD = real_password
        s.VCD_ALLOW_UNVERIFIED_SSL = False
        s.TF_PG_CONN_STR = "postgresql://portal:portal@postgres/portal"
        s.TF_MODULE_SOURCE = "/app/terraform/modules/vapp_vm"
        s.VCD_NETWORK_NAME = "default-network"
        adapter._write_workspace(tmp_path, _DUMMY_CONFIG, api_token=None)

    main_tf = (tmp_path / "main.tf").read_text()
    assert real_user not in main_tf
    assert real_password not in main_tf


# ── _cred_env: correct env vars built ────────────────────────────────────────

def test_cred_env_token_auth_returns_vcd_token():
    adapter = TerraformVcdAdapter()
    with patch("app.infrastructure.terraform.vcd_adapter.settings") as s:
        s.VCD_API_TOKEN = ""
        env = adapter._cred_env(api_token="my-token")
    assert env == {"VCD_API_TOKEN": "my-token"}


def test_cred_env_token_falls_back_to_settings_vcd_api_token():
    adapter = TerraformVcdAdapter()
    with patch("app.infrastructure.terraform.vcd_adapter.settings") as s:
        s.VCD_API_TOKEN = "settings-token"
        env = adapter._cred_env(api_token=None)
    assert env == {"VCD_API_TOKEN": "settings-token"}


def test_cred_env_integrated_auth_returns_user_and_password():
    adapter = TerraformVcdAdapter()
    with patch("app.infrastructure.terraform.vcd_adapter.settings") as s:
        s.VCD_API_TOKEN = ""
        s.VCD_USER = "admin"
        s.VCD_PASSWORD = "secret"
        env = adapter._cred_env(api_token=None)
    assert env == {"VCD_USER": "admin", "VCD_PASSWORD": "secret"}


# ── _run: extra_env is merged into subprocess env ────────────────────────────

@pytest.mark.asyncio
async def test_run_passes_extra_env_to_subprocess(tmp_path):
    """Credentials supplied via extra_env must reach the subprocess environment."""
    adapter = TerraformVcdAdapter()
    captured_env = {}

    class _EmptyStdout:
        def __aiter__(self): return self
        async def __anext__(self): raise StopAsyncIteration

    async def fake_exec(*args, env=None, **kwargs):
        captured_env.update(env or {})
        proc = MagicMock()
        proc.stdout = _EmptyStdout()
        proc.wait = AsyncMock()
        proc.returncode = 0
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await adapter._run("version", cwd=tmp_path, extra_env={"VCD_TOKEN": "secret-token"})

    assert captured_env.get("VCD_TOKEN") == "secret-token"
    assert "TF_CLI_CONFIG_FILE" in captured_env  # base env preserved
