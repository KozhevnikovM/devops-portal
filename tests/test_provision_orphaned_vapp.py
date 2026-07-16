"""Regression test for #197 — recover provisioning from an orphaned vApp after a reboot.

A reboot during `terraform apply` can leave the vApp created in VCD but absent from state, so the
next apply re-plans the create and VCD rejects it: "entity portal-... already exists". The adapter
self-heals: import the orphan, destroy it (clearing the vApp + any partial children), apply fresh.
"""
import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from app.infrastructure.terraform.vcd_adapter import TerraformError, TerraformVcdAdapter

ALREADY_EXISTS = (
    "terraform apply failed (exit 1):\n"
    "Error: error creating vApp portal-abc: error executing task request: "
    "error instantiating a new vApp:: API Error: 400: "
    "The VMware Cloud Director entity portal-abc already exists."
)
CONFIG = {
    "name": "portal-abc",
    "vapp_template_id": "tpl-1",
    "cpus": 2,
    "memory": 4096,
    "disk_size": 26624,
    "vm_password": "pw",
}


def _apply(adapter):
    return asyncio.run(adapter.apply("booking-abc", CONFIG, api_token="tok"))


def test_apply_recovers_from_orphaned_vapp():
    adapter = TerraformVcdAdapter()
    calls: list[tuple] = []

    async def fake_run(*args, cwd=None, on_progress=None, **kwargs):
        calls.append(args)
        # The first apply hits the orphaned-vApp conflict; everything after succeeds.
        if args[0] == "apply" and len([c for c in calls if c[0] == "apply"]) == 1:
            raise TerraformError(ALREADY_EXISTS)
        if args[0] == "output":
            return '{"primary_ip": {"value": "10.0.0.5"}}'
        return ""

    with patch.object(adapter, "_write_workspace"), \
         patch("app.infrastructure.terraform.vcd_adapter.settings") as s:
        s.VCD_ORG = "my-org"
        s.VCD_VDC = "my-vdc"
        s.TF_APPLY_REFRESH = True
        s.TF_APPLY_PARALLELISM = 1
        s.TF_WORKSPACES_DIR = "/tmp/tf-workspaces"
        adapter._run = fake_run
        result = _apply(adapter)

    assert result == {"ip": "10.0.0.5"}
    verbs = [c[0] for c in calls]
    # init, select, apply(fails) -> import -> destroy -> apply(ok) -> output
    assert verbs == ["init", "workspace", "apply", "import", "destroy", "apply", "output"]
    imp = next(c for c in calls if c[0] == "import")
    assert imp == ("import", "-no-color", "vcd_vapp.this", "my-org.my-vdc.portal-abc")


def test_apply_non_conflict_error_propagates():
    adapter = TerraformVcdAdapter()
    calls: list[tuple] = []

    async def fake_run(*args, cwd=None, on_progress=None, **kwargs):
        calls.append(args)
        if args[0] == "apply":
            raise TerraformError("terraform apply failed (exit 1):\nError: quota exceeded in vDC")
        return ""

    with patch.object(adapter, "_write_workspace"), \
         patch("app.infrastructure.terraform.vcd_adapter.settings") as s:
        s.VCD_ORG = "my-org"
        s.VCD_VDC = "my-vdc"
        s.TF_APPLY_REFRESH = True
        s.TF_APPLY_PARALLELISM = 1
        s.TF_WORKSPACES_DIR = "/tmp/tf-workspaces"
        adapter._run = fake_run
        with pytest.raises(TerraformError):
            _apply(adapter)

    # No orphan recovery for an unrelated apply failure.
    assert "import" not in [c[0] for c in calls]
    assert "destroy" not in [c[0] for c in calls]
