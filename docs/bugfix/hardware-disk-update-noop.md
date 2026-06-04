# Bugfix: `PATCH /api/hardware/{id}` silently drops disk updates (#140)

**Type: Bug** · Source: CQ#2 · Phase 1, item #4

## Root cause

The `HWConfigUpdate` schema ([`app/presentation/routes/api.py`](../../app/presentation/routes/api.py))
declares the disk field as `disk_mb`, but the ORM column on `HWConfigModel`
([`app/infrastructure/database/models.py`](../../app/infrastructure/database/models.py)) is
`hdd_mb`:

```python
class HWConfigUpdate(BaseModel):
    ...
    disk_mb: Optional[int] = None   # <-- wrong name
```

`update_hardware` does `fields = body.model_dump(exclude_none=True)` and the repository applies
them with `setattr(model, key, value)`. Because `disk_mb` is **not a mapped column**, `setattr`
just attaches a stray Python attribute to the model instance — it never reaches a column, so the
commit persists nothing. **Disk edits via the API silently no-op**; the existing `hdd_mb` value is
untouched and the response echoes the unchanged value. (`HWConfigCreate` and `HWConfigResponse`
already use `hdd_mb` correctly, so only the update path is affected.)

## Change

Rename the schema field `disk_mb` → `hdd_mb` so it matches the column and actually persists:

```python
class HWConfigUpdate(BaseModel):
    ...
    hdd_mb: Optional[int] = None
```

No repository or model change is needed — once the key matches the column, the existing
`setattr` loop persists it.

> **Sequencing note.** Phase 3 (`feature/drive-type-quota`, #147) later renames the column itself
> to a generic `disk_mb` and adds a `drive_type`. This fix deliberately renames the *schema* to the
> current column name (`hdd_mb`) so the endpoint stops dropping data **now**; Phase 3 will do the
> coordinated column+schema rename. Shipping the quick fix first keeps the endpoint correct in the
> interim.

## Expected behaviour after the fix

- `PATCH /api/hardware/{id}` with `{"hdd_mb": 51200}` persists the new disk size and the response
  reflects it.
- Other fields (`name`, `cpus`, `memory_mb`, `is_active`) are unchanged in behaviour.

## Test

`tests/test_hardware_disk_update.py`: `PATCH /api/hardware/{id}` with a new `hdd_mb` calls the repo
`update` with `hdd_mb` in the fields dict and returns the updated value. (Regression: the field
name reaches the persistence layer as `hdd_mb`, not the stray `disk_mb`.)

## Docs

`api-reference.md` — `PATCH /api/hardware/{id}` body uses `hdd_mb` (correcting any `disk_mb`
reference).
