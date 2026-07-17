# Feature: YAML editor for Ansible variables in admin UI (#285)

## Goal

Replace the single-line JSON `<input>` for Ansible role `default_vars` and blueprint
`extra_vars` with a multi-line YAML `<textarea>`. YAML is more readable and less error-prone
for nested maps that operators write by hand.

## What changes

### Backend

**`app/presentation/routes/admin.py`** (or whichever route handles role create/edit and
blueprint create/edit)
- On form submit, parse the raw textarea value as YAML (`yaml.safe_load`).
- Validate it is a `dict` (not a scalar, list, or null); return a 422 with a clear message if not.
- Store the parsed dict in the DB as today (JSON column). No schema change.
- `pyyaml` must be added to `requirements.txt` if not already present.

**Display** (GET/edit paths)
- When rendering the textarea, convert the stored dict to YAML with `yaml.dump(...,
  default_flow_style=False, allow_unicode=True)` so it is human-readable on re-open.

### Template

**`app/presentation/templates/admin/catalog.html`** (and any blueprint edit template)
- Change `<input type="text" name="default_vars">` → `<textarea name="default_vars" rows="6">`.
- Update the label: `Default vars (YAML)`.
- Placeholder example in YAML format:
  ```yaml
  packages:
    - git
    - curl
  debug: false
  ```

### Dependency

`pyyaml` — add to `requirements.txt`. Already available in many Python environments;
pure-Python, no build step.

## Expected behaviour / edge cases

- **Valid YAML dict** → parsed and stored; success redirect.
- **YAML parse error** → 422 with message `"default_vars must be valid YAML"`.
- **YAML that parses to a non-dict** (e.g. a list or bare string) → 422 with message
  `"default_vars must be a YAML mapping (key: value pairs)"`.
- **Empty textarea** → treated as `{}` (empty dict), same as today's empty JSON.
- **Existing records** (stored as JSON) displayed correctly because `yaml.dump({})` of a Python
  dict produces valid YAML.
- **Blueprint `extra_vars`** follows the same pattern as role `default_vars`.

## Test

- Unit test the YAML-parse helper: valid dict, parse error, non-dict type → check return values.
- Integration test: POST role with YAML `default_vars` → verify stored dict matches.
