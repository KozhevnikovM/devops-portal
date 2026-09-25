## Purpose

Defines which environments the environments list returns when released environments are hidden or shown. It also requires that hiding released environments stays cheap as released history grows. The rule must agree with the aggregate environment status derived from the child bookings.

## ADDED Requirements

### Requirement: Hiding released environments uses the derived-status rule

When released environments are hidden, the environments list SHALL exclude exactly the environments whose derived aggregate status is `RELEASED`. These are the environments that have at least one child booking and whose children are all `RELEASED`. Every other environment SHALL be returned. This includes an environment with no children and an environment with at least one child in any status other than `RELEASED`. The exclusion SHALL be decided from the current child booking statuses. The system SHALL NOT keep a separately stored environment status for this purpose.

Hiding released environments is the default for the browser environments page (`GET /environments`). `show_released=1` SHALL return released environments as well. The owner, `filter=mine|all` and `label` filters SHALL combine with this rule as they do today.

#### Scenario: Fully released environment is hidden by default
- **WHEN** a user views the environments page without `show_released` and one of their environments has two children, both `RELEASED`
- **THEN** that environment is not listed

#### Scenario: Fully released environment is shown on request
- **WHEN** the same user views the environments page with `show_released=1`
- **THEN** the fully released environment is listed with status `RELEASED`

#### Scenario: Mixed child statuses stay visible
- **WHEN** an environment has one `RELEASED` child and one `READY` child, and the page is viewed without `show_released`
- **THEN** the environment is listed, with the derived status `FAILED`

#### Scenario: Releasing environment stays visible
- **WHEN** an environment has one `RELEASED` child and one `RELEASING` child, and the page is viewed without `show_released`
- **THEN** the environment is listed

#### Scenario: Active environment stays visible
- **WHEN** an environment's children are all `READY`, or any child is still `QUEUED`, `PENDING`, `PROVISIONING`, `CONFIGURING` or `RETRY`, and the page is viewed without `show_released`
- **THEN** the environment is listed

#### Scenario: Failed environment stays visible
- **WHEN** an environment has one `FAILED` child and one `RELEASED` child, and the page is viewed without `show_released`
- **THEN** the environment is listed, with the derived status `FAILED`

#### Scenario: Environment with no children stays visible
- **WHEN** an environment has no child bookings and the page is viewed without `show_released`
- **THEN** the environment is listed, with the derived status `READY`

#### Scenario: Filters combine with hiding released
- **WHEN** a user views the environments page with `filter=all` and a `label` and without `show_released`
- **THEN** the result is exactly the label-matching environments that are not fully released

### Requirement: Released history does not add to the default environments list's cost

When released environments are hidden, fully released environments SHALL be excluded before any child bookings are loaded. Child bookings SHALL be loaded only for the environments that are returned. The database SHALL be able to find the child bookings of an environment, and whether it has any non-`RELEASED` child, through an index rather than a full scan of all bookings.

#### Scenario: Large released history with a small active set
- **WHEN** a user has many fully released environments and a few active ones, and views the environments page without `show_released`
- **THEN** only the active environments are returned
- **AND** child bookings are loaded only for those active environments, not for any released one

#### Scenario: Child lookups use an index
- **WHEN** the query plan of the hidden-released environments list is inspected on PostgreSQL with enough booking rows that a sequential scan is not the cheapest option
- **THEN** the lookup of child bookings by environment uses an index on the booking's environment

### Requirement: The JSON environments list is unchanged

The JSON environments list (`GET /api/v1/environments` and the legacy unversioned `GET /api/environments`) SHALL keep its existing contract. It SHALL still return released environments with the same fields and derived `status`.

#### Scenario: JSON list still includes released environments
- **WHEN** a user calls `GET /api/v1/environments` and one of their environments is fully released
- **THEN** the response includes that environment with `status` `RELEASED`
