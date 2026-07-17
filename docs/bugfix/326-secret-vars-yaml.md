# Bugfix #326 — Secret vars textarea rejects YAML input

## Root cause

`_parse_default_vars` was migrated from `json.loads` to `yaml.safe_load` in PR #319, but
`_parse_secret_vars` in `app/presentation/routes/admin.py` was missed. It still calls
`json.loads`, so any YAML input (including multi-line mappings) raises:

```
secret_vars must be valid JSON: Expecting value: line 1 column 1
```

## What changes

- `_parse_secret_vars`: replace `json.loads` / `json.JSONDecodeError` with
  `yaml.safe_load` / `yaml.YAMLError`, identical pattern to `_parse_default_vars`.
- Secret vars template (`partials/role_table.html`): change the placeholder from
  `{"token": ""}` to `token: ""` and widen the textarea to `rows="4"` to match the
  default vars field.

## Expected behaviour after fix

YAML mappings (and JSON, which is valid YAML) are accepted in the secret vars textarea.
Lists and bare scalars are rejected with "must be a YAML mapping".
