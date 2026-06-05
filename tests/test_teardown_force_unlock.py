"""Regression test for #180 — VM release force-unlocks a stale Terraform state lock.

Before the fix, an interrupted apply/destroy could leave the per-booking PG backend holding a
stale lock; every later `terraform destroy` then failed on "Error acquiring the state lock" and
the booking could never be released. After the fix, `_destroy_state` detects the lock error,
parses the lock id, runs `terraform force-unlock -force <id>`, and retries the destroy once.
"""
import asyncio
from pathlib import Path

import pytest

from app.infrastructure.terraform.vcd_adapter import TerraformError, TerraformVcdAdapter

LOCK_ERROR = """\
Error: Error acquiring the state lock

Error message: ResourceExists
Lock Info:
  ID:        9b3f1c4e-1a2b-4c3d-8e9f-0a1b2c3d4e5f
  Path:      tfstate/booking-abc
  Operation: OperationTypeApply
  Who:       portal@worker
"""


def _run_destroy(adapter, calls):
    """Drive _destroy_state and record every terraform invocation in `calls`."""
    asyncio.run(adapter._destroy_state("booking-abc", Path("/tmp/ws"), on_progress=None))


def test_destroy_force_unlocks_stale_lock_then_retries():
    adapter = TerraformVcdAdapter()
    calls: list[tuple] = []

    async def fake_run(*args, cwd=None, on_progress=None):
        calls.append(args)
        # First destroy hits the stale lock; everything after it succeeds.
        if args[0] == "destroy" and len([c for c in calls if c[0] == "destroy"]) == 1:
            raise TerraformError(f"terraform destroy failed (exit 1):\n{LOCK_ERROR}")
        return ""

    adapter._run = fake_run
    _run_destroy(adapter, calls)

    verbs = [c[0] for c in calls]
    assert verbs == ["destroy", "force-unlock", "destroy"]
    # The exact stale lock id was force-unlocked, with -force.
    unlock = next(c for c in calls if c[0] == "force-unlock")
    assert unlock == ("force-unlock", "-force", "9b3f1c4e-1a2b-4c3d-8e9f-0a1b2c3d4e5f")


def test_destroy_without_lock_never_force_unlocks():
    adapter = TerraformVcdAdapter()
    calls: list[tuple] = []

    async def fake_run(*args, cwd=None, on_progress=None):
        calls.append(args)
        return ""

    adapter._run = fake_run
    _run_destroy(adapter, calls)

    verbs = [c[0] for c in calls]
    assert verbs == ["destroy"]
    assert "force-unlock" not in verbs


def test_destroy_non_lock_error_propagates_unchanged():
    adapter = TerraformVcdAdapter()
    calls: list[tuple] = []

    async def fake_run(*args, cwd=None, on_progress=None):
        calls.append(args)
        raise TerraformError("terraform destroy failed (exit 1):\nError: vApp not found (VCD-1041)")

    adapter._run = fake_run
    with pytest.raises(TerraformError):
        _run_destroy(adapter, calls)

    # No recovery attempted for a non-lock failure.
    assert [c[0] for c in calls] == ["destroy"]
