## Purpose

Defines how the browser ordering endpoints interpret the optional resource-selection fields that the order forms submit, so the forms' "no specific choice" options always work.

## ADDED Requirements

### Requirement: A blank optional resource selection means "no explicit selection"
The browser ordering endpoints SHALL treat an optional resource-selection field that is submitted with an empty value the same as a request that omits the field. This covers `namespace_id` on `POST /environments`, and `namespace_id` and `static_vm_id` on `POST /bookings`. The request SHALL proceed with no explicit selection and SHALL NOT be rejected for an invalid identifier.

#### Scenario: Environment ordered with "Blueprint default" namespace
- **WHEN** a signed-in user submits `POST /environments` with a valid blueprint, a TTL, and `namespace_id=""`
- **THEN** the environment is ordered with no namespace override, using the blueprint's own namespace configuration
- **AND** the response is the same as when `namespace_id` is omitted

#### Scenario: Blueprint without a namespace item ordered with "Blueprint default"
- **WHEN** a signed-in user submits `POST /environments` with `namespace_id=""` for a blueprint that has no namespace item
- **THEN** the environment is ordered successfully, without a namespace

#### Scenario: Namespace booked with "Any available"
- **WHEN** a signed-in user submits `POST /bookings` with `resource_type=NAMESPACE`, a TTL, and `namespace_id=""`
- **THEN** the booking is made from any available namespace, or queued if none is free, as when `namespace_id` is omitted

#### Scenario: Static VM reserved with "Any available"
- **WHEN** a signed-in user submits `POST /bookings` with `resource_type=STATIC_VM`, a TTL, and `static_vm_id=""`
- **THEN** a static VM is reserved from any available one, or queued if none is free, as when `static_vm_id` is omitted

#### Scenario: Field omitted entirely
- **WHEN** any of these fields is absent from the request
- **THEN** the request proceeds with no explicit selection, as it does today

### Requirement: A non-empty resource selection must be a valid identifier
For the same fields, a non-empty value SHALL be treated as an explicit selection. A well-formed identifier SHALL be used as the selected resource. A malformed non-empty value SHALL be rejected with HTTP 422 and SHALL NOT be ignored or treated as "no explicit selection".

#### Scenario: Specific namespace chosen for an environment
- **WHEN** a signed-in user submits `POST /environments` with `namespace_id` set to a well-formed namespace identifier
- **THEN** the environment is ordered with that namespace as the override

#### Scenario: Specific namespace or static VM chosen for a booking
- **WHEN** a signed-in user submits `POST /bookings` with `namespace_id` (namespace booking) or `static_vm_id` (static-VM booking) set to a well-formed identifier
- **THEN** that specific resource is requested

#### Scenario: Malformed identifier
- **WHEN** a signed-in user submits `POST /environments` or `POST /bookings` with `namespace_id` or `static_vm_id` set to a non-empty value that is not a valid identifier (e.g. `junk`)
- **THEN** the response is HTTP 422 and no environment or booking is created

### Requirement: Each order form's default option can be submitted as rendered
The default "no specific choice" option rendered by each browser order form SHALL be accepted by its endpoint. This covers "Blueprint default" on the environment order form, and "Any available" for namespaces and for static VMs on the booking form.

#### Scenario: Default option value round-trips
- **WHEN** the value of a form's default option, as rendered by the server, is submitted to that form's endpoint together with otherwise valid fields
- **THEN** the endpoint does not reject it with HTTP 422
