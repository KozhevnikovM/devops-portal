## Context

`TerraformVcdAdapter.apply()` (`app/infrastructure/terraform/vcd_adapter.py`) already recovers from one flavor of orphan: a vApp that exists in VCD but not in state, detected by `_ALREADY_EXISTS_RE = re.compile(r"entity (\S+) already exists")` and `_is_orphaned_vapp()`. The recovery is: `import vcd_vapp.this <org.vdc.name>` → `_destroy_state()` → `_apply()` again. This was built for #197 and is covered by `tests/test_provision_orphaned_vapp.py`.

Issue #415 is a sibling failure the current code misses. The apply trace shows `vcd_vapp.this` and `vcd_vapp_org_network.this` refreshing from state successfully, while `module.vm.vcd_vapp_vm.vm` is planned as `+ create` and VCD rejects it: `API Error: 400: ... There is already a VM named "portal-c3a62046"`. The vApp is in state; only the VM is orphaned. Two problems block the existing recovery from firing:

1. The VM-conflict message (`There is already a VM named "<name>"`) does not match `_ALREADY_EXISTS_RE`, so `_is_orphaned_vapp()` returns False and the error propagates.
2. Even if it matched, the recovery unconditionally runs `import vcd_vapp.this`, which errors when the vApp is already managed ("Resource already managed by Terraform").

See proposal.md — Why for user impact.

## Goals / Non-Goals

**Goals:**
- Recover the orphaned-VM case (vApp tracked, VM in VCD but not in state) via the same destroy-and-recreate flow, keeping a single general recovery path for "our resource exists in VCD but not in state."
- Only import the vApp when it is not already managed, so recovery works in both the vApp-orphan and VM-orphan cases.
- Preserve existing behavior: unrelated apply failures and already-exists conflicts for a *different* resource name still propagate untouched.

**Non-Goals:**
- No change to how the orphaned VM is physically removed: destroying the tracked vApp in VCD already cascades to delete the VM inside it (the existing destroy comment already relies on this). We do not add per-VM import/destroy.
- No change to the provision task's retry/FAILED state machine, credentials handling, or the destroy lock-recovery logic.
- No new configuration or API surface.

## Decisions

**1. Broaden conflict detection instead of adding a parallel path.**
Replace the single-purpose `_is_orphaned_vapp(message, vapp_name)` with a check that recognizes an orphaned-resource conflict for *our own name* from either message form:
- vApp: `entity <name> already exists`
- VM: `There is already a VM named "<name>"`

Both indicate the same underlying condition — a resource we own exists in VCD but Terraform doesn't know about it — and both are reconciled by the same destroy-and-recreate. Keeping one predicate avoids drift between two nearly-identical branches. Alternative considered: a second `except`/branch specific to VMs — rejected as duplicated recovery logic that would diverge over time.

**2. Make the vApp import conditional on state membership.**
Before importing, check whether `vcd_vapp.this` is already in state (e.g. `terraform state list`). Import only when absent. In the VM-orphan case the vApp is already tracked, so we skip straight to `destroy` + `apply`; in the vApp-orphan case we import first, exactly as today. This is the minimal change that makes the shared path correct for both. Alternative considered: always attempt import and swallow the "already managed" error — rejected as relying on parsing yet another error string and masking genuine import failures.

**3. Reuse `_destroy_state()` + `_apply()` unchanged.**
Destroying the tracked vApp removes it (and, in VCD, everything inside it, including the orphaned VM) and clears state; the subsequent `_apply()` recreates from a clean slate. This is the same two-step the vApp-orphan recovery already performs, so no new teardown code is needed.

## Risks / Trade-offs

- [The orphaned VM is not inside our tracked vApp] → In practice the portal always creates the VM inside the same-named vApp it owns (`vapp_name = var.name`), so destroying the vApp removes it. If a stray VM of the same name existed outside our vApp, destroy would not clear it and the conflict would persist; that is out of scope (it would indicate external tampering with a portal-owned name).
- [Destroy-and-recreate discards a partially-provisioned VM] → Intended: the VM is unconfigured and unusable at this point, and recreating yields a known-good VM. The booking's lease only starts at READY, so no user-visible resource is lost.
- [Matching on VCD error text is brittle if the provider reword happens] → Same existing risk as the current vApp matcher; mitigated by anchoring on the stable `already exists` / `already a VM named` phrasing and by the regression tests. A miss degrades to the current behavior (propagate → retry/FAILED), not to a worse state.

## Migration Plan

Pure code change to the adapter plus a regression test. No data migration, no config, no rollback steps beyond reverting the commit. Deploys with the normal worker image; takes effect on the next provision retry for any currently-stuck booking.
