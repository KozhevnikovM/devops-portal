## 1. Broaden orphaned-resource detection

- [ ] 1.1 Add a VM-name conflict pattern (`There is already a VM named "<name>"`) alongside the existing `entity <name> already exists` vApp pattern in `app/infrastructure/terraform/vcd_adapter.py`
- [ ] 1.2 Replace `_is_orphaned_vapp(message, vapp_name)` with a predicate that returns True when either conflict form names this booking's own resource, and False otherwise (including already-exists conflicts for a different name)

## 2. Generalize the recovery in apply()

- [ ] 2.1 In `apply()`, detect whether `vcd_vapp.this` is already tracked in state (e.g. via `terraform state list`) and import the vApp only when it is not already managed
- [ ] 2.2 Keep the destroy-then-reapply flow (`_destroy_state()` → `_apply()`) as the shared reconciliation for both the vApp-orphan and VM-orphan cases
- [ ] 2.3 Ensure non-recoverable failures (unrelated errors, conflicts for a different resource name) still propagate without import/destroy/reapply

## 3. Tests

- [ ] 3.1 Add a regression test for the VM-name conflict: apply fails with `There is already a VM named "<name>"` while the vApp is already in state → adapter destroys + reapplies (no re-import) and returns the primary IP
- [ ] 3.2 Keep/verify the existing orphaned-vApp test still passes (import → destroy → reapply path unchanged)
- [ ] 3.3 Verify the "unrelated apply failure" and "different resource name" cases do not trigger recovery
- [ ] 3.4 Run the affected test module and confirm it passes at runtime (not just static checks)

## 4. Wrap-up

- [ ] 4.1 Run the `py-review` quality gate on the changed Python
- [ ] 4.2 Confirm no user-facing docs (`docs/admin-guide.md`, `docs/api-reference.md`) need changes — this fix has no API/CLI/workflow surface
