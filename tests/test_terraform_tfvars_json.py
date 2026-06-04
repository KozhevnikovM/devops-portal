"""Regression test for #145 — Terraform vars are written as injection-proof JSON.

Before the fix, terraform.tfvars was built by f-string interpolation into quoted HCL, so a value
containing a double quote could break out and inject HCL. After the fix the vars are written to
terraform.tfvars.json via json.dump, which escapes every value.
"""
import json

from app.infrastructure.terraform.vcd_adapter import TerraformVcdAdapter


def test_write_workspace_emits_tfvars_json_with_escaped_values(tmp_path):
    adapter = TerraformVcdAdapter()
    config = {
        "name": 'portal-abc',
        # admin free-text + a password, both containing characters that would break HCL quoting
        "vapp_template_id": 'urn:vcloud:vapptemplate:abc" injected = "pwn',
        "cpus": 2,
        "memory": 4096,
        "disk_size": 26624,
        "vm_password": 'p@ss"word\nwith-quote',
    }

    adapter._write_workspace(tmp_path, config, api_token="tok")

    # New format: JSON, not a hand-built terraform.tfvars.
    json_file = tmp_path / "terraform.tfvars.json"
    assert json_file.exists()
    assert not (tmp_path / "terraform.tfvars").exists()

    data = json.loads(json_file.read_text())
    # Values round-trip exactly — no breakout, no extra injected keys.
    assert data["vapp_template_id"] == config["vapp_template_id"]
    assert data["name"] == "portal-abc"
    assert data["cpus"] == 2
    assert data["customization"]["admin_password"] == config["vm_password"]
    assert "injected" not in data  # the quote did not create a new top-level variable
