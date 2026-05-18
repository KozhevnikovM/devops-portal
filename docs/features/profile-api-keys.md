# Feature: API Key Management in User Profile

## Goal

Let any authenticated user create and revoke their own API keys directly from the
`/profile` page, without needing an admin or curl. Eliminates the "Option B" workaround
documented in the API reference.

## What Changes

### Routes (`app/presentation/routes/auth.py`)

Two new endpoints, both protected by `require_user`:

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/profile/api-keys` | Create a new API key for the current user |
| `DELETE` | `/profile/api-keys/{key_id}` | Revoke one of the current user's API keys |

`POST /profile/api-keys` accepts an optional form field `description` and returns an
HTML fragment (the new key row) with the raw key visible. The raw key is shown **once**
in the UI — the user must copy it before navigating away or dismissing it.

`DELETE /profile/api-keys/{key_id}` revokes the key and returns an empty response so
HTMX can remove the row.

Both routes reuse the existing `UserRepository.create_api_key()` and
`UserRepository.revoke_api_key()` methods — no repository changes needed.

The `GET /profile` route already exists; it needs to additionally fetch and pass
`api_keys: list[APIKey]` to the template context via `UserRepository.list_api_keys()`.

### Template (`app/presentation/templates/profile.html`)

New **API Keys** section below the timezone form:

```
API Keys
────────────────────────────────────────────────

  Description          Created        
  Jenkins CI           2026-05-10      [Revoke]
  My laptop            2026-05-18      [Revoke]

  [Description (optional)  ]  [ + Generate key ]

  ┌─────────────────────────────────────────────────────────────────┐
  │ New key (copy now — shown once):                                │
  │  dp_a1b2c3d4e5f6...                              [Copy]        │
  └─────────────────────────────────────────────────────────────────┘
```

- The key list is rendered server-side on `GET /profile`; each row has an HTMX
  `hx-delete` Revoke button that removes the row on success.
- The generate form posts to `POST /profile/api-keys` and swaps in the new row
  (including the one-time raw key reveal) via HTMX into the key list.
- The raw key reveal block only appears in the response to a successful create — it is
  not persisted or re-shown on reload.
- The **Copy** button uses the Clipboard API (`navigator.clipboard.writeText`).

## Expected Behaviour / Edge Cases

- Users can only create and revoke their own keys. The ownership check already exists
  in the API endpoints; the profile routes enforce it by using `current_user.id` directly
  (no user_id path parameter needed).
- No limit on the number of keys per user (admins can revoke via the existing API if
  needed).
- If a user has no keys the section shows "No API keys yet." and the generate form.
- Revoking the key currently used to authenticate does not invalidate the session — the
  user stays logged in via cookie; subsequent API calls with that key will start returning
  401.
- No new DB migrations — `api_keys` table already exists.
