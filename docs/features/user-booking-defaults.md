# Feature: User Default Image & Hardware (#105)

## Goal

Allow users to save a preferred image and hardware config. The booking form
pre-selects these values so repeat bookings require fewer clicks.

## DB change

Add two nullable FK columns to `users`:

```
default_image_id     UUID nullable FK → vm_images.id
default_hw_config_id UUID nullable FK → hw_configs.id
```

`NULL` means no preference — booking form falls back to the first active option.

New Alembic migration: `0011_user_defaults.py`.

## Domain change (`app/domain/entities.py`)

Add to `User`:
```python
default_image_id: UUID | None = None
default_hw_config_id: UUID | None = None
```

## Model change (`app/infrastructure/database/models.py`)

Add to `UserModel`:
```python
default_image_id     = Column(UUID, ForeignKey("vm_images.id"), nullable=True)
default_hw_config_id = Column(UUID, ForeignKey("hw_configs.id"), nullable=True)
```

## Repository changes

`app/infrastructure/repositories/user_repo.py`:
- Include new fields in `_to_entity`
- Add `async set_defaults(session, user_id, image_id, hw_config_id)`

## Route changes (`app/presentation/routes/auth.py`)

- `GET /profile` — pass active `vm_images` and `hw_configs` so preference selects
  can be populated
- `PATCH /profile/defaults` — accepts `default_image_id` and `default_hw_config_id`
  form fields; calls `user_repo.set_defaults()`; returns a redirect or partial swap

## Booking form (`app/presentation/templates/partials/booking_form.html`)

Mark the user's default option as `selected`:

```html
<option value="{{ img.id }}"
    {% if img.id == current_user.default_image_id %}selected{% endif %}>
    {{ img.name }}
</option>
```

Same for hardware config.

## Profile page (`app/presentation/templates/profile.html`)

Add a "Booking defaults" section with two `<select>` dropdowns and a Save button:

```
Booking defaults
  Image    [ Ubuntu 22.04 ▾ ]
  Hardware [ medium         ▾ ]
  [ Save ]
```

`hx-patch="/profile/defaults"`, targeting the defaults section for inline swap on save.

## Files changed

| File | Change |
|------|--------|
| `app/domain/entities.py` | Add `default_image_id`, `default_hw_config_id` to `User` |
| `app/infrastructure/database/models.py` | Add two nullable FK columns to `UserModel` |
| `app/infrastructure/repositories/user_repo.py` | `set_defaults()`; include fields in `_to_entity` |
| `app/presentation/routes/auth.py` | Pass images/hw_configs to profile; add `PATCH /profile/defaults` |
| `app/presentation/templates/profile.html` | Booking defaults section |
| `app/presentation/templates/partials/booking_form.html` | Pre-select defaults |
| `alembic/versions/0011_user_defaults.py` | Migration |

## Tests

- `PATCH /profile/defaults` saves both fields; subsequent `GET /profile` reflects them
- Booking form renders with `selected` on user's default image and hw_config
- No default set → no `selected` attribute on any option
