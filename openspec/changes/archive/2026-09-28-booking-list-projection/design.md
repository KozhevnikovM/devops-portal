## Context

See `proposal.md` for the motivation. The current shape:

- **Only two bulk list reads.** `BookingRepository.list_all` and `list_by_user` (`app/infrastructure/repositories/booking_repo.py`) are the only bulk booking list reads.
  - Each does `select(BookingModel, UserModel.username, NamespaceModel, StaticVMModel, _CreatorUser.username)` with outer joins for the owner, namespace, static VM and creator.
  - Each maps every row through `_to_entity`, which fills a full `Booking`, including `provisioning_log`, `startup_script`, `extra_vars` and `config_roles` (each role dict carries `vars` and `secret_vars`), plus the `details` and `footprint` value objects.
  - Neither is declared on `BookingRepositoryPort`, and no use case calls them.
- **Two callers.**
  - `_render_bookings_page` in `routes/bookings.py` renders the HTML table.
  - `list_bookings` in `routes/api_bookings.py` builds the JSON list through `_summary`.
- **One shared row partial.** `partials/booking_row.html` renders a row, and every other path passes it a *full* `Booking` from `get()`:
  - `GET /bookings/{id}/row`, the 60 s fallback poll
  - the SSE `_render_booking_event`
  - `POST /bookings`
  - release, extend and label updates
  - admin force-release

  The template recomputes its permission gates in Jinja from `owner_username`, `created_by` and `current_user`. For the provisioning log it only checks truthiness. For the roles it only reads `role.name`.
- **Queue position.** `_attach_queue_position` sets `queue_position` after the read on each row, with one `COUNT` per queued row.

## Goals / Non-Goals

**Goals:**
- The two list reads select an explicit, closed set of scalar columns. Nothing on the list path builds a `Booking`.
- The row partial takes either a `BookingListItem` or a full `Booking` without branching.
- A test pins the projection's column set, so adding a detail-only column back fails CI.

**Non-Goals:**
- Changing the single-row, SSE or action paths. They keep loading a full `Booking` and are per-row, not bulk.
- Fixing the N+1 `COUNT` for queue positions. The behaviour is kept as is.
- Environment child rows. `environment_repo._children_batch` still uses `_to_entity`.
- Adding an `id` tiebreaker or an index for booking ordering. That belongs with booking pagination.

## Decisions

### D1. `BookingListItem` is a flat domain read model beside `Booking`

A plain `@dataclass(slots=True)` in `app/domain/booking_list.py`, with no framework imports. It mirrors how `app/domain/pagination.py` holds read-side values. Its fields are the union of what `booking_row.html` and `_summary` read, under the **same attribute names** `Booking` uses:

- **Identity and state:**
  - `id`, `user_id`, `status: BookingStatus`, `resource_type: ResourceType`
  - `ttl_minutes`, `expires_at`, `created_at`
  - `label`, `status_message`, `config_failed`
  - `environment_id`
- **People:** `owner_username`, `created_by`, `created_by_username`
- **VM:** `image_id`, `image_name`, `hw_config_id`, `hw_config_name`, `vm_ip`, `vm_password`
- **Namespace:** `namespace_name`, `cluster_name`, `api_url`
- **Static VM:** `static_vm_name`, `static_vm_host`, `static_vm_username`, `static_vm_password`, `static_vm_ssh_key`

  The table still renders these credentials. Moving them off the list read is a separate issue.
- **Derived:** `has_provisioning_log: bool`, `config_role_names: tuple[str, ...]`
- **Display-only, mutable:** `queue_position: int | None = None`, set after the read

`status` and `resource_type` stay enums, because the template reads `.value`. The dataclass is not frozen, because `_attach_queue_position` assigns `queue_position`. That helper is shared with the full-`Booking` paths, and a frozen type would need a second code path.

*Alternatives considered:*
- **Keep returning `Booking` built from a column subset**, with the detail fields left `None`. Rejected: a `Booking` whose `provisioning_log is None` is indistinguishable from one without a log. That is a silent lie that a later caller could act on. The acceptance criteria also ask that list rendering not build a `Booking`.
- **SQLAlchemy `load_only`/`defer` on `BookingModel`.** Rejected: it still builds ORM instances and full entities. A later attribute access would trigger a lazy load, which fails under async instead of being caught by a test.
- **Put the model in `application/`.** Rejected: the application layer has no read-model home yet, and the model is pure data with domain enums.

### D2. The row partial reads two derived names, and `Booking` exposes them too

`booking_row.html` changes two reads:
- `booking.provisioning_log` → `booking.has_provisioning_log`
- `booking.config_roles` / `role.name` → `booking.config_role_names` / `name`

`Booking` gains matching read-only properties:
- `has_provisioning_log`: `bool(self.provisioning_log)`
- `config_role_names`: a tuple of each role's `name`

So every renderer passes whatever it has, and the partial needs no `if` on the type. This follows the rule to minimise special cases: one template contract, two types that satisfy it. The truthiness rule (a non-empty string) matches what the template checks today.

*Alternative considered:* convert the full `Booking` to a `BookingListItem` on the single-row and SSE paths. Rejected: it adds a conversion to five call sites for no gain, and those paths are per-row.

### D3. The list SQL selects labelled scalar columns and derives the two flags in the database

A shared private builder, `_list_item_stmt()`, returns:

