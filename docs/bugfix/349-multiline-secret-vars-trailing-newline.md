# Bugfix: multiline secret_vars (and default_vars) lose their trailing newline (#349)

## Root cause

`_parse_secret_vars` and `_parse_default_vars` (`app/presentation/routes/admin.py`) both start
with:

```python
raw = (raw or "").strip()
```

`.strip()` removes leading/trailing whitespace from the **entire textarea submission**, not
just from around it. When the *last* key in the YAML mapping is a multi-line literal block
scalar (`key: |` followed by indented lines — the standard YAML way to embed a certificate,
SSH key, or any multi-line text), that key's value is also the very end of the raw text, so
its trailing newline is part of what `.strip()` removes.

Confirmed directly against the production function:

```python
>>> _parse_secret_vars("my_owned_ca: |\n  -----BEGIN CERTIFICATE-----\n  ...\n  -----END CERTIFICATE-----\nmy_owned_keys: |\n  some-key\n  multiline\n  here\n")
my_owned_ca:   ends with "\n"  →  True   (not the last field — unaffected)
my_owned_keys: ends with "\n"  →  False  (last field — trailing newline silently stripped)
```

Only the **last** field in the textarea is affected — every other field's value keeps its
newline intact because YAML's own line structure (the next `key:` on its own line) terminates
the block scalar correctly regardless of `.strip()`.

This matters specifically for certificate/key-shaped secrets (the reported case: a CA
certificate and a private key) because most consumers (OpenSSL, nginx, ssh, cert bundles that
concatenate multiple PEM blocks) require a trailing newline after the last line of PEM content
to parse it correctly. A single-line secret (a password, a token) never has this problem, and
neither does a multi-line value that isn't the *last* key in the textarea — which is why the
issue looked specific to "multiline secrets" without being universal to every multi-line value.

## What changes

**`app/presentation/routes/admin.py`**

In both `_parse_secret_vars` and `_parse_default_vars`, stop mutating the value that gets
parsed. Use `.strip()` only to test for "is this submission blank" (so an accidental
whitespace-only textarea still means "keep existing" / "empty mapping"), and pass the
*original*, un-stripped `raw` string to `yaml.safe_load()`:

```python
def _parse_secret_vars(raw: str) -> dict | None:
    raw = raw or ""
    if not raw.strip():
        return None
    try:
        parsed = yaml.safe_load(raw)
    ...
```

(same change in `_parse_default_vars`, minus the "return None" early-out since that function
returns `{}` for blank input instead.)

YAML parsing itself is unaffected by surrounding whitespace/blank lines — `yaml.safe_load`
already tolerates leading/trailing blank lines around the document, so removing the `.strip()`
from the parsed content doesn't introduce any new failure mode; it only stops silently
truncating a value the user actually typed.

## Expected behaviour after fix

- A multi-line block scalar (`key: |` style) as the **last** field in either the
  `default_vars` or `secret_vars` textarea keeps its trailing newline, matching what the user
  typed.
- Blank/whitespace-only submissions still behave as before (`None` for secret_vars = "keep
  existing"; `{}` for default_vars = "no vars").
- All other parsing behaviour (YAML mapping requirement, error messages) is unchanged.
