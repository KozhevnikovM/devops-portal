## ADDED Requirements

### Requirement: Environments have a bounded number of children

The system SHALL limit the number of children an environment can have to `ENVIRONMENT_MAX_CHILDREN`, a configured value (default 25, at least 1). An environment's children are created only when it is ordered, one per blueprint item, and an adopted standalone booking takes the place of an item. The limit is therefore enforced on blueprints and on orders:

- **Saving a blueprint.** A blueprint with more than `ENVIRONMENT_MAX_CHILDREN` items SHALL be rejected, whether it is created or updated, from the admin page or through the JSON API. The blueprint SHALL be left unchanged. The JSON API SHALL respond 422, and the admin page SHALL show its validation error. The error SHALL name the limit.
- **Ordering an environment.** An order from a blueprint with more than `ENVIRONMENT_MAX_CHILDREN` items SHALL be rejected before any child is created or any resource is reserved, through either the browser or the JSON API. This covers a blueprint saved before the limit existed. The JSON API SHALL respond 400, as for other invalid environment items, and the error SHALL name the limit.

**Effective limit.** Environments that already exist may exceed the limit. At startup, the service SHALL determine L, the largest number of children of any environment that is not fully released. Its effective limit `C_eff` SHALL then be the larger of `ENVIRONMENT_MAX_CHILDREN` and L, and SHALL stay fixed for the lifetime of the process. When L exceeds the configured limit, the service SHALL log a warning stating L and how many such environments exist. Fully released environments SHALL NOT count, because they never change again. After those environments are released, a restart brings `C_eff` back to the configured limit.

Every environment that is not fully released SHALL therefore have at most `C_eff` children while the process runs.

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

#### Scenario: Released legacy environments do not count
- **WHEN** the service starts and the only environments with more than `ENVIRONMENT_MAX_CHILDREN` children are fully released
- **THEN** `C_eff` equals `ENVIRONMENT_MAX_CHILDREN` and no warning is logged
