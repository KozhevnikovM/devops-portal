## Purpose

Defines how a VM booking's Terraform provisioning recovers when a cloud resource already exists in VMware Cloud Director but is absent from Terraform state, so an interrupted apply self-heals on retry instead of failing permanently.

## ADDED Requirements

### Requirement: Recover from an orphaned resource on apply

When a `terraform apply` for a VM booking fails because a resource the apply is trying to create already exists in VCD under this booking's own name — but is absent from Terraform state — the provisioning adapter SHALL reconcile the orphan and complete the apply, rather than propagating the failure. This covers both an orphaned vApp and an orphaned VM inside an otherwise-tracked vApp, which arise when an apply is killed after creating a resource in VCD but before persisting it to state.

Reconciliation SHALL leave the booking's workspace in a clean, fully-applied state: the recovered resources are removed from VCD and recreated so that Terraform state and VCD agree.

#### Scenario: Orphaned vApp (vApp exists in VCD, absent from state)
- **WHEN** an apply fails with a VCD conflict that the vApp for this booking already exists (e.g. `entity <name> already exists`)
- **THEN** the adapter imports the existing vApp into state, destroys it (clearing the vApp and any partial resources inside it), and applies again
- **AND** the apply completes and returns the provisioned VM's primary IP

#### Scenario: Orphaned VM (vApp tracked in state, VM exists in VCD but absent from state)
- **WHEN** an apply fails with a VCD conflict that a VM for this booking already exists (e.g. `There is already a VM named "<name>"`) while the vApp is already tracked in state
- **THEN** the adapter destroys the tracked vApp (which cascades in VCD to remove the orphaned VM inside it) without attempting to re-import the already-managed vApp, and applies again
- **AND** the apply completes and returns the provisioned VM's primary IP

#### Scenario: The conflict is only recovered for this booking's own resource
- **WHEN** an apply fails with an already-exists conflict whose resource name is not this booking's own resource name
- **THEN** the adapter does not attempt recovery and the failure propagates

#### Scenario: Unrelated apply failures are not treated as recoverable
- **WHEN** an apply fails for a reason other than an already-exists conflict for this booking's resource (for example a quota error)
- **THEN** the adapter does not import, destroy, or re-apply, and the original error propagates so the task's retry/FAILED handling runs

### Requirement: Recovery is idempotent across retries
Because provisioning retries the whole task on failure, the recovery SHALL be safe to attempt repeatedly: a booking left with an orphaned resource SHALL eventually reach a successful apply on a subsequent attempt rather than failing identically on every retry.

#### Scenario: Repeated retries converge to success
- **WHEN** a booking's first apply orphans a VM and the task retries
- **THEN** the retry's recovery reconciles the orphan and the booking provisions successfully instead of failing with the same conflict again
