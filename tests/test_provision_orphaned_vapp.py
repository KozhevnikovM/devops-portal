"""Regression tests for recovering provisioning from an orphaned resource after a reboot.

A reboot during `terraform apply` can leave a resource created in VCD but absent from state, so the
next apply re-plans the create and VCD rejects it. Two shapes, both self-healed the same way — make
sure the vApp is in state, destroy it (clearing the vApp + any partial children), apply fresh:

- #197: the vApp itself is orphaned — "entity portal-... already exists". The vApp is not in state,
  so the adapter imports it before destroying.
- #415: the vApp is tracked but the VM inside it is orphaned — "There is already a VM named ...".
  The vApp is already in state, so the adapter skips the import and goes straight to destroy.
"""
import asyncio
from unittest.mock import patch

import pytest

from app.infrastructure.terraform.vcd_adapter import TerraformError, TerraformVcdAdapter

VAPP_ALREADY_EXISTS = (
    "terraform apply failed (exit 1):\n"
    "Error: error creating vApp portal-abc: error executing task request: "
    "error instantiating a new vApp:: API Error: 400: "
    "The VMware Cloud Director entity portal-abc already exists."
)
VM_ALREADY_EXISTS = (
    "terraform apply failed (exit 1):\n"
    "module.vm.vcd_vapp_vm.vm: Creating...\n"
    "Error: error creating VM from template: [VM creation] error getting VM portal-abc : "
    "error instantiating a new VM: API Error: 400: "
    'There is already a VM named "portal-abc".'
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


def _patched_settings():
    return patch("app.infrastructure.terraform.vcd_adapter.settings")


def _configure(s):
    s.VCD_ORG = "my-org"
    s.VCD_VDC = "my-vdc"
    s.TF_APPLY_REFRESH = True
    s.TF_APPLY_PARALLELISM = 1
    s.TF_WORKSPACES_DIR = "/tmp/tf-workspaces"


def test_apply_recovers_from_orphaned_vapp():
    """#197 — vApp orphaned (not in state): import, then destroy + recreate."""
    adapter = TerraformVcdAdapter()
    calls: list[tuple] = []

    async def fake_run(*args, cwd=None, on_progress=None, **kwargs):
        calls.append(args)
        # The first apply hits the orphaned-vApp conflict; everything after succeeds.
        if args[0] == "apply" and len([c for c in calls if c[0] == "apply"]) == 1:
            raise TerraformError(VAPP_ALREADY_EXISTS)
        if args[:2] == ("state", "list"):
            return ""  # vApp not in state → adapter imports it before destroying
        if args[0] == "output":
            return '{"primary_ip": {"value": "10.0.0.5"}}'
        return ""

    with patch.object(adapter, "_write_workspace"), _patched_settings() as s:
        _configure(s)
        adapter._run = fake_run
        result = _apply(adapter)

    assert result == {"ip": "10.0.0.5"}
    verbs = [c[0] for c in calls]
    # init, select, apply(fails) -> state list -> import -> destroy -> apply(ok) -> output
    assert verbs == ["init", "workspace", "apply", "state", "import", "destroy", "apply", "output"]
    imp = next(c for c in calls if c[0] == "import")
    assert imp == ("import", "-no-color", "vcd_vapp.this", "my-org.my-vdc.portal-abc")


def test_apply_recovers_from_orphaned_vm():
    """#415 — VM orphaned inside a tracked vApp: skip import, destroy + recreate."""
    adapter = TerraformVcdAdapter()
    calls: list[tuple] = []

    async def fake_run(*args, cwd=None, on_progress=None, **kwargs):
        calls.append(args)
        if args[0] == "apply" and len([c for c in calls if c[0] == "apply"]) == 1:
            raise TerraformError(VM_ALREADY_EXISTS)
        if args[:2] == ("state", "list"):
            return "vcd_vapp.this\nvcd_vapp_org_network.this"  # vApp already tracked → no import
        if args[0] == "output":
            return '{"primary_ip": {"value": "10.0.0.9"}}'
        return ""

    with patch.object(adapter, "_write_workspace"), _patched_settings() as s:
        _configure(s)
        adapter._run = fake_run
        result = _apply(adapter)

    assert result == {"ip": "10.0.0.9"}
    verbs = [c[0] for c in calls]
    # init, select, apply(fails) -> state list -> destroy -> apply(ok) -> output — NO import
    assert verbs == ["init", "workspace", "apply", "state", "destroy", "apply", "output"]
    assert "import" not in verbs


def test_apply_non_conflict_error_propagates():
    adapter = TerraformVcdAdapter()
    calls: list[tuple] = []

    async def fake_run(*args, cwd=None, on_progress=None, **kwargs):
        calls.append(args)
        if args[0] == "apply":
            raise TerraformError("terraform apply failed (exit 1):\nError: quota exceeded in vDC")
        return ""

    with patch.object(adapter, "_write_workspace"), _patched_settings() as s:
        _configure(s)
        adapter._run = fake_run
        with pytest.raises(TerraformError):
            _apply(adapter)

    # No orphan recovery for an unrelated apply failure.
    verbs = [c[0] for c in calls]
    assert "state" not in verbs
    assert "import" not in verbs
    assert "destroy" not in verbs


def test_apply_conflict_for_other_resource_propagates():
    """An already-exists conflict naming a *different* resource is not ours to reconcile."""
    adapter = TerraformVcdAdapter()
    calls: list[tuple] = []

    async def fake_run(*args, cwd=None, on_progress=None, **kwargs):
        calls.append(args)
        if args[0] == "apply":
            raise TerraformError(
                "terraform apply failed (exit 1):\n"
                'Error: ... There is already a VM named "portal-someone-else".'
            )
        return ""

    with patch.object(adapter, "_write_workspace"), _patched_settings() as s:
        _configure(s)
        adapter._run = fake_run
        with pytest.raises(TerraformError):
            _apply(adapter)

    verbs = [c[0] for c in calls]
    assert "state" not in verbs
    assert "import" not in verbs
    assert "destroy" not in verbs
