## ADDED Requirements

### Requirement: Environments have a bounded number of children

The system SHALL limit the number of children an environment can have to `ENVIRONMENT_MAX_CHILDREN`, a configured value (default 25, at least 1). An environment's children are created only when it is ordered, one per blueprint item, and an adopted standalone booking takes the place of an item. The limit is therefore enforced on blueprints and on orders:

- **Saving a blueprint.** A blueprint with more than `ENVIRONMENT_MAX_CHILDREN` items SHALL be rejected, whether it is created or updated, from the admin page or through the JSON API. The blueprint SHALL be left unchanged. The JSON API SHALL respond 422, and the admin page SHALL show its validation error. The error SHALL name the limit.
  - An update that keeps the stored items counts them: changing the name, description or active state of a blueprint saved before the limit existed with more items SHALL be rejected the same way.
  - Activating such a blueprint from the admin page SHALL be rejected the same way.
  - Deactivating or deleting a blueprint SHALL always be allowed, so an oversized one can be retired.
- **Ordering an environment.** An order from a blueprint with more than `ENVIRONMENT_MAX_CHILDREN` items SHALL be rejected before any child is created or any resource is reserved, through either the browser or the JSON API. This covers a blueprint saved before the limit existed. The JSON API SHALL respond 400, as for other invalid environment items, and the error SHALL name the limit.

**Effective limit.** Some environments that are not fully released may exceed the limit: those ordered before it existed, and those the previous app version ordered while it was still serving during a rolling or blue-green deploy. The service SHALL determine L, the largest number of children of any environment that is not fully released, and set its effective limit `C_eff` to the larger of `ENVIRONMENT_MAX_CHILDREN` and L. This SHALL happen:
- at startup, before serving;
- every `ENVIRONMENT_CHILD_LIMIT_REFRESH_SECONDS` (default 300);
- promptly when a reconciliation request meets an environment over the current `C_eff`.

When L exceeds the configured limit, the service SHALL log a warning stating L and how many such environments exist. Fully released environments SHALL NOT count, because they never change again. Once those environments are released, the next recomputation brings `C_eff` back to the configured limit. A failed recomputation SHALL keep the previous value.

An environment that is not fully released therefore has at most `C_eff` children, except one created since the last recomputation; recomputation then covers it.

#### Scenario: Blueprint over the limit is rejected
- **WHEN** an admin creates or updates a blueprint with `ENVIRONMENT_MAX_CHILDREN + 1` items, from the admin page or through the JSON API
- **THEN** the save is rejected with an error naming the limit, and the blueprint is unchanged

#### Scenario: Blueprint at the limit is accepted
- **WHEN** an admin saves a blueprint with exactly `ENVIRONMENT_MAX_CHILDREN` items
- **THEN** it is saved, and environments ordered from it are created with that many children

#### Scenario: Order from a legacy blueprint over the limit
- **WHEN** a user orders an environment from a blueprint saved with more items than the current limit
- **THEN** the order is rejected with an error naming the limit
- **AND** no environment or child booking is created, and no resource is reserved

#### Scenario: Legacy live environment raises the effective limit
- **WHEN** the service starts while a not fully released environment has `ENVIRONMENT_MAX_CHILDREN + 7` children
- **THEN** `C_eff` is `ENVIRONMENT_MAX_CHILDREN + 7` and a warning is logged

#### Scenario: Effective limit drops back after release
- **WHEN** the only environments over the limit are released
- **THEN** the next recomputation sets `C_eff` back to `ENVIRONMENT_MAX_CHILDREN`, without a restart

#### Scenario: Released legacy environments do not count
- **WHEN** the service starts and the only environments with more than `ENVIRONMENT_MAX_CHILDREN` children are fully released
- **THEN** `C_eff` equals `ENVIRONMENT_MAX_CHILDREN` and no warning is logged

#### Scenario: Metadata update of a legacy oversized blueprint
- **WHEN** an admin renames, re-describes or activates a blueprint saved before the limit existed with `ENVIRONMENT_MAX_CHILDREN + 1` items
- **THEN** the update is rejected with an error naming the limit, and the blueprint is unchanged

#### Scenario: Retiring a legacy oversized blueprint
- **WHEN** an admin deactivates or deletes a blueprint with `ENVIRONMENT_MAX_CHILDREN + 1` items
- **THEN** it is deactivated or deleted

#### Scenario: Environment ordered by the previous version during a deploy
- **WHEN** the previous app version, still serving during a blue-green deploy, orders an environment with `ENVIRONMENT_MAX_CHILDREN + 3` children after this process started
- **THEN** within `ENVIRONMENT_CHILD_LIMIT_REFRESH_SECONDS` this process's `C_eff` is at least `ENVIRONMENT_MAX_CHILDREN + 3`, without a restart