- `select(...)` of the explicit `BookingModel` columns, each labelled with its `BookingListItem` field name
- the owner and creator usernames
- `NamespaceModel.name`/`cluster_name`/`api_url`
- `StaticVMModel.name`/`host`/`username`/`password`/`ssh_key`

It uses the same four outer joins as today. `list_all` and `list_by_user` both build on it, so their statements cannot drift apart. The filter helpers `_apply_resource_type_filter` and `_apply_label_filter`, the hidden-released `WHERE` and `ORDER BY created_at DESC` are unchanged.

The two derived columns:

- **`has_provisioning_log`**: `coalesce(octet_length(provisioning_log), 0) > 0`.
  - On PostgreSQL, `octet_length` on `text` reads the stored size from the TOAST header without detoasting the value.
  - `length()` (a character count) or `<> ''` would have to fetch the out-of-line log.
  - The result matches Jinja truthiness: `NULL` and `''` are false.
- **`config_role_names`**: a correlated scalar subquery that collects `->> 'name'` over `jsonb_array_elements(config_roles)`, ordered by array position (`WITH ORDINALITY`), with `coalesce` to an empty array.
  - It returns `text[]`, so only the names travel to the app.
  - The per-role `vars`/`secret_vars` never leave the database.
  - `WITH ORDINALITY` keeps the role order the table shows today.

The mapper `_to_list_item(row)` builds a `BookingListItem` from `row._mapping`. It converts `status` and `resource_type` to enums and raises the same `ValueError` as `_to_entity` for an unknown status.

*Alternative considered:* select `config_roles` whole and take the names in Python. Rejected: it moves the encrypted `secret_vars` and the `vars` of every role over the wire for every listed booking. Those are the detail-only payloads this change exists to drop.

### D4. The repo list methods keep their names and change their return type

`list_all` and `list_by_user` keep their names and signatures. Their return type becomes `list[BookingListItem]`. They have exactly two callers and no port declaration.

Most route tests patch them with `Booking` objects. Those tests keep passing, because under D2 `Booking` satisfies the same template contract, and `_summary` reads only attributes both types have.

`_summary` is retyped to `BookingListItem | Booking` and reads `config_role_names` for `roles`. The JSON output is unchanged. See the risk on role names below.

### D5. Guard tests work on the statement's selected columns, not the SQL text

The new tests do three things:

1. **Projection shape.** Assert that the set of `BookingListItem` dataclass field names equals the set of labels in `_list_item_stmt().selected_columns`, minus the display-only `queue_position`. They also assert that neither set contains `provisioning_log`, `startup_script`, `extra_vars` or `config_roles`.
2. **Both list methods use the builder.** Capture the statement that each of `list_all`/`list_by_user` passes to `session.execute`, using the existing AsyncMock pattern from `test_filter_by_label.py`. Assert that the forbidden columns do not appear among its **selected** columns. Also assert that `BookingModel` is not an entity selected in it (`stmt.column_descriptions`).

   A plain substring test on the compiled SQL would falsely fail, because `provisioning_log` and `config_roles` legitimately appear inside the two derived expressions.
3. **Postgres integration.** Against a real database, insert a VM booking with a large log, roles with vars and secret vars, a startup script and extra-vars. Then check:
   - `list_by_user` returns `has_provisioning_log=True` and the ordered `config_role_names`.
   - An empty-string log gives `False`.
   - The table row and the `/row` rendering of the same booking are **semantically** equal. This is the spec's "List row matches the refreshed row" scenario.
     - Each booking is rendered from the bookings page that lists its type: VM and static VM from `/book/vm`, namespace from `/book/namespace`. `GET /` lists only VM and static VM.
     - Byte equality is not the contract. The list passes `is_first_row = loop.first`, which switches the action-menu positioning classes, and `/row` does not. So a small stdlib `html.parser` helper reduces each `<tr id="booking-<id>">` to a comparable summary:
       - the whitespace-normalised visible text, which covers state, labels, credentials, TTL/expiry, queue position and role chips
       - the set of actions, each as the tag, its `href`/`hx-get`/`hx-post`/`hx-put`/`hx-patch`/`hx-delete` target and its visible label

       `class` and other purely presentational attributes are ignored. The test compares the two summaries.

## Risks / Trade-offs

- **The partial silently reads an attribute the projection lacks.** Jinja renders a missing attribute as empty. → The row-parity test in D5.3 renders both paths for each resource type (VM, static VM, namespace, queued), each from the page that lists it. It renders as the owner and as a non-owner admin, and compares the semantic row summaries. Missing text, credentials or actions show up as a difference, while positioning classes do not. The field set is pinned by D5.1.
- **Role names come from a PostgreSQL-only JSONB function.** The unit suite uses mocked sessions and never runs it. → It is exercised by the `-m integration` suite, which CI already gates.
- **Rows without a `name` key.** `_summary` today emits `None` for a role dict with no `name` key. The SQL `->> 'name'` also yields `NULL`, which is kept in the array. Every writer (`_roles.py`, `order_environment.py`) always sets `name`, so the result is identical in practice.
- **Mutable read model.** Only `queue_position` is meant to be assigned. → It is documented on the field. The alternative, a frozen type with a second queue-position helper, would add a special case.

## Migration Plan

No schema change and no data migration. It deploys as a plain code change. To roll back, revert the commit. The template and entity properties are additive, so there is no mixed-version hazard between the web and worker processes, which do not render rows.
