# Feature: Hardware Config UI in GB (#104)

## Goal

The admin catalog currently shows `memory_mb` and `hdd_mb` fields in MB, requiring
admins to enter values like `4096` and `51200`. Change the create/edit forms to accept
GB values. Conversion to MB happens in the route. No DB schema change.

## What changes

### Route change (`app/presentation/routes/admin.py`)

In `admin_create_hardware` and `admin_update_hardware`, read `memory_gb` and `hdd_gb`
form fields and multiply by 1024 before storing:

```python
memory_mb = memory_gb * 1024
hdd_mb    = hdd_gb    * 1024
```

### Template changes

`app/presentation/templates/partials/hw_config_table.html` and
`app/presentation/templates/admin/catalog.html`:

- Field names: `memory_gb`, `hdd_gb`
- Labels: "RAM (GB)", "HDD (GB)"
- Displayed values in table: `{{ config.memory_mb // 1024 }}` and `{{ config.hdd_mb // 1024 }}`
- Edit form pre-populates with GB values
- Placeholder examples: `4`, `50` instead of `4096`, `51200`

## Files changed

| File | Change |
|------|--------|
| `app/presentation/routes/admin.py` | Multiply `memory_gb` × 1024 and `hdd_gb` × 1024 in create + update |
| `app/presentation/templates/partials/hw_config_table.html` | Field names, labels, display values → GB |

## Tests

- Create hardware config with `memory_gb=4`, `hdd_gb=50` → stored as `memory_mb=4096`, `hdd_mb=51200`
- Edit hardware config → form pre-populated with GB values (memory_mb ÷ 1024)
