# Bugfix: Environment row missing namespace (and static-VM) name (#271)

## Root cause

`EnvironmentRepository._children()` builds its child-booking query with only a `UserModel` join:

```python
select(BookingModel, UserModel.username)
    .join(UserModel, ...)
```

It never joins `NamespaceModel` or `StaticVMModel`, so `_booking_to_entity` is called with
`namespace=None, static_vm=None`. This means `namespace_name`, `cluster_name`, `api_url`,
`static_vm_name`, and `static_vm_host` are always `None` for every child booking of an environment.

The row template (`partials/environment_row.html`) already has the "ns: …" display at line 22,
but the data never arrives.

## What changes

**`app/infrastructure/repositories/environment_repo.py`**

- Import `StaticVMModel` from the models module.
- `_children()` — add `.outerjoin(NamespaceModel, …)` and `.outerjoin(StaticVMModel, …)` to the
  query and include both models in the `select(…)` projection.
- `_children()` — unpack `(b, u, ns, svm)` and pass `namespace=ns, static_vm=svm` to
  `_booking_to_entity`.

No template changes needed — the row template is already correct.

## Expected behaviour after the fix

| Scenario | Before fix | After fix |
|---|---|---|
| Environment with a NAMESPACE child booking | `namespace_name = None`, "ns: …" line hidden | `namespace_name = "dev1"`, "ns: dev1" shown |
| Environment with a STATIC_VM child booking | `static_vm_name = None`, shows "—" | `static_vm_name = "vm-01"`, shows "vm-01" |
| Environment with a VM child booking | unaffected (`image_name` is a stored column) | same |
| `GET /environments/{id}/row` poll endpoint | same missing data | same fix applies (also calls `_children`) |
