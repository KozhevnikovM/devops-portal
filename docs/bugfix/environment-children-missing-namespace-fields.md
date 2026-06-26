# Bugfix: namespace name and cluster missing from environment Resources column

## Root cause

`EnvironmentRepository._children()` fetched child bookings with only a `UserModel` join —
`NamespaceModel` and `StaticVMModel` were not joined. `_booking_to_entity` received `namespace=None`
and `static_vm=None`, so `namespace_name`, `cluster_name`, `static_vm_name`, and `static_vm_host`
were always `None`, rendering as `—` in the Resources column of the Environments tab.

## What changes

`environment_repo.py` — `_children()` now outer-joins both `NamespaceModel` and `StaticVMModel`
and passes them through to `_booking_to_entity`:

```python
select(BookingModel, UserModel.username, NamespaceModel, StaticVMModel)
.outerjoin(NamespaceModel, NamespaceModel.id == BookingModel.namespace_id)
.outerjoin(StaticVMModel, StaticVMModel.id == BookingModel.static_vm_id)
```

## Expected behaviour after fix

- Namespace bookings in an environment show `namespace-name / cluster-name` in the Resources column.
- Static VM bookings in an environment show `vm-name @ host` in the Resources column.
- VM bookings are unaffected (they have no namespace or static VM FK).
