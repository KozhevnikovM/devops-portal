## 1. Broaden orphaned-resource detection

- [x] 1.1 Add a VM-name conflict pattern (`There is already a VM named "<name>"`) alongside the existing `entity <name> already exists` vApp pattern in `app/infrastructure/terraform/vcd_adapter.py` — `_VM_ALREADY_EXISTS_RE`
- [x] 1.2 Replace `_is_orphaned_vapp(message, vapp_name)` with a predicate that returns True when either conflict form names this booking's own resource, and False otherwise (including already-exists conflicts for a different name) — `_is_orphaned_resource`

## 2. Generalize the recovery in apply()

- [x] 2.1 In `apply()`, detect whether `vcd_vapp.this` is already tracked in state (`_vapp_in_state` via `terraform state list`) and import the vApp only when it is not already managed
- [x] 2.2 Keep the destroy-then-reapply flow (`_destroy_state()` → `_apply()`) as the shared reconciliation for both the vApp-orphan and VM-orphan cases
- [x] 2.3 Ensure non-recoverable failures (unrelated errors, conflicts for a different resource name) still propagate without import/destroy/reapply

## 3. Tests

- [x] 3.1 Add a regression test for the VM-name conflict: apply fails with `There is already a VM named "<name>"` while the vApp is already in state → adapter destroys + reapplies (no re-import) and returns the primary IP — `test_apply_recovers_from_orphaned_vm`
- [x] 3.2 Keep/verify the existing orphaned-vApp test still passes (import → destroy → reapply path unchanged; now preceded by the state-list check) — `test_apply_recovers_from_orphaned_vapp`
- [x] 3.3 Verify the "unrelated apply failure" and "different resource name" cases do not trigger recovery — `test_apply_non_conflict_error_propagates`, `test_apply_conflict_for_other_resource_propagates`
- [x] 3.4 Run the affected test module and confirm it passes at runtime (4 passed); confirmed the new VM-orphan test fails against the pre-fix adapter and passes after

## 4. Wrap-up

- [x] 4.1 Run the `py-review` quality gate on the changed Python
- [x] 4.2 Confirm no user-facing docs (`docs/admin-guide.md`, `docs/api-reference.md`) need changes — this fix has no API/CLI/workflow surface
